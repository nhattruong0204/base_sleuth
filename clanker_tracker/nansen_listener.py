"""NansenBot Telegram message listener and parser.

Actively listens to @NansenBot (Telegram user ID 1593218799) using
the Telethon client API.  NansenBot never sends messages to our bot —
it has its own chat — so we need to subscribe via a *user* session.

URLs in NansenBot messages are embedded as Telegram ``TextUrl`` entities
(they do **not** appear in the plain text), so we extract them from
``message.entities`` rather than regex-matching the text.

Extracts:
- Wallet addresses + Nansen labels (e.g. "Former Smart Trader")
- Token addresses + symbols
- USD / ETH values + token amounts
- Transaction hashes

Then:
1. Saves wallet addresses + labels to data/nansen_wallets.md
2. Records buy in wallet_swaps table (dedup by tx_hash)
3. Adds wallet to smart_wallets table for Stage 4 scoring
4. Checks conviction (token already in our DB)
5. Optionally forwards alert + fires conviction alert

NansenBot message format (example):
    🚨 Smart Alert: Smart Money Large Token Purchases
    🟢 [Former Smart Trader](profiler_url) bought 294M #GITC ($406.42)
        for 0.2 #ETH @ $0.0000014
    #Base | Txn (...) | Profiler (...) | Token (...) | ...
    (URLs are in TextUrl entities, NOT in the raw text)
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import SmartWallet, Token, WalletSwap

if TYPE_CHECKING:
    from .config import NansenConfig

logger = structlog.get_logger(__name__)

# The real @NansenBot Telegram user ID (not @NansenAIBot which is a squatter)
NANSEN_BOT_ID = 1593218799

# ── Regex patterns ────────────────────────────────────────────

# -- Entity-URL extraction (primary — URLs live in message entities) --
RE_URL_ADDRESS = re.compile(r"address=(0x[0-9a-fA-F]{40})", re.IGNORECASE)
RE_URL_TOKEN = re.compile(r"tokenAddress=(0x[0-9a-fA-F]{40})", re.IGNORECASE)
RE_URL_TX = re.compile(r"/tx/(0x[0-9a-fA-F]{64})", re.IGNORECASE)
RE_URL_CHAIN = re.compile(r"chain=(\w+)", re.IGNORECASE)

# -- Text-based extraction (fallback / supplement) --

# Wallet label from markdown: 🟢 [Label]( or 🟢 Label
RE_LABEL = re.compile(r"🟢\s+\[?([^\]\(]+?)[\]\(]", re.UNICODE)
RE_LABEL_FALLBACK = re.compile(r"🟢\s+(.+?)\s+bought", re.UNICODE)

# Token symbol from hashtag   e.g. "bought 294M #GITC"
RE_TOKEN_SYMBOL = re.compile(
    r"bought\s+[\d,.]+[KMBkmb]?\s+#(\w+)",
    re.IGNORECASE,
)

# USD value   e.g. "($406.42)"  or "($1,234.56)"
RE_USD_VALUE = re.compile(r"\(\$([\d,]+\.?\d*)\)")

# ETH value   e.g. "0.2 #ETH"
RE_ETH_VALUE = re.compile(r"([\d.]+)\s*#ETH", re.IGNORECASE)

# Token amount   e.g. "bought 294M #"
RE_TOKEN_AMOUNT = re.compile(r"bought\s+([\d,.]+)([KMBkmb])?\s+#")

# Contract address from "CA: 0x..."
RE_CA = re.compile(r"CA:\s*(0x[0-9a-fA-F]{40})", re.IGNORECASE)

# Chain tag  #Base #Ethereum etc.
RE_CHAIN = re.compile(r"#(Base|Ethereum|Polygon|Arbitrum|Optimism|Solana)", re.IGNORECASE)

# General EVM address (0x + 40 hex chars)
RE_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")

# Legacy text-based URL patterns (kept for manual-paste fallback)
RE_PROFILER_URL = re.compile(
    r"https://app\.nansen\.ai/profiler\?address=(0x[0-9a-fA-F]{40})",
    re.IGNORECASE,
)
RE_TX_HASH_URL = re.compile(
    r"https://app\.nansen\.ai/tx/(0x[0-9a-fA-F]{64})",
    re.IGNORECASE,
)
RE_TOKEN_URL = re.compile(
    r"https://app\.nansen\.ai/token-god-mode\?tokenAddress=(0x[0-9a-fA-F]{40})",
    re.IGNORECASE,
)


class NansenSignal:
    """Parsed data from a single NansenBot message."""

    def __init__(self) -> None:
        self.wallet_address: str = ""
        self.wallet_label: str = ""
        self.token_address: str = ""
        self.token_symbol: str = ""
        self.usd_value: float = 0.0
        self.eth_value: float = 0.0
        self.token_amount: float = 0.0
        self.tx_hash: str = ""
        self.chain: str = ""
        self.raw_text: str = ""
        self.parsed_at: datetime = datetime.now(timezone.utc)

    @property
    def is_base(self) -> bool:
        return self.chain.lower() == "base"

    @property
    def is_valid(self) -> bool:
        return bool(self.wallet_address and self.token_address)

    def to_dict(self) -> dict:
        return {
            "wallet_address": self.wallet_address,
            "wallet_label": self.wallet_label,
            "token_address": self.token_address,
            "token_symbol": self.token_symbol,
            "usd_value": self.usd_value,
            "eth_value": self.eth_value,
            "token_amount": self.token_amount,
            "tx_hash": self.tx_hash,
            "chain": self.chain,
            "source": "nansen",
        }


# ── Entity-aware parser (primary — for real Telethon messages) ──


def _extract_entity_urls(message) -> list[str]:
    """Extract all URLs from Telegram message entities.

    NansenBot embeds profiler/token/tx links as TextUrl entities.
    They do NOT appear in ``message.text``.
    """
    urls: list[str] = []
    entities = getattr(message, "entities", None)
    if not entities:
        return urls
    for ent in entities:
        url = getattr(ent, "url", None)
        if url:
            urls.append(url)
    return urls


def parse_nansen_entity_message(message) -> Optional[NansenSignal]:
    """Parse a Telethon Message object using entity URLs + text.

    This is the primary parser used by the live listener.
    Returns NansenSignal if valid Base chain smart alert, else None.
    """
    text = getattr(message, "text", "") or ""
    if not text:
        return None
    if "Smart Alert" not in text and "nansen.ai" not in text:
        return None

    urls = _extract_entity_urls(message)
    return _parse_from_text_and_urls(text, urls)


def parse_nansen_message(text: str) -> Optional[NansenSignal]:
    """Parse a NansenBot message from **plain text** (manual paste fallback).

    When a user pastes a NansenBot message into the BaseSleuth chat,
    the URLs may or may not be present in the text.  This tries both
    entity-URL regex AND legacy full-URL regex.

    Returns NansenSignal if valid Base chain smart alert, else None.
    """
    if not text:
        return None
    if "Smart Alert" not in text and "nansen.ai" not in text:
        return None

    # Try to extract URLs from text itself (manual paste may include them)
    urls: list[str] = re.findall(r"https?://[^\s\)]+", text)
    sig = _parse_from_text_and_urls(text, urls)
    if sig:
        return sig

    # Fallback: legacy regex on full text (for older paste formats)
    return _parse_legacy_text(text)


def _parse_from_text_and_urls(text: str, urls: list[str]) -> Optional[NansenSignal]:
    """Core parser that uses text + a list of URLs (from entities or text)."""
    sig = NansenSignal()
    sig.raw_text = text

    # ── Extract from entity URLs ────────────────────────
    chain_from_url = ""
    for url in urls:
        if "profiler" in url and not sig.wallet_address:
            m = RE_URL_ADDRESS.search(url)
            if m:
                sig.wallet_address = m.group(1).lower()
            cm = RE_URL_CHAIN.search(url)
            if cm:
                chain_from_url = cm.group(1)

        if "token-god-mode" in url and not sig.token_address:
            m = RE_URL_TOKEN.search(url)
            if m:
                sig.token_address = m.group(1).lower()

        if "/tx/" in url and not sig.tx_hash:
            m = RE_URL_TX.search(url)
            if m:
                sig.tx_hash = m.group(1).lower()

    # ── Extract from text ───────────────────────────────
    # Chain
    chain_match = RE_CHAIN.search(text)
    sig.chain = chain_from_url or (chain_match.group(1) if chain_match else "")

    if sig.chain.lower() != "base":
        return None

    # Wallet label
    label_match = RE_LABEL.search(text)
    if not label_match:
        label_match = RE_LABEL_FALLBACK.search(text)
    if label_match:
        sig.wallet_label = label_match.group(1).strip().lstrip("👤🟢 ").strip()

    # Token address fallback from CA: line
    if not sig.token_address:
        ca_match = RE_CA.search(text)
        if ca_match:
            sig.token_address = ca_match.group(1).lower()

    # Token symbol
    sym_match = RE_TOKEN_SYMBOL.search(text)
    if sym_match:
        sig.token_symbol = sym_match.group(1)

    # USD value
    usd_match = RE_USD_VALUE.search(text)
    if usd_match:
        try:
            sig.usd_value = float(usd_match.group(1).replace(",", ""))
        except ValueError:
            pass

    # ETH value
    eth_match = RE_ETH_VALUE.search(text)
    if eth_match:
        try:
            sig.eth_value = float(eth_match.group(1))
        except ValueError:
            pass

    # Token amount
    amount_match = RE_TOKEN_AMOUNT.search(text)
    if amount_match:
        try:
            raw = float(amount_match.group(1).replace(",", ""))
            multiplier = {"k": 1e3, "m": 1e6, "b": 1e9}.get(
                (amount_match.group(2) or "").lower(), 1,
            )
            sig.token_amount = raw * multiplier
        except ValueError:
            pass

    if not sig.is_valid:
        logger.debug(
            "nansen.parse_incomplete",
            has_wallet=bool(sig.wallet_address),
            has_token=bool(sig.token_address),
        )
        return None

    logger.info(
        "nansen.parsed",
        wallet=sig.wallet_label or sig.wallet_address[:10],
        token=sig.token_symbol,
        usd=sig.usd_value,
        chain=sig.chain,
    )
    return sig


def _parse_legacy_text(text: str) -> Optional[NansenSignal]:
    """Fallback parser using regex on full text (for manual paste with URLs in text)."""
    sig = NansenSignal()
    sig.raw_text = text

    chain_match = RE_CHAIN.search(text)
    sig.chain = chain_match.group(1) if chain_match else ""
    if sig.chain.lower() != "base":
        return None

    profiler_match = RE_PROFILER_URL.search(text)
    if profiler_match:
        sig.wallet_address = profiler_match.group(1).lower()

    label_match = RE_LABEL.search(text)
    if not label_match:
        label_match = RE_LABEL_FALLBACK.search(text)
    if label_match:
        sig.wallet_label = label_match.group(1).strip()

    ca_match = RE_CA.search(text)
    if ca_match:
        sig.token_address = ca_match.group(1).lower()
    else:
        token_url = RE_TOKEN_URL.search(text)
        if token_url:
            sig.token_address = token_url.group(1).lower()

    sym_match = RE_TOKEN_SYMBOL.search(text)
    if sym_match:
        sig.token_symbol = sym_match.group(1)

    usd_match = RE_USD_VALUE.search(text)
    if usd_match:
        try:
            sig.usd_value = float(usd_match.group(1).replace(",", ""))
        except ValueError:
            pass

    eth_match = RE_ETH_VALUE.search(text)
    if eth_match:
        try:
            sig.eth_value = float(eth_match.group(1))
        except ValueError:
            pass

    amount_match = RE_TOKEN_AMOUNT.search(text)
    if amount_match:
        try:
            raw = float(amount_match.group(1).replace(",", ""))
            multiplier = {"k": 1e3, "m": 1e6, "b": 1e9}.get(
                (amount_match.group(2) or "").lower(), 1,
            )
            sig.token_amount = raw * multiplier
        except ValueError:
            pass

    tx_match = RE_TX_HASH_URL.search(text)
    if tx_match:
        sig.tx_hash = tx_match.group(1).lower()

    if not sig.is_valid:
        return None

    logger.info(
        "nansen.parsed",
        wallet=sig.wallet_label or sig.wallet_address[:10],
        token=sig.token_symbol,
        usd=sig.usd_value,
        chain=sig.chain,
    )
    return sig


# ── All-address extractor for batch parsing ─────────────────

def extract_base_wallets(text: str) -> list[tuple[str, str]]:
    """Extract all EVM wallet addresses + nearby labels from text.

    Returns list of (address, label) tuples.
    Designed for batch extraction from NansenBot messages.
    """
    wallets: list[tuple[str, str]] = []
    seen: set[str] = set()

    # Method 1: Extract from profiler URLs with labels
    for match in RE_PROFILER_URL.finditer(text):
        addr = match.group(1).lower()
        if addr in seen:
            continue
        seen.add(addr)

        # Try to find label before this URL
        before = text[:match.start()]
        label = ""
        label_match = re.search(r"🟢\s+(.+?)\s*$", before)
        if label_match:
            label = label_match.group(1).strip()
        wallets.append((addr, label))

    # Method 2: Find any remaining 0x addresses not yet captured
    for match in RE_EVM_ADDRESS.finditer(text):
        addr = match.group(0).lower()
        if addr in seen:
            continue
        # Skip common non-wallet addresses (token contracts, etc.)
        # Only grab from profiler-like context
        seen.add(addr)

    return wallets


# ── Persistence layer — save wallets + record swaps ──────────

class NansenIngestor:
    """Processes parsed Nansen signals into the Base Sleuth pipeline.

    - Upserts wallet into smart_wallets with Nansen label
    - Records swap in wallet_swaps (dedup by tx_hash)
    - Checks conviction (token exists in our DB)
    - Appends wallet to data/nansen_wallets.md + data/smart_money_wallets.txt
    """

    def __init__(self, session_factory, nansen_md_path: str = "data/nansen_wallets.md") -> None:
        self._session_factory = session_factory
        self._nansen_md_path = Path(nansen_md_path)
        self._wallet_file_path = Path("data/smart_money_wallets.txt")
        self._known_wallets: set[str] = set()
        self._load_known_wallets()

    def _load_known_wallets(self) -> None:
        """Load already-known wallets from nansen MD + wallet file to avoid duplicates."""
        for path in (self._nansen_md_path, self._wallet_file_path):
            if not path.exists():
                continue
            text = path.read_text()
            for match in RE_EVM_ADDRESS.finditer(text):
                self._known_wallets.add(match.group(0).lower())

    async def ingest(self, signal: NansenSignal) -> dict:
        """Process a Nansen signal through the pipeline.

        Returns dict with keys:
          wallet_new: bool — whether wallet was newly added
          swap_new: bool — whether swap was newly recorded
          conviction: dict|None — conviction info if token in our DB
        """
        result = {"wallet_new": False, "swap_new": False, "conviction": None}

        async with self._session_factory() as session:
            # 1. Upsert wallet
            wallet_new = await self._upsert_wallet(session, signal)
            result["wallet_new"] = wallet_new

            # 2. Record swap
            swap_new = await self._record_swap(session, signal)
            result["swap_new"] = swap_new

            # 3. Check conviction
            conviction = await self._check_conviction(session, signal)
            result["conviction"] = conviction

            await session.commit()

        # 4. Append to nansen_wallets.md + smart_money_wallets.txt
        if wallet_new:
            self._append_nansen_md(signal)
            self._append_wallet_file(signal)

        return result

    async def _upsert_wallet(
        self, session: AsyncSession, signal: NansenSignal,
    ) -> bool:
        """Add or update wallet in smart_wallets table.

        Returns True if wallet was newly inserted.
        """
        addr = signal.wallet_address.lower()

        existing = (
            await session.execute(
                select(SmartWallet).where(SmartWallet.address == addr)
            )
        ).scalar_one_or_none()

        if existing:
            # Update label if Nansen provides a better one
            if signal.wallet_label and not existing.arkham_label:
                existing.arkham_label = signal.wallet_label
            if existing.tag != "nansen":
                existing.tag = "nansen"
            existing.last_activity_at = datetime.now(timezone.utc)
            return False

        wallet = SmartWallet(
            address=addr,
            chain="base",
            tag="nansen",
            arkham_entity=None,
            arkham_label=signal.wallet_label or None,
            tier=1,  # Nansen-flagged wallets are Tier 1 (Nansen already curated)
            is_active=True,
            profitable_periods=0,
            last_synced_at=datetime.now(timezone.utc),
            last_activity_at=datetime.now(timezone.utc),
        )
        session.add(wallet)
        logger.info(
            "nansen.wallet_added",
            address=addr[:10],
            label=signal.wallet_label,
        )
        return True

    async def _record_swap(
        self, session: AsyncSession, signal: NansenSignal,
    ) -> bool:
        """Record the buy in wallet_swaps. Returns True if new."""
        if not signal.tx_hash:
            return False

        existing = (
            await session.execute(
                select(WalletSwap.id)
                .where(WalletSwap.tx_hash == signal.tx_hash)
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing:
            return False

        swap = WalletSwap(
            wallet_address=signal.wallet_address,
            token_address=signal.token_address,
            token_symbol=signal.token_symbol or None,
            token_name=None,
            action="buy",
            usd_value=signal.usd_value if signal.usd_value > 0 else None,
            tx_hash=signal.tx_hash,
            block_timestamp=signal.parsed_at,
        )
        session.add(swap)
        return True

    async def _check_conviction(
        self, session: AsyncSession, signal: NansenSignal,
    ) -> Optional[dict]:
        """Check if the bought token is already in our DB.

        Returns conviction dict if found, else None.
        """
        token = (
            await session.execute(
                select(Token)
                .where(Token.contract_address == signal.token_address.lower())
                .limit(1)
            )
        ).scalar_one_or_none()

        if not token:
            return None

        wallet = (
            await session.execute(
                select(SmartWallet)
                .where(SmartWallet.address == signal.wallet_address.lower())
                .limit(1)
            )
        ).scalar_one_or_none()

        return {
            "token": token,
            "wallet": wallet,
            "buy": signal.to_dict(),
            "is_alerted": token.alert_sent,
        }

    def _append_nansen_md(self, signal: NansenSignal) -> None:
        """Append new wallet to data/nansen_wallets.md table."""
        addr = signal.wallet_address.lower()
        if addr in self._known_wallets:
            return

        self._known_wallets.add(addr)

        if not self._nansen_md_path.exists():
            return

        text = self._nansen_md_path.read_text()
        if addr in text:
            return  # Already there

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = (
            f"| `{addr}` | {signal.wallet_label or 'Unknown'} "
            f"| {date_str} | Auto-added from NansenBot |\n"
        )

        # Insert before the --- separator at the end
        marker = "\n---"
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx] + entry + text[idx:]
        else:
            # Fallback: append at end
            text += entry

        self._nansen_md_path.write_text(text)
        logger.info(
            "nansen.md_updated",
            wallet=addr[:10],
            label=signal.wallet_label,
        )

    def _append_wallet_file(self, signal: NansenSignal) -> None:
        """Append new wallet to data/smart_money_wallets.txt."""
        addr = signal.wallet_address.lower()
        if not self._wallet_file_path.exists():
            return

        existing = self._wallet_file_path.read_text()
        if addr in existing:
            return

        label = signal.wallet_label or "nansen"
        with open(self._wallet_file_path, "a") as f:
            f.write(f"{addr} {label}\n")

        logger.info("nansen.wallet_file_updated", wallet=addr[:10])


# ══════════════════════════════════════════════════════════════
# Telethon live listener — watches @NansenBot in real-time
# ══════════════════════════════════════════════════════════════


class NansenTelethonListener:
    """Actively listens to @NansenBot messages via Telethon (client API).

    The python-telegram-bot (bot API) can only receive messages sent
    TO the bot.  NansenBot posts in its own chat — we need a Telegram
    *user* session to subscribe to new messages.

    Usage (in main.py)::

        listener = NansenTelethonListener(cfg.nansen, session_factory)
        await listener.start()          # connects + starts event loop
        # ... later on shutdown ...
        await listener.stop()

    Requires ``telethon`` (``pip install telethon``).
    """

    def __init__(
        self,
        nansen_cfg: "NansenConfig",
        session_factory,
        *,
        filter_obj=None,
        wallet_monitor=None,
        notifier=None,
    ) -> None:
        self._cfg = nansen_cfg
        self._session_factory = session_factory
        self._filter = filter_obj
        self._wallet_monitor = wallet_monitor
        self._notifier = notifier

        self._client = None  # TelegramClient — set in start()
        self._ingestor: NansenIngestor | None = None
        self._running = False
        self._stats = {"received": 0, "parsed": 0, "ingested": 0, "errors": 0}

    # ── lifecycle ────────────────────────────────────────

    async def start(self) -> None:
        """Connect to Telegram and register the NansenBot event handler."""
        api_id = self._cfg.api_id
        api_hash = self._cfg.api_hash
        if not api_id or not api_hash:
            logger.warning(
                "nansen_telethon.disabled",
                reason="api_id or api_hash not configured",
            )
            return

        try:
            from telethon import TelegramClient, events
        except ImportError:
            logger.error(
                "nansen_telethon.disabled",
                reason="telethon not installed — pip install telethon",
            )
            return

        session_path = self._cfg.session_path
        self._client = TelegramClient(session_path, api_id, api_hash)

        await self._client.start()
        me = await self._client.get_me()
        logger.info(
            "nansen_telethon.connected",
            user=me.first_name,
            user_id=me.id,
        )

        # Verify we can reach @NansenBot
        try:
            entity = await self._client.get_entity(NANSEN_BOT_ID)
            logger.info(
                "nansen_telethon.bot_found",
                username=entity.username,
                bot_id=entity.id,
            )
        except Exception as exc:
            logger.error(
                "nansen_telethon.bot_not_found",
                error=str(exc),
                hint="Make sure the Telegram account has a chat with @NansenBot",
            )
            await self._client.disconnect()
            return

        # Initialise ingestor
        self._ingestor = NansenIngestor(self._session_factory)

        # Register event handler — only messages from NansenBot
        @self._client.on(events.NewMessage(from_users=NANSEN_BOT_ID))
        async def _on_nansen_message(event):
            await self._handle_message(event.message)

        self._running = True
        logger.info("nansen_telethon.listening", bot_id=NANSEN_BOT_ID)

    async def stop(self) -> None:
        """Disconnect from Telegram."""
        self._running = False
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            logger.info(
                "nansen_telethon.stopped",
                stats=self._stats,
            )

    async def run_until_disconnected(self) -> None:
        """Block until the client disconnects (for use in asyncio.gather)."""
        if self._client and self._running:
            await self._client.run_until_disconnected()

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    # ── message handler ──────────────────────────────────

    async def _handle_message(self, message) -> None:
        """Process a single NansenBot message."""
        self._stats["received"] += 1
        try:
            signal = parse_nansen_entity_message(message)
            if not signal:
                return

            # Base-only filter
            if self._cfg.base_only and not signal.is_base:
                return

            self._stats["parsed"] += 1

            # Ingest through the pipeline
            result = await self._ingestor.ingest(signal)
            self._stats["ingested"] += 1

            # Enrich filter's smart wallet set live
            if result["wallet_new"]:
                if self._filter:
                    self._filter._smart_wallets.add(signal.wallet_address.lower())
                if self._wallet_monitor:
                    self._wallet_monitor._wallet_addresses.add(signal.wallet_address.lower())
                    self._wallet_monitor._alias_map[signal.wallet_address.lower()] = (
                        signal.wallet_label
                    )

            # Forward as alert (only if configured)
            if self._cfg.forward_alerts and self._notifier:
                await self._notifier.notify_nansen_signal(signal.to_dict())

            # Conviction alert
            if result["conviction"] and self._notifier:
                await self._notifier.notify_conviction(result["conviction"])

            logger.info(
                "nansen_telethon.signal",
                wallet=signal.wallet_label or signal.wallet_address[:10],
                token=signal.token_symbol,
                usd=signal.usd_value,
                new_wallet=result["wallet_new"],
                conviction=result["conviction"] is not None,
            )

        except Exception:
            self._stats["errors"] += 1
            logger.exception("nansen_telethon.handler_error")
