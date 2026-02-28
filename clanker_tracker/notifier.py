"""Telegram notification sender for high-scoring tokens.

Sends rich HTML-formatted alerts containing:
- Token name, symbol, contract address
- Origin tweet / cast URL and author
- Project idea summary
- Quality score breakdown
- Quick trading links (DexScreener, Uniswap on Base)
- Inline action buttons for trading and details
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx
import structlog
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from .config import AppConfig
from .filters import FilterResult
from .models import SmartWallet, Token, TokenContext

logger = structlog.get_logger(__name__)


class TelegramNotifier:
    """Formats and sends Telegram alerts for qualifying tokens."""

    _DEX_BASE = "https://api.dexscreener.com/latest/dex"

    def __init__(self, config: AppConfig) -> None:
        self.cfg = config.telegram
        self._bot: Optional[Bot] = None
        if self.cfg.bot_token:
            self._bot = Bot(token=self.cfg.bot_token)
        self._http = httpx.AsyncClient(
            timeout=10,
            headers={"User-Agent": "BaseSleuth/1.0"},
        )

    @property
    def enabled(self) -> bool:
        return self._bot is not None and self.cfg.chat_id is not None

    # ------------------------------------------------------------------
    # Live DexScreener data
    # ------------------------------------------------------------------

    async def _fetch_live_dex(self, token_address: str) -> dict | None:
        """Fetch live mcap / FDV / liquidity from DexScreener.

        Returns dict with keys: market_cap, fdv, liquidity, price
        or None on failure.
        """
        if not token_address:
            return None
        url = f"{self._DEX_BASE}/tokens/{token_address}"
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
            pairs = resp.json().get("pairs") or []
            if not pairs:
                return None
            pair = pairs[0]
            return {
                "market_cap": _safe_float(pair.get("marketCap")),
                "fdv": _safe_float(pair.get("fdv")),
                "liquidity": _safe_float((pair.get("liquidity") or {}).get("usd")),
                "price": _safe_float(pair.get("priceUsd")),
            }
        except Exception as exc:
            logger.debug("dex.live_fetch.failed", addr=token_address[:12], error=str(exc))
            return None

    @staticmethod
    def _format_dex_line(dex: dict) -> str:
        """Format a compact Market Cap / FDV / Liq line."""
        parts: list[str] = []
        mc = dex.get("market_cap")
        fdv = dex.get("fdv")
        liq = dex.get("liquidity")
        price = dex.get("price")
        if mc and mc > 0:
            parts.append(f"MCap ${_fmt_number(mc)}")
        if fdv and fdv > 0 and fdv != mc:
            parts.append(f"FDV ${_fmt_number(fdv)}")
        if liq and liq > 0:
            parts.append(f"Liq ${_fmt_number(liq)}")
        if price and price > 0:
            if price >= 0.01:
                parts.append(f"Price ${price:,.4f}")
            else:
                parts.append(f"Price ${price:.6g}")
        return " · ".join(parts) if parts else ""

    async def notify(
        self,
        token: Token,
        ctx: Optional[TokenContext],
        result: FilterResult,
        *,
        nansen_buys: list[dict] | None = None,
    ) -> bool:
        """Send a Telegram alert for *token*. Returns True on success.

        Args:
            nansen_buys: Optional list of dicts with keys
                {wallet_address, wallet_label, usd_value, token_symbol, date}
                from Nansen-tagged wallet_swaps for this token.
        """
        if not self.enabled:
            logger.warning("telegram.disabled", reason="missing bot_token or chat_id")
            return False

        dex = await self._fetch_live_dex(token.contract_address)
        message = self._format_message(token, ctx, result, nansen_buys=nansen_buys, dex=dex)
        buttons = self._build_alert_buttons(token)

        try:
            async with Bot(token=self.cfg.bot_token) as bot:
                await bot.send_message(
                    chat_id=self.cfg.chat_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    disable_notification=self.cfg.disable_notification,
                    reply_markup=buttons,
                )
            logger.info("telegram.sent", token=token.symbol, score=result.final_score)
            return True
        except TelegramError as exc:
            logger.error("telegram.send.failed", token=token.symbol, error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Message formatting
    # ------------------------------------------------------------------

    def _format_message(
        self,
        token: Token,
        ctx: Optional[TokenContext],
        result: FilterResult,
        *,
        nansen_buys: list[dict] | None = None,
        dex: dict | None = None,
    ) -> str:
        lines: list[str] = []

        # ── Header with badges ──────────────────────────────
        badges: list[str] = []
        if getattr(token, "is_breakout", False):
            badges.append("📈 Breakout")
        if getattr(token, "is_champagne", False):
            badges.append("🍾 Champagne")
        if getattr(token, "is_verified", False):
            badges.append("✅ Verified")
        platform = getattr(token, "launch_platform", None)
        if platform and platform != "unknown":
            platform_emoji = {
                "bankr": "🏦", "clawnch": "🐱", "farcaster": "🟣", "direct": "🔧",
            }
            badges.append(f"{platform_emoji.get(platform, '🔗')} {platform.title()}")

        badge_str = "  ".join(badges)
        lines.append(f"🚀 <b>New Token Alert</b>")
        if badge_str:
            lines.append(badge_str)
        lines.append("")

        # ── Identity ────────────────────────────────────────
        lines.append(f"<b>{_esc(token.name or 'Unknown')}</b> (${_esc(token.symbol or '???')})")
        lines.append(f"<code>{token.contract_address}</code>")

        # ── Live market data ────────────────────────────────
        if dex:
            dex_line = self._format_dex_line(dex)
            if dex_line:
                lines.append(f"💰 {dex_line}")
        lines.append("")

        # ── Score & stage breakdown ─────────────────────────
        score_bar = self._score_bar(result.final_score)
        lines.append(f"Score: {score_bar} <b>{result.final_score:.0%}</b>")

        stage_names = [
            "Reject Gate", "DEX Metrics", "Momentum",
            "Smart Money", "Context",
        ]
        for i, sr in enumerate(result.stage_results):
            label = stage_names[i] if i < len(stage_names) else f"Stage {i+1}"
            emoji = "✅" if sr.passed else "❌"
            lines.append(f"  {emoji} {label}: {sr.score:.0%} — {_esc(sr.reason)}")

        lines.append("")

        # ── DEX metrics snapshot (if available) ─────────────
        if hasattr(result, "stage_results") and len(result.stage_results) > 1:
            sr_metrics = result.stage_results[1]
            reason = sr_metrics.reason
            # The reason string contains key metrics — show raw
            if "$" in reason or "liq" in reason.lower():
                lines.append(f"📊 <i>{_esc(reason[:200])}</i>")
                lines.append("")

        # ── Origin context ──────────────────────────────────
        if ctx and ctx.origin_url:
            platform_label = {
                "x": "𝕏 Tweet",
                "farcaster": "🟣 Farcaster Cast",
            }.get(ctx.origin_platform or "", "🔗 Origin")

            lines.append(f"<b>{platform_label}:</b>")
            lines.append(f'<a href="{_esc(ctx.origin_url)}">{_esc(ctx.origin_url[:80])}</a>')
            if ctx.origin_author:
                lines.append(f"By: {_esc(ctx.origin_author)}")
            if ctx.project_idea:
                lines.append(f"💡 <i>{_esc(ctx.project_idea[:200])}</i>")
            lines.append(f"Strategy: {_esc(ctx.resolution_strategy or 'n/a')}")
        else:
            lines.append("⚠️ <i>Origin not resolved</i>")

        lines.append("")

        # ── Trading links ───────────────────────────────────
        addr = token.contract_address
        lines.append("<b>Trade:</b>")
        lines.append(
            f'📊 <a href="https://dexscreener.com/base/{addr}">DexScreener</a>'
            f' | 🦄 <a href="https://app.uniswap.org/swap?chain=base&outputCurrency={addr}">Uniswap</a>'
        )
        lines.append(
            f'🔍 <a href="https://www.clanker.world/clanker/{addr}">Clanker Page</a>'
        )

        # ── Nansen smart money activity ──────────────────────
        if nansen_buys:
            n_wallets = len(nansen_buys)
            total_usd = sum(b.get("usd_value") or 0 for b in nansen_buys)
            lines.append("")
            if total_usd > 0:
                lines.append(
                    f"🧠 <b>Nansen Smart Money:</b> {n_wallets} wallet{'s' if n_wallets != 1 else ''}"
                    f" bought ${_fmt_number(total_usd)}"
                )
            else:
                lines.append(
                    f"🧠 <b>Nansen Smart Money:</b> {n_wallets} wallet{'s' if n_wallets != 1 else ''}"
                    f" bought this token"
                )
            # Show up to 3 individual wallets
            for buy in nansen_buys[:3]:
                label = buy.get("wallet_label") or buy["wallet_address"][:8] + "…"
                usd = buy.get("usd_value")
                usd_str = f" ${_fmt_number(usd)}" if usd and usd > 0 else ""
                lines.append(f"  🟢 {_esc(label)}{usd_str}")
            if n_wallets > 3:
                lines.append(f"  … +{n_wallets - 3} more")

        # ── Time since launch ───────────────────────────────
        launched = getattr(token, "launched_at", None)
        if launched:
            delta = datetime.now(timezone.utc) - launched
            total_seconds = int(delta.total_seconds())
            if total_seconds < 0:
                age_str = "just now"
            elif total_seconds < 3600:
                mins = max(1, total_seconds // 60)
                age_str = f"{mins} minute{'s' if mins != 1 else ''}"
            elif total_seconds < 86400:
                hrs = total_seconds // 3600
                age_str = f"{hrs} hour{'s' if hrs != 1 else ''}"
            else:
                days = total_seconds // 86400
                age_str = f"{days} day{'s' if days != 1 else ''}"
            lines.append("")
            lines.append(f"⏰ Time Since Launch: {age_str}")

        return "\n".join(lines)

    @staticmethod
    def _score_bar(score: float, length: int = 10) -> str:
        filled = round(score * length)
        return "█" * filled + "░" * (length - filled)

    @staticmethod
    def _build_alert_buttons(token: Token) -> InlineKeyboardMarkup:
        """Build inline action buttons for a token alert."""
        addr = token.contract_address
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📊 DexScreener",
                    url=f"https://dexscreener.com/base/{addr}",
                ),
                InlineKeyboardButton(
                    "🦄 Uniswap",
                    url=f"https://app.uniswap.org/swap?chain=base&outputCurrency={addr}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔍 Clanker Page",
                    url=f"https://www.clanker.world/clanker/{addr}",
                ),
                InlineKeyboardButton(
                    "📋 Dashboard",
                    callback_data="menu",
                ),
            ],
        ])

    # ------------------------------------------------------------------
    # Wallet buy alert — tracked wallet buys a new token
    # ------------------------------------------------------------------

    async def notify_wallet_buy(
        self,
        buy: dict,
        wallet: SmartWallet | None,
    ) -> bool:
        """Send alert when a tracked smart wallet buys a token."""
        if not self.enabled:
            return False

        tier_emoji = {1: "🥇", 2: "🥈", 3: "🥉"}.get(
            wallet.tier if wallet else 3, "🔘",
        )

        # Wallet label
        label = ""
        if wallet and wallet.arkham_label:
            label = wallet.arkham_label
        elif buy.get("wallet_alias"):
            label = buy["wallet_alias"]
        else:
            label = buy["wallet_address"][:8] + "…" + buy["wallet_address"][-4:]

        token_sym = buy.get("token_symbol") or "???"
        usd_val = buy.get("usd_value") or 0
        token_amount = buy.get("token_amount") or 0
        token_addr = buy.get("token_address", "")
        tx_hash = buy.get("tx_hash", "")
        wallet_addr = buy["wallet_address"]

        lines: list[str] = []

        # Header
        amount_str = f" ({_fmt_number(token_amount)} #{_esc(token_sym)})" if token_amount > 0 else ""
        usd_str = f" ${_fmt_number(usd_val)}" if usd_val > 0 else ""
        lines.append(
            f"🚨 <b>Smart Alert</b>: {tier_emoji} Smart Money Buy"
        )
        lines.append(
            f'🟢 <a href="https://basescan.org/address/{wallet_addr}">'
            f"{_esc(label)}</a> bought{amount_str}{usd_str} of "
            f"<b>#{_esc(token_sym)}</b>"
        )

        # Links
        link_parts = [f"#Base"]
        if tx_hash:
            link_parts.append(f'<a href="https://basescan.org/tx/{tx_hash}">Txn</a>')
        link_parts.append(f'<a href="https://basescan.org/address/{wallet_addr}">Wallet</a>')
        link_parts.append(f'<a href="https://dexscreener.com/base/{token_addr}">Chart</a>')
        link_parts.append(
            f'<a href="https://app.uniswap.org/swap?chain=base&outputCurrency={token_addr}">Swap</a>'
        )
        lines.append(" | ".join(link_parts))

        # Contract address
        lines.append(f"CA: <code>{token_addr}</code>")

        # Live market data
        dex = await self._fetch_live_dex(token_addr)
        if dex:
            dex_line = self._format_dex_line(dex)
            if dex_line:
                lines.append(f"💰 {dex_line}")

        message = "\n".join(lines)

        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📊 Chart",
                    url=f"https://dexscreener.com/base/{token_addr}",
                ),
                InlineKeyboardButton(
                    "🦄 Swap",
                    url=f"https://app.uniswap.org/swap?chain=base&outputCurrency={token_addr}",
                ),
            ],
        ])

        try:
            async with Bot(token=self.cfg.bot_token) as bot:
                await bot.send_message(
                    chat_id=self.cfg.chat_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=buttons,
                )
            logger.info(
                "telegram.wallet_buy",
                wallet=label,
                token=token_sym,
                usd=usd_val,
            )
            return True
        except TelegramError as exc:
            logger.error("telegram.wallet_buy.failed", error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Conviction alert — tracked wallet buys token already in our DB
    # ------------------------------------------------------------------

    async def notify_conviction(
        self,
        conviction: dict,
    ) -> bool:
        """Send high-priority conviction alert.

        Fires when a profitable tracked wallet buys a token that
        Base Sleuth already discovered or alerted on.
        """
        if not self.enabled:
            return False

        token: Token = conviction["token"]
        wallet: SmartWallet | None = conviction.get("wallet")
        buy: dict = conviction["buy"]
        was_alerted = conviction.get("is_alerted", False)

        tier_emoji = {1: "🥇", 2: "🥈", 3: "🥉"}.get(
            wallet.tier if wallet else 3, "🔘",
        )
        alert_badge = "ALERTED ✓" if was_alerted else "TRACKED"

        # Wallet label
        label = ""
        if wallet and wallet.arkham_label:
            label = wallet.arkham_label
        elif buy.get("wallet_alias"):
            label = buy["wallet_alias"]
        else:
            label = buy["wallet_address"][:8] + "…" + buy["wallet_address"][-4:]

        addr = token.contract_address
        wallet_addr = buy["wallet_address"]
        tx_hash = buy.get("tx_hash", "")
        usd_val = buy.get("usd_value") or 0

        lines: list[str] = []

        # Header
        lines.append(f"🔥 <b>CONVICTION</b> [{alert_badge}]")
        usd_str = f" ${_fmt_number(usd_val)}" if usd_val > 0 else ""
        lines.append(
            f'{tier_emoji} <a href="https://basescan.org/address/{wallet_addr}">'
            f"{_esc(label)}</a> bought{usd_str} of "
            f"<b>{_esc(token.name or 'Unknown')}</b> (${_esc(token.symbol or '???')})"
        )

        # Score
        if token.quality_score:
            lines.append(f"⭐ Score: {token.quality_score:.0%}")

        # Links
        link_parts = [f"#Base"]
        if tx_hash:
            link_parts.append(f'<a href="https://basescan.org/tx/{tx_hash}">Txn</a>')
        link_parts.append(f'<a href="https://dexscreener.com/base/{addr}">Chart</a>')
        link_parts.append(
            f'<a href="https://app.uniswap.org/swap?chain=base&outputCurrency={addr}">Swap</a>'
        )
        link_parts.append(f'<a href="https://basescan.org/address/{wallet_addr}">Wallet</a>')
        lines.append(" | ".join(link_parts))

        lines.append(f"CA: <code>{addr}</code>")

        # Live market data
        dex = await self._fetch_live_dex(addr)
        if dex:
            dex_line = self._format_dex_line(dex)
            if dex_line:
                lines.append(f"💰 {dex_line}")

        message = "\n".join(lines)

        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📊 Chart",
                    url=f"https://dexscreener.com/base/{addr}",
                ),
                InlineKeyboardButton(
                    "🦄 Swap",
                    url=f"https://app.uniswap.org/swap?chain=base&outputCurrency={addr}",
                ),
                InlineKeyboardButton(
                    "👛 Wallet",
                    url=f"https://basescan.org/address/{wallet_addr}",
                ),
            ],
        ])

        try:
            async with Bot(token=self.cfg.bot_token) as bot:
                await bot.send_message(
                    chat_id=self.cfg.chat_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=buttons,
                )
            logger.info(
                "telegram.conviction",
                token=token.symbol,
                wallet=label,
                alerted=was_alerted,
            )
            return True
        except TelegramError as exc:
            logger.error("telegram.conviction.failed", error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Nansen signal alert — forwarded from NansenBot
    # ------------------------------------------------------------------

    async def notify_nansen_signal(
        self,
        signal: dict,
    ) -> bool:
        """Send alert when NansenBot detects a smart wallet buy on Base.

        signal keys: wallet_address, wallet_label, token_address,
                     token_symbol, usd_value, eth_value, token_amount,
                     tx_hash, source
        """
        if not self.enabled:
            return False

        wallet_addr = signal.get("wallet_address", "")
        label = signal.get("wallet_label", wallet_addr[:10])
        token_sym = signal.get("token_symbol", "???")
        token_addr = signal.get("token_address", "")
        usd_val = signal.get("usd_value") or 0
        eth_val = signal.get("eth_value") or 0
        token_amount = signal.get("token_amount") or 0
        tx_hash = signal.get("tx_hash", "")

        lines: list[str] = []

        # Header
        lines.append("🔔 <b>Nansen Smart Alert</b>")

        amount_parts: list[str] = []
        if token_amount > 0:
            amount_parts.append(f"{_fmt_number(token_amount)} #{_esc(token_sym)}")
        if usd_val > 0:
            amount_parts.append(f"${_fmt_number(usd_val)}")
        if eth_val > 0:
            amount_parts.append(f"{eth_val:.2f} #ETH")
        amount_str = " · ".join(amount_parts) if amount_parts else f"#{_esc(token_sym)}"

        lines.append(
            f'🟢 <a href="https://basescan.org/address/{wallet_addr}">'
            f"{_esc(label)}</a> bought {amount_str}"
        )

        # Links
        link_parts = ["#Base"]
        if tx_hash:
            link_parts.append(f'<a href="https://basescan.org/tx/{tx_hash}">Txn</a>')
        link_parts.append(f'<a href="https://dexscreener.com/base/{token_addr}">Chart</a>')
        link_parts.append(
            f'<a href="https://app.uniswap.org/swap?chain=base&outputCurrency={token_addr}">Swap</a>'
        )
        lines.append(" | ".join(link_parts))

        if token_addr:
            lines.append(f"CA: <code>{token_addr}</code>")

        # Live market data
        if token_addr:
            dex = await self._fetch_live_dex(token_addr)
            if dex:
                dex_line = self._format_dex_line(dex)
                if dex_line:
                    lines.append(f"💰 {dex_line}")

        message = "\n".join(lines)

        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📊 Chart",
                    url=f"https://dexscreener.com/base/{token_addr}",
                ),
                InlineKeyboardButton(
                    "🦄 Swap",
                    url=f"https://app.uniswap.org/swap?chain=base&outputCurrency={token_addr}",
                ),
            ],
        ])

        try:
            async with Bot(token=self.cfg.bot_token) as bot:
                await bot.send_message(
                    chat_id=self.cfg.chat_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=buttons,
                )
            logger.info(
                "telegram.nansen_signal",
                wallet=label,
                token=token_sym,
            )
            return True
        except TelegramError as exc:
            logger.error("telegram.nansen_signal.failed", error=str(exc))
            return False


def _esc(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt_number(value: float) -> str:
    """Format a number compactly: 1_234_567 → '1.23M', 45_678 → '45.7K'."""
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}"


def _safe_float(val) -> float | None:
    """Safe float conversion for DexScreener values."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
