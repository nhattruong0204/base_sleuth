"""Binance Skills Hub API client for Base chain intelligence.

Integrates multiple Binance Web3 Skills:
1. Unified Token Rank  — trending / top-search tokens on Base (chain 8453)
2. Social Hype         — social buzz leaderboard with sentiment
3. Token Security Audit — honeypot / scam / rug detection
4. Token Dynamic Data  — rich market data with KOL / smart money holder %
5. Token Search        — cross-chain token lookup by keyword / address
6. Wallet Balance      — on-chain wallet token positions

All endpoints are public (no API key needed), rate-limited only by
web3.binance.com infra.  We add Accept-Encoding: identity per the
official SKILL.md docs and keep a polite request pace.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import AppConfig, BinanceSkillsConfig
from .models import Token

logger = structlog.get_logger(__name__)

# Base chain ID in Binance's system
BASE_CHAIN_ID = "8453"

_HEADERS_GET = {"Accept-Encoding": "identity", "User-Agent": "BaseSleuth/1.0"}
_HEADERS_POST = {
    "Content-Type": "application/json",
    "Accept-Encoding": "identity",
    "User-Agent": "BaseSleuth/1.0",
}


class BinanceSkillsClient:
    """Fetches data from Binance Skills Hub public APIs."""

    def __init__(self, config: AppConfig, http: httpx.AsyncClient) -> None:
        self.cfg: BinanceSkillsConfig = config.binance_skills
        self._http = http
        # Cooldown tracking for trending scanner
        self._last_seen_trending: set[str] = set()
        self._last_scan_time: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    # ------------------------------------------------------------------
    # 1. Unified Token Rank — trending / top-search on Base
    # ------------------------------------------------------------------

    async def fetch_trending_tokens(
        self,
        rank_type: int = 10,
        period: int = 50,
        size: int = 50,
    ) -> list[dict]:
        """Fetch trending (10) or top-search (11) tokens on Base.

        rank_type: 10=Trending, 11=TopSearch, 20=Alpha
        period: 10=1m, 20=5m, 30=1h, 40=4h, 50=24h
        """
        url = (
            "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/"
            "buw/wallet/market/token/pulse/unified/rank/list"
        )
        body = {
            "rankType": rank_type,
            "chainId": BASE_CHAIN_ID,
            "period": period,
            "sortBy": 70,  # Sort by volume
            "orderAsc": False,
            "page": 1,
            "size": size,
            # Focus on gems: reasonable mcap range
            "marketCapMin": self.cfg.trending_min_mcap,
            "marketCapMax": self.cfg.trending_max_mcap,
            "liquidityMin": self.cfg.trending_min_liquidity,
        }
        try:
            resp = await self._http.post(url, json=body, headers=_HEADERS_POST, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("success") and data.get("data"):
                tokens = data["data"].get("tokens") or []
                logger.info(
                    "binance.trending.fetched",
                    rank_type=rank_type,
                    count=len(tokens),
                )
                return tokens
        except Exception as exc:
            logger.warning("binance.trending.failed", error=str(exc))
        return []

    # ------------------------------------------------------------------
    # 2. Social Hype Leaderboard — sentiment-ranked tokens on Base
    # ------------------------------------------------------------------

    async def fetch_social_hype(self) -> list[dict]:
        """Fetch social hype leaderboard for Base tokens.

        Returns tokens ranked by social buzz with sentiment analysis
        and AI-generated social summaries.
        """
        url = (
            "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/"
            "buw/wallet/market/token/pulse/social/hype/rank/leaderboard"
        )
        params = {
            "chainId": BASE_CHAIN_ID,
            "sentiment": "All",
            "socialLanguage": "ALL",
            "targetLanguage": "en",
            "timeRange": 1,  # 24 hours
        }
        try:
            resp = await self._http.get(url, params=params, headers=_HEADERS_GET, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("success") and data.get("data"):
                leaders = data["data"].get("leaderBoardList") or []
                logger.info("binance.social_hype.fetched", count=len(leaders))
                return leaders
        except Exception as exc:
            logger.warning("binance.social_hype.failed", error=str(exc))
        return []

    # ------------------------------------------------------------------
    # 3. Token Security Audit — honeypot / scam detection
    # ------------------------------------------------------------------

    async def audit_token(self, contract_address: str) -> Optional[dict]:
        """Run a security audit on a Base token.

        Returns audit result dict with keys:
            risk_level (0-5), risk_level_enum (LOW/MEDIUM/HIGH/BLOCKED),
            buy_tax, sell_tax, is_verified, risk_items [{title, isHit, riskType}]
        or None on failure.
        """
        url = (
            "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/"
            "security/token/audit"
        )
        body = {
            "binanceChainId": BASE_CHAIN_ID,
            "contractAddress": contract_address,
            "requestId": str(uuid.uuid4()),
        }
        headers = {**_HEADERS_POST, "source": "agent"}
        try:
            resp = await self._http.post(url, json=body, headers=headers, timeout=15)
            resp.raise_for_status()
            raw = resp.json()
            if not raw.get("success") or not raw.get("data"):
                return None

            d = raw["data"]
            if not d.get("hasResult") or not d.get("isSupported"):
                return None

            extra = d.get("extraInfo") or {}
            risk_items = []
            for cat in d.get("riskItems") or []:
                for detail in cat.get("details") or []:
                    if detail.get("isHit"):
                        risk_items.append({
                            "category": cat.get("id", ""),
                            "title": detail.get("title", ""),
                            "description": detail.get("description", ""),
                            "risk_type": detail.get("riskType", ""),
                        })

            result = {
                "risk_level": d.get("riskLevel", -1),
                "risk_level_enum": d.get("riskLevelEnum", "UNKNOWN"),
                "buy_tax": _safe_float(extra.get("buyTax")),
                "sell_tax": _safe_float(extra.get("sellTax")),
                "is_verified": extra.get("isVerified", False),
                "risk_items": risk_items,
                "risk_count": len(risk_items),
            }
            logger.info(
                "binance.audit.ok",
                addr=contract_address[:12],
                risk=result["risk_level_enum"],
                hits=len(risk_items),
            )
            return result
        except Exception as exc:
            logger.warning(
                "binance.audit.failed",
                addr=contract_address[:12],
                error=str(exc),
            )
            return None

    # ------------------------------------------------------------------
    # 4. Token Dynamic Data — rich market data + smart money holders
    # ------------------------------------------------------------------

    async def fetch_token_dynamic(self, contract_address: str) -> Optional[dict]:
        """Fetch rich dynamic market data for a Base token.

        Returns data including KOL holders, smart money %, pro holders,
        holder distribution, and multi-timeframe volume/price data.
        """
        url = (
            "https://web3.binance.com/bapi/defi/v4/public/wallet-direct/"
            "buw/wallet/market/token/dynamic/info"
        )
        params = {
            "chainId": BASE_CHAIN_ID,
            "contractAddress": contract_address,
        }
        try:
            resp = await self._http.get(url, params=params, headers=_HEADERS_GET, timeout=15)
            resp.raise_for_status()
            raw = resp.json()
            if not raw.get("success") or not raw.get("data"):
                return None

            d = raw["data"]
            result = {
                "price": _safe_float(d.get("price")),
                "market_cap": _safe_float(d.get("marketCap")),
                "fdv": _safe_float(d.get("fdv")),
                "liquidity": _safe_float(d.get("liquidity")),
                "holders": _safe_int(d.get("holders")),
                "top10_pct": _safe_float(d.get("top10HoldersPercentage")),
                "kyc_holders": _safe_int(d.get("kycHolderCount")),
                "kol_holders": _safe_int(d.get("kolHolders")),
                "kol_pct": _safe_float(d.get("kolHoldingPercent")),
                "pro_holders": _safe_int(d.get("proHolders")),
                "pro_pct": _safe_float(d.get("proHoldingPercent")),
                "smart_money_holders": _safe_int(d.get("smartMoneyHolders")),
                "smart_money_pct": _safe_float(d.get("smartMoneyHoldingPercent")),
                "dev_pct": _safe_float(d.get("devHoldingPercent")),
                # Multi-timeframe data
                "volume_5m": _safe_float(d.get("volume5m")),
                "volume_1h": _safe_float(d.get("volume1h")),
                "volume_4h": _safe_float(d.get("volume4h")),
                "volume_24h": _safe_float(d.get("volume24h")),
                "pct_change_5m": _safe_float(d.get("percentChange5m")),
                "pct_change_1h": _safe_float(d.get("percentChange1h")),
                "pct_change_4h": _safe_float(d.get("percentChange4h")),
                "pct_change_24h": _safe_float(d.get("percentChange24h")),
                "count_24h": _safe_int(d.get("count24h")),
                "count_24h_buy": _safe_int(d.get("count24hBuy")),
                "count_24h_sell": _safe_int(d.get("count24hSell")),
            }
            logger.info(
                "binance.dynamic.ok",
                addr=contract_address[:12],
                kol=result["kol_holders"],
                sm=result["smart_money_holders"],
            )
            return result
        except Exception as exc:
            logger.warning(
                "binance.dynamic.failed",
                addr=contract_address[:12],
                error=str(exc),
            )
            return None

    # ------------------------------------------------------------------
    # 5. Token Search — find tokens by keyword / address on Base
    # ------------------------------------------------------------------

    async def search_tokens(
        self, keyword: str, chain_ids: str = BASE_CHAIN_ID,
    ) -> list[dict]:
        """Search for tokens on Base by keyword, symbol, or contract address."""
        url = (
            "https://web3.binance.com/bapi/defi/v5/public/wallet-direct/"
            "buw/wallet/market/token/search"
        )
        params = {
            "keyword": keyword,
            "chainIds": chain_ids,
            "orderBy": "volume24h",
        }
        try:
            resp = await self._http.get(url, params=params, headers=_HEADERS_GET, timeout=15)
            resp.raise_for_status()
            raw = resp.json()
            if raw.get("success") and raw.get("data"):
                tokens = raw["data"]
                logger.info("binance.search.ok", keyword=keyword, count=len(tokens))
                return tokens
        except Exception as exc:
            logger.warning("binance.search.failed", keyword=keyword, error=str(exc))
        return []

    # ------------------------------------------------------------------
    # 6. Wallet Balance — query wallet holdings on Base
    # ------------------------------------------------------------------

    async def fetch_wallet_balance(
        self, address: str,
    ) -> list[dict]:
        """Fetch all token holdings for a wallet on Base."""
        url = (
            "https://web3.binance.com/bapi/defi/v3/public/wallet-direct/"
            "buw/wallet/address/pnl/active-position-list"
        )
        params = {
            "address": address,
            "chainId": BASE_CHAIN_ID,
            "offset": 0,
        }
        headers = {
            **_HEADERS_GET,
            "clienttype": "web",
            "clientversion": "1.2.0",
        }
        try:
            resp = await self._http.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            raw = resp.json()
            if raw.get("success") and raw.get("data"):
                positions = raw["data"].get("list") or []
                logger.info(
                    "binance.wallet.ok",
                    addr=address[:12],
                    tokens=len(positions),
                )
                return positions
        except Exception as exc:
            logger.warning(
                "binance.wallet.failed",
                addr=address[:12],
                error=str(exc),
            )
        return []


class BinanceTrendingScanner:
    """Scanner loop that discovers tokens via Binance trending / top-search.

    Runs periodically to find Base tokens that are:
    - Trending on Binance Web3 (ranked by volume)
    - Top searched on Binance Web3
    - Socially hyped with positive sentiment

    Tokens found here are ingested into the DB for scoring, similar to
    the breakout scanner flow.
    """

    def __init__(
        self,
        config: AppConfig,
        client: BinanceSkillsClient,
        http: httpx.AsyncClient,
    ) -> None:
        self.cfg = config.binance_skills
        self._client = client
        self._http = http
        self._app_config = config
        self._seen_addresses: set[str] = set()
        # Known blue-chip / infrastructure addresses to skip
        self._skip_addresses: set[str] = {
            addr.lower() for addr in config.gainers.skip_addresses
        }

    async def scan(self, session: AsyncSession) -> list[Token]:
        """Run one scan cycle: fetch trending + top-search, ingest new tokens."""
        new_tokens: list[Token] = []

        # Fetch trending (rank_type=10) and top-search (rank_type=11) in parallel
        trending_task = self._client.fetch_trending_tokens(rank_type=10, size=50)
        top_search_task = self._client.fetch_trending_tokens(rank_type=11, size=50)
        social_task = self._client.fetch_social_hype()

        trending, top_search, social_hype = await asyncio.gather(
            trending_task, top_search_task, social_task,
            return_exceptions=True,
        )

        # Merge all discovered addresses
        candidates: dict[str, dict] = {}

        for source_name, result in [
            ("trending", trending),
            ("top_search", top_search),
            ("social_hype", social_hype),
        ]:
            if isinstance(result, Exception):
                logger.warning(
                    "binance.scan.source_failed",
                    source=source_name,
                    error=str(result),
                )
                continue

            for item in result:
                addr = self._extract_address(item, source_name)
                if not addr:
                    continue
                addr_lower = addr.lower()

                # Skip known infrastructure tokens
                if addr_lower in self._skip_addresses:
                    continue

                if addr_lower not in candidates:
                    candidates[addr_lower] = {
                        "address": addr_lower,
                        "sources": [],
                        "raw": item,
                    }
                candidates[addr_lower]["sources"].append(source_name)

        if not candidates:
            return new_tokens

        # Check which addresses are already in DB
        from sqlalchemy import func

        existing_stmt = (
            select(Token.contract_address)
            .where(
                func.lower(Token.contract_address).in_(
                    list(candidates.keys())
                )
            )
        )
        existing_rows = (await session.execute(existing_stmt)).scalars().all()
        existing_addrs = {a.lower() for a in existing_rows}

        for addr_lower, info in candidates.items():
            if addr_lower in existing_addrs:
                continue  # Already tracked
            if addr_lower in self._seen_addresses:
                continue  # Already tried this cycle

            self._seen_addresses.add(addr_lower)

            # Create token record from Binance data
            raw = info["raw"]
            token = self._build_token_from_binance(raw, info["sources"])
            if token:
                session.add(token)
                new_tokens.append(token)

        if new_tokens:
            logger.info(
                "binance.scan.ingested",
                count=len(new_tokens),
                symbols=[t.symbol for t in new_tokens],
                total_candidates=len(candidates),
            )

        return new_tokens

    def _extract_address(self, item: dict, source: str) -> Optional[str]:
        """Extract contract address from different response shapes."""
        if source == "social_hype":
            meta = item.get("metaInfo") or {}
            return meta.get("contractAddress")
        else:
            return item.get("contractAddress")

    def _build_token_from_binance(
        self, raw: dict, sources: list[str],
    ) -> Optional[Token]:
        """Build a Token ORM object from Binance trending/search data."""
        addr = raw.get("contractAddress")
        if not addr:
            # Try social hype shape
            meta = raw.get("metaInfo") or {}
            addr = meta.get("contractAddress")
        if not addr:
            return None

        symbol = raw.get("symbol") or (raw.get("metaInfo") or {}).get("symbol")
        name = raw.get("name") or symbol

        source_tag = "binance_" + "+".join(sources)

        return Token(
            contract_address=addr.lower(),
            chain="base",
            name=name,
            symbol=symbol,
            is_breakout=True,  # Treat as breakout-class (delayed mover)
            discovery_source=source_tag,
            launched_at=None,
            discovered_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None
