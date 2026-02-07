"""Telegram notification sender for high-scoring tokens.

Sends rich HTML-formatted alerts containing:
- Token name, symbol, contract address
- Origin tweet / cast URL and author
- Project idea summary
- Quality score breakdown
- Quick trading links (DexScreener, Uniswap on Base)
"""

from __future__ import annotations

from typing import Optional

import structlog
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from .config import AppConfig
from .filters import FilterResult
from .models import Token, TokenContext

logger = structlog.get_logger(__name__)


class TelegramNotifier:
    """Formats and sends Telegram alerts for qualifying tokens."""

    def __init__(self, config: AppConfig) -> None:
        self.cfg = config.telegram
        self._bot: Optional[Bot] = None
        if self.cfg.bot_token:
            self._bot = Bot(token=self.cfg.bot_token)

    @property
    def enabled(self) -> bool:
        return self._bot is not None and self.cfg.chat_id is not None

    async def notify(
        self,
        token: Token,
        ctx: Optional[TokenContext],
        result: FilterResult,
    ) -> bool:
        """Send a Telegram alert for *token*. Returns True on success."""
        if not self.enabled:
            logger.warning("telegram.disabled", reason="missing bot_token or chat_id")
            return False

        message = self._format_message(token, ctx, result)

        try:
            await self._bot.send_message(
                chat_id=self.cfg.chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                disable_notification=self.cfg.disable_notification,
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
    ) -> str:
        lines: list[str] = []

        # ── Header with badges ──────────────────────────────
        badges: list[str] = []
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

        return "\n".join(lines)

    @staticmethod
    def _score_bar(score: float, length: int = 10) -> str:
        filled = round(score * length)
        return "█" * filled + "░" * (length - filled)


def _esc(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
