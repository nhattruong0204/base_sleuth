"""On-chain wallet monitor using BaseScan API.

Polls BaseScan for ERC-20 token transfers from tracked wallets.
Detects buy signals when a watched wallet receives a new token
(paired with an outgoing WETH/USDC transfer in the same transaction).

Strategy:
1. Check latest ERC-20 transfers for each tracked wallet
2. Incoming token transfer from a DEX router = BUY
3. Record in wallet_swaps table
4. If the token exists in our DB → conviction alert + score boost
5. All buys → Telegram alert with wallet alias + token info

BaseScan free tier: 5 requests/second, no key needed.
With API key: higher limits.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import AppConfig, WalletMonitorConfig
from .models import SmartWallet, Token, WalletSwap

logger = structlog.get_logger(__name__)

# Known Base chain DEX routers (swaps from these = user trades)
DEX_ROUTERS: set[str] = {
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Uniswap Universal Router
    "0x2626664c2603336e57b271c5c0b26f421741e481",  # Uniswap Universal Router v2
    "0x6cb442acf35158d5eda88fe602221b67b400be3e",  # Aerodrome Router
    "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43",  # Aerodrome Router v2
    "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24",  # PancakeSwap Router
    "0x198ef79f1f515f02dfe9e3115ed9fc07a3a63800",  # Clanker v3 bonding
    "0x54018b83f0d7de0f70a52987918d9cac41345427",  # Clanker v2
}


class WalletMonitorEntry:
    """Represents a tracked wallet with its alias and metadata."""

    def __init__(self, address: str, alias: str = "", source: str = "file") -> None:
        self.address = address.lower()
        self.alias = alias
        self.source = source

    def __repr__(self) -> str:
        return f"<Wallet {self.alias or self.address[:10]}>"


class WalletMonitor:
    """Monitors on-chain activity from tracked wallets via BaseScan API.

    Lifecycle:
    1. load_wallets() — parse wallet file + load from DB at startup
    2. poll_transfers() — called periodically from main.py loop
    3. Returns list of detected buys for alert/conviction processing
    """

    def __init__(
        self,
        config: AppConfig,
        http: httpx.AsyncClient,
        session_factory,
    ) -> None:
        self.cfg: WalletMonitorConfig = config.wallet_monitor
        self._http = http
        self._session_factory = session_factory

        # Wallet state
        self._wallets: dict[str, WalletMonitorEntry] = {}  # addr -> entry
        self._wallet_addresses: set[str] = set()

        # Track last seen block per wallet to avoid duplicates
        self._last_block: dict[str, int] = {}

        # Global latest block number (polled from BaseScan)
        self._current_block: int = 0

        # Stablecoins / WETH (outgoing from wallet = buying something)
        self._stable_set: set[str] = {
            addr.lower() for addr in self.cfg.stable_addresses
        }

        # Wallet alias map loaded from wallets.txt
        self._alias_map: dict[str, str] = {}

    @property
    def tracked_wallets(self) -> set[str]:
        return self._wallet_addresses

    @property
    def wallet_aliases(self) -> dict[str, str]:
        return self._alias_map

    # ------------------------------------------------------------------
    # Wallet loading — file + database
    # ------------------------------------------------------------------

    async def load_wallets(self) -> int:
        """Load wallets from the configured file and seed into DB.

        Parses the wallet file for addresses + aliases.
        Upserts each wallet into the smart_wallets table.
        Returns number of wallets loaded.
        """
        # Parse wallet file (supports "address alias" format)
        wallet_path = Path(self.cfg.wallet_file)
        if not wallet_path.exists():
            logger.warning("wallet_monitor.file_missing", path=str(wallet_path))
            return 0

        entries: list[WalletMonitorEntry] = []
        for line in wallet_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Format: "0xAddress" or "0xAddress alias text"
            parts = line.split(maxsplit=1)
            addr = parts[0].lower()
            alias = ""
            if len(parts) > 1:
                # Extract alias from quotes or plain text
                alias = parts[1].strip().strip('"').strip("'")

            if addr.startswith("0x") and len(addr) == 42:
                entries.append(WalletMonitorEntry(addr, alias))

        # Also try to load the extended wallet file (wallets.txt)
        extended_path = Path("wallets.txt")
        if extended_path.exists():
            for line in extended_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(maxsplit=1)
                addr = parts[0].lower()
                alias = parts[1].strip().strip('"').strip("'") if len(parts) > 1 else ""
                if addr.startswith("0x") and len(addr) == 42:
                    # Only add if not already present
                    if addr not in {e.address for e in entries}:
                        entries.append(WalletMonitorEntry(addr, alias, source="wallets.txt"))

        if not entries:
            logger.warning("wallet_monitor.no_wallets_found")
            return 0

        # Upsert into DB
        async with self._session_factory() as session:
            for entry in entries:
                existing = (
                    await session.execute(
                        select(SmartWallet).where(SmartWallet.address == entry.address)
                    )
                ).scalar_one_or_none()

                if existing:
                    # Update alias if provided and missing
                    if entry.alias and not existing.arkham_label:
                        existing.arkham_label = entry.alias
                    if not existing.is_active:
                        existing.is_active = True
                    existing.tag = "manual"
                else:
                    wallet = SmartWallet(
                        address=entry.address,
                        chain="base",
                        tag="manual",
                        arkham_entity=None,
                        arkham_label=entry.alias or None,
                        tier=2,  # Default new wallets to Tier 2
                        is_active=True,
                        profitable_periods=0,
                        last_synced_at=datetime.now(timezone.utc),
                    )
                    session.add(wallet)

                self._wallets[entry.address] = entry
                self._wallet_addresses.add(entry.address)
                self._alias_map[entry.address] = entry.alias

            await session.commit()

        logger.info(
            "wallet_monitor.loaded",
            count=len(self._wallets),
            aliases=sum(1 for e in entries if e.alias),
        )
        return len(self._wallets)

    # ------------------------------------------------------------------
    # On-chain polling — BaseScan ERC-20 transfers
    # ------------------------------------------------------------------

    async def poll_transfers(self) -> list[dict]:
        """Poll BaseScan for recent ERC-20 transfers from tracked wallets.

        For each wallet, fetches incoming token transfers (ERC-20).
        Detects buys by checking if the wallet received tokens from a DEX.

        Returns list of new buy dicts ready for alert processing.
        """
        if not self._wallet_addresses:
            return []

        # Get current block number
        current_block = await self._get_block_number()
        if current_block == 0:
            return []
        self._current_block = current_block

        all_buys: list[dict] = []
        addresses = list(self._wallet_addresses)

        # Process in batches to respect rate limits
        for i in range(0, len(addresses), self.cfg.batch_size):
            batch = addresses[i : i + self.cfg.batch_size]

            for wallet_addr in batch:
                try:
                    buys = await self._check_wallet_transfers(wallet_addr)
                    all_buys.extend(buys)
                except Exception:
                    logger.debug(
                        "wallet_monitor.check_failed",
                        wallet=wallet_addr[:10],
                    )

                # Rate limit between API calls
                await asyncio.sleep(self.cfg.rate_delay_seconds)

        # Record new buys in DB (deduplicate by tx_hash)
        if all_buys:
            recorded = await self._record_buys(all_buys)
            if recorded:
                logger.info(
                    "wallet_monitor.buys_detected",
                    count=len(recorded),
                    wallets=[b["wallet_alias"] or b["wallet_address"][:10] for b in recorded],
                    tokens=[b["token_symbol"] for b in recorded],
                )
            return recorded

        return []

    async def _get_block_number(self) -> int:
        """Get the current Base block number from BaseScan."""
        params: dict = {
            "module": "proxy",
            "action": "eth_blockNumber",
        }
        if self.cfg.basescan_api_key:
            params["apikey"] = self.cfg.basescan_api_key

        try:
            resp = await self._http.get(
                self.cfg.basescan_api_url,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result", "0x0")
            return int(result, 16)
        except Exception:
            logger.debug("wallet_monitor.block_number_failed")
            return 0

    async def _check_wallet_transfers(self, wallet_addr: str) -> list[dict]:
        """Check ERC-20 transfers for a single wallet.

        Uses BaseScan tokentx API to get recent token transfers.
        Looks for incoming transfers from DEX routers (= buys).
        """
        # Start from last known block or lookback
        start_block = self._last_block.get(
            wallet_addr,
            max(0, self._current_block - self.cfg.lookback_blocks),
        )

        params: dict = {
            "module": "account",
            "action": "tokentx",
            "address": wallet_addr,
            "startblock": start_block,
            "endblock": "latest",
            "sort": "desc",
            "page": 1,
            "offset": 50,  # Last 50 transfers
        }
        if self.cfg.basescan_api_key:
            params["apikey"] = self.cfg.basescan_api_key

        try:
            resp = await self._http.get(
                self.cfg.basescan_api_url,
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.debug(
                "wallet_monitor.api_failed",
                wallet=wallet_addr[:10],
                error=str(exc),
            )
            return []

        if data.get("status") != "1" or not data.get("result"):
            return []

        transfers = data["result"]
        if not isinstance(transfers, list):
            return []

        # Update last seen block
        if transfers:
            max_block = max(int(tx.get("blockNumber", 0)) for tx in transfers)
            self._last_block[wallet_addr] = max_block + 1

        buys: list[dict] = []
        seen_hashes: set[str] = set()

        for tx in transfers:
            tx_hash = tx.get("hash", "").lower()
            token_addr = tx.get("contractAddress", "").lower()
            from_addr = tx.get("from", "").lower()
            to_addr = tx.get("to", "").lower()

            # Skip if already processed in this batch
            if tx_hash in seen_hashes:
                continue

            # Skip stablecoins/WETH (these are the payment side)
            if token_addr in self._stable_set:
                continue

            # Incoming token transfer to our wallet = potential buy
            # Must come from a DEX router
            is_incoming = to_addr == wallet_addr
            is_from_dex = from_addr in DEX_ROUTERS

            if not is_incoming or not is_from_dex:
                continue

            seen_hashes.add(tx_hash)

            # Parse token amount and value
            decimals = int(tx.get("tokenDecimal", 18))
            raw_value = int(tx.get("value", 0))
            token_amount = raw_value / (10 ** decimals) if decimals > 0 else raw_value

            token_symbol = tx.get("tokenSymbol", "???")
            token_name = tx.get("tokenName", "")
            block_number = int(tx.get("blockNumber", 0))
            timestamp = int(tx.get("timeStamp", 0))

            buys.append({
                "wallet_address": wallet_addr,
                "wallet_alias": self._alias_map.get(wallet_addr, ""),
                "token_address": token_addr,
                "token_symbol": token_symbol,
                "token_name": token_name,
                "token_amount": token_amount,
                "action": "buy",
                "usd_value": None,  # Will be enriched with DexScreener later
                "tx_hash": tx_hash,
                "block_number": block_number,
                "block_timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else None,
            })

        return buys

    # ------------------------------------------------------------------
    # Buy recording — persist to DB + dedup
    # ------------------------------------------------------------------

    async def _record_buys(self, buys: list[dict]) -> list[dict]:
        """Record new buys in wallet_swaps table, deduplicating by tx_hash.

        Returns only the newly recorded buys (not duplicates).
        """
        new_buys: list[dict] = []

        async with self._session_factory() as session:
            for buy in buys:
                tx_hash = buy.get("tx_hash")
                if not tx_hash:
                    continue

                # Check if already recorded
                existing = (
                    await session.execute(
                        select(WalletSwap.id)
                        .where(WalletSwap.tx_hash == tx_hash)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing:
                    continue

                swap = WalletSwap(
                    wallet_address=buy["wallet_address"],
                    token_address=buy["token_address"],
                    token_symbol=buy["token_symbol"],
                    token_name=buy["token_name"],
                    action="buy",
                    usd_value=buy.get("usd_value"),
                    tx_hash=tx_hash,
                    block_timestamp=buy.get("block_timestamp"),
                )
                session.add(swap)
                new_buys.append(buy)

            if new_buys:
                await session.commit()

        return new_buys

    # ------------------------------------------------------------------
    # Conviction detection — bought token exists in our DB
    # ------------------------------------------------------------------

    async def detect_convictions(self, buys: list[dict]) -> list[dict]:
        """Check if any bought tokens exist in our token database.

        A conviction signal means: a smart money wallet is buying a token
        we already track. This is a strong bullish signal.

        Returns list of conviction dicts with merged token + wallet info.
        """
        if not buys:
            return []

        convictions: list[dict] = []

        async with self._session_factory() as session:
            for buy in buys:
                token_addr = buy["token_address"].lower()

                # Check if token exists in our DB
                token = (
                    await session.execute(
                        select(Token)
                        .where(Token.contract_address == token_addr)
                        .limit(1)
                    )
                ).scalar_one_or_none()

                if not token:
                    continue

                # Get wallet info from DB
                wallet = (
                    await session.execute(
                        select(SmartWallet)
                        .where(SmartWallet.address == buy["wallet_address"])
                        .limit(1)
                    )
                ).scalar_one_or_none()

                convictions.append({
                    "token": token,
                    "wallet": wallet,
                    "buy": buy,
                    "is_alerted": token.alert_sent,
                })

                # Mark swap as conviction-sent
                if buy.get("tx_hash"):
                    swap_record = (
                        await session.execute(
                            select(WalletSwap)
                            .where(WalletSwap.tx_hash == buy["tx_hash"])
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if swap_record:
                        swap_record.conviction_sent = True

            if convictions:
                await session.commit()

        if convictions:
            logger.info(
                "wallet_monitor.convictions",
                count=len(convictions),
                tokens=[c["token"].symbol for c in convictions],
            )

        return convictions

    # ------------------------------------------------------------------
    # Stats for bot commands
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict:
        """Get summary stats for the /wallets bot command."""
        from datetime import timedelta
        from sqlalchemy import func as sqlfunc

        async with self._session_factory() as session:
            total_stmt = select(sqlfunc.count(SmartWallet.id))
            total = (await session.execute(total_stmt)).scalar() or 0

            active_stmt = (
                select(sqlfunc.count(SmartWallet.id))
                .where(SmartWallet.is_active.is_(True))
            )
            active = (await session.execute(active_stmt)).scalar() or 0

            swaps_24h_stmt = (
                select(sqlfunc.count(WalletSwap.id))
                .where(
                    WalletSwap.recorded_at >= datetime.now(timezone.utc) - timedelta(hours=24)
                )
            )
            swaps_24h = (await session.execute(swaps_24h_stmt)).scalar() or 0

            convictions_stmt = (
                select(sqlfunc.count(WalletSwap.id))
                .where(WalletSwap.conviction_sent.is_(True))
                .where(
                    WalletSwap.recorded_at >= datetime.now(timezone.utc) - timedelta(hours=24)
                )
            )
            convictions = (await session.execute(convictions_stmt)).scalar() or 0

            # Recent buys
            recent_stmt = (
                select(WalletSwap)
                .where(WalletSwap.action == "buy")
                .order_by(WalletSwap.recorded_at.desc())
                .limit(10)
            )
            recent_buys = list((await session.execute(recent_stmt)).scalars().all())

        return {
            "total_wallets": total,
            "active_wallets": active,
            "tracked_addresses": len(self._wallet_addresses),
            "swaps_24h": swaps_24h,
            "convictions_24h": convictions,
            "recent_buys": recent_buys,
            "aliases": self._alias_map,
        }
