"""Polls the Clanker public API for new token launches on Base.

Endpoint: GET https://www.clanker.world/api/tokens?sort=desc&pageSize=10
Pagination: cursor-based (NOT page offsets).
Response shape:
{
  "data": [...],
  "total": 431558,
  "cursor": "eyJpZCI6IjIwMjYtMDItMDdUMDM6NTA6NDkifQ==",
  "tokensDeployed": 431558
}

Bankr detection: ~92% of launches have description containing "bankrbot".
Clawnch detection: description contains "CLAWNCH VIA 4CLAW".
Champagne tag (query param champagne=true): only ~96 curated tokens out of 431K+.

Breakout scanner: uses DexScreener trending/boosted/profiles endpoints
to find Base tokens with delayed momentum that the firehose missed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import AppConfig
from .models import Token

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Launch platform detection from Clanker description field
# ---------------------------------------------------------------------------

_BANKR_KEYWORDS = ("bankrbot", "bankr bot", "bankr terminal", "bankr.bot")
_CLAWNCH_KEYWORDS = ("clawnch", "4claw")


def detect_launch_platform(description: str) -> str:
    """Classify a token's launch platform from its Clanker description.

    Returns one of: 'bankr', 'clawnch', 'farcaster', 'direct', 'unknown'.
    """
    desc_lower = (description or "").lower()
    if any(kw in desc_lower for kw in _BANKR_KEYWORDS):
        return "bankr"
    if any(kw in desc_lower for kw in _CLAWNCH_KEYWORDS):
        return "clawnch"
    if "farcaster" in desc_lower or "warpcast" in desc_lower:
        return "farcaster"
    if description and description.strip():
        return "direct"
    return "unknown"


class ClankerClient:
    """Polls the Clanker public API and persists new tokens to the database.

    Two scanning modes:
    1. **Firehose** — streams ALL new tokens via cursor pagination (30s poll).
    2. **Champagne** — periodically checks `champagne=true` for curated gems.
    """

    def __init__(self, config: AppConfig, http: httpx.AsyncClient) -> None:
        self.cfg = config.clanker
        self.bankr_addresses = {
            addr.lower() for addr in config.bankr.deployer_addresses
        }
        self._http = http
        # Cursor-based watermark (base64-encoded JSON from Clanker API)
        self._cursor: Optional[str] = None
        # ID watermark for dedup safety net
        self._watermark: Optional[int] = None

    async def init_watermark(self, session: AsyncSession) -> None:
        """Set watermark from the DB so we don't re-process old tokens."""
        result = await session.execute(
            select(Token.clanker_id)
            .where(Token.clanker_id.isnot(None))
            .order_by(Token.clanker_id.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            self._watermark = row
            self._is_first_run = False
            logger.info("watermark.init", watermark=self._watermark)
        else:
            self._is_first_run = True
            logger.info("watermark.init", watermark="none — first run")

    @property
    def is_first_run(self) -> bool:
        return getattr(self, "_is_first_run", True)

    # ------------------------------------------------------------------
    # Backfill — page backward through Clanker API on first run
    # ------------------------------------------------------------------

    async def backfill(
        self,
        session: AsyncSession,
        hours: int = 12,
        max_pages: int = 200,
    ) -> int:
        """Page backward through Clanker API to catch recent tokens.

        Called once on first startup (empty DB) to fill the gap between
        the bot starting and the firehose's forward-only scanning.

        Returns the total number of tokens backfilled.
        """
        url = f"{self.cfg.base_url}/tokens"
        headers = {}
        if self.cfg.api_key:
            headers["x-api-key"] = self.cfg.api_key

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        total_saved = 0
        cursor = None  # Start from latest page

        logger.info(
            "backfill.starting",
            hours=hours,
            max_pages=max_pages,
            cutoff=cutoff.isoformat(),
        )

        for page_num in range(max_pages):
            params: dict = {"sort": "desc", "pageSize": self.cfg.page_size}
            if cursor:
                params["cursor"] = cursor

            try:
                resp = await self._http.get(
                    url, params=params, headers=headers,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning(
                    "backfill.page_failed",
                    page=page_num,
                    error=str(exc),
                )
                break

            payload = resp.json()
            raw_tokens = payload.get("data", [])
            cursor = payload.get("cursor")

            if not raw_tokens:
                logger.info("backfill.no_more_data", pages_fetched=page_num)
                break

            page_saved = 0
            reached_cutoff = False

            for item in raw_tokens:
                clanker_id = item.get("id")
                if clanker_id is None:
                    continue

                # Check if we've gone past our time cutoff
                raw_ts = item.get("created_at") or item.get("deployed_at")
                if raw_ts:
                    try:
                        ts = datetime.fromisoformat(
                            raw_ts.replace("Z", "+00:00"),
                        )
                        if ts < cutoff:
                            reached_cutoff = True
                            break
                    except (ValueError, AttributeError):
                        pass

                # Dedup via DB
                exists = await session.execute(
                    select(Token.id).where(Token.clanker_id == clanker_id)
                )
                if exists.scalar_one_or_none() is not None:
                    continue

                token = self._parse_token(item)
                session.add(token)
                page_saved += 1

            if page_saved:
                await session.flush()
                total_saved += page_saved

                # Update watermark to the highest ID we've seen
                max_id_result = await session.execute(
                    select(Token.clanker_id)
                    .where(Token.clanker_id.isnot(None))
                    .order_by(Token.clanker_id.desc())
                    .limit(1)
                )
                max_id = max_id_result.scalar_one_or_none()
                if max_id and (self._watermark is None or max_id > self._watermark):
                    self._watermark = max_id

            if reached_cutoff:
                logger.info(
                    "backfill.reached_cutoff",
                    pages_fetched=page_num + 1,
                    tokens_saved=total_saved,
                    cutoff=cutoff.isoformat(),
                )
                break

            if not cursor:
                logger.info("backfill.no_cursor", pages_fetched=page_num + 1)
                break

            # Log progress every 10 pages
            if (page_num + 1) % 10 == 0:
                logger.info(
                    "backfill.progress",
                    pages=page_num + 1,
                    tokens_saved=total_saved,
                )

            # Rate-limit: small delay between pages
            await asyncio.sleep(0.5)

        logger.info(
            "backfill.complete",
            total_tokens=total_saved,
            watermark=self._watermark,
        )
        return total_saved

    # ------------------------------------------------------------------
    # Firehose poll — all new tokens
    # ------------------------------------------------------------------

    async def poll(self, session: AsyncSession) -> list[Token]:
        """Fetch latest tokens via cursor pagination and persist new ones.

        Uses cursor returned by the API (NOT page offsets — those are
        ignored by Clanker and always return the same first page).
        """
        url = f"{self.cfg.base_url}/tokens"
        params: dict = {
            "sort": "desc",
            "pageSize": self.cfg.page_size,
        }
        # On subsequent polls we don't pass cursor — we want the latest
        # tokens and will dedup via watermark.
        headers = {}
        if self.cfg.api_key:
            headers["x-api-key"] = self.cfg.api_key

        try:
            resp = await self._http.get(url, params=params, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("clanker.poll.failed", error=str(exc))
            return []

        payload = resp.json()
        raw_tokens = payload.get("data", [])
        self._cursor = payload.get("cursor")
        if not raw_tokens:
            logger.debug("clanker.poll.empty")
            return []

        new_tokens: list[Token] = []
        for item in raw_tokens:
            clanker_id = item.get("id")
            if clanker_id is None:
                continue
            # Dedup via watermark (fast path)
            if self._watermark is not None and clanker_id <= self._watermark:
                continue
            # Dedup via DB (safety net)
            exists = await session.execute(
                select(Token.id).where(Token.clanker_id == clanker_id)
            )
            if exists.scalar_one_or_none() is not None:
                continue

            token = self._parse_token(item)
            session.add(token)
            new_tokens.append(token)

        if new_tokens:
            await session.flush()
            max_id = max(t.clanker_id for t in new_tokens if t.clanker_id)
            if self._watermark is None or max_id > self._watermark:
                self._watermark = max_id
            logger.info(
                "clanker.poll.new_tokens",
                count=len(new_tokens),
                watermark=self._watermark,
            )
        else:
            logger.debug("clanker.poll.no_new")

        return new_tokens

    # ------------------------------------------------------------------
    # Champagne scan — curated quality tokens only (~96 out of 431K+)
    # ------------------------------------------------------------------

    async def poll_champagne(self, session: AsyncSession) -> list[Token]:
        """Fetch tokens with the champagne tag — Clanker's curated tier.

        These are extremely rare (~0.02% of all tokens) and have much
        higher survival rates.  58% have real DexScreener liquidity.
        """
        url = f"{self.cfg.base_url}/tokens"
        params: dict = {
            "sort": "desc",
            "pageSize": self.cfg.page_size,
            "champagne": "true",
        }
        headers = {}
        if self.cfg.api_key:
            headers["x-api-key"] = self.cfg.api_key

        try:
            resp = await self._http.get(url, params=params, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("clanker.poll_champagne.failed", error=str(exc))
            return []

        payload = resp.json()
        raw_tokens = payload.get("data", [])
        if not raw_tokens:
            return []

        new_tokens: list[Token] = []
        for item in raw_tokens:
            clanker_id = item.get("id")
            if clanker_id is None:
                continue
            # Check if already tracked by clanker_id
            exists = await session.execute(
                select(Token.id).where(Token.clanker_id == clanker_id)
            )
            existing_id = exists.scalar_one_or_none()
            if existing_id is not None:
                # Update champagne flag if it wasn't set
                await session.execute(
                    Token.__table__.update()
                    .where(Token.id == existing_id)
                    .values(is_champagne=True)
                )
                continue

            # Also check by contract_address to avoid IntegrityError
            contract = (item.get("contract_address") or "").lower()
            if contract:
                ca_exists = await session.execute(
                    select(Token.id).where(Token.contract_address == contract)
                )
                ca_existing_id = ca_exists.scalar_one_or_none()
                if ca_existing_id is not None:
                    await session.execute(
                        Token.__table__.update()
                        .where(Token.id == ca_existing_id)
                        .values(is_champagne=True)
                    )
                    continue

            token = self._parse_token(item)
            token.is_champagne = True
            session.add(token)
            new_tokens.append(token)

        if new_tokens:
            await session.flush()
            logger.info(
                "clanker.champagne.new_tokens",
                count=len(new_tokens),
                names=[t.name for t in new_tokens],
            )

        return new_tokens

    # ------------------------------------------------------------------
    # Token parser
    # ------------------------------------------------------------------

    def _parse_token(self, item: dict) -> Token:
        """Map a Clanker API response item to a Token ORM object."""
        contract = (item.get("contract_address") or "").lower()
        requestor = (item.get("requestorAddress") or item.get("msg_sender") or "").lower()
        deployer = (item.get("deployer_address") or requestor).lower()
        description = (item.get("description") or "").strip()

        # Detect launch platform from description text
        platform = detect_launch_platform(description)
        is_bankr = (
            platform == "bankr"
            or requestor in self.bankr_addresses
            or deployer in self.bankr_addresses
        )

        # Parse timestamps
        launched_at = None
        raw_ts = item.get("created_at") or item.get("deployed_at")
        if raw_ts:
            try:
                launched_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        # Social links (new format: [{name, link}] array)
        social_links = item.get("socialLinks") or []
        # Also check metadata.socialMediaUrls as fallback
        if not social_links:
            meta = item.get("metadata") or {}
            meta_urls = meta.get("socialMediaUrls") or []
            social_links = [
                {"name": u.get("platform", "unknown"), "link": u.get("url", "")}
                for u in meta_urls
            ]
        social_json = json.dumps(social_links) if social_links else None

        # Tags
        tags = item.get("tags") or {}

        return Token(
            contract_address=contract,
            chain="base",
            deployer_address=deployer or None,
            pool_address=(item.get("pool_address") or "").lower() or None,
            clanker_id=item.get("id"),
            name=item.get("name"),
            symbol=item.get("symbol"),
            image_url=item.get("img_url"),
            description=description or None,
            requestor_address=requestor or None,
            social_media_urls=social_json,
            is_champagne=bool(tags.get("champagne")),
            is_verified=bool(tags.get("verified")),
            launch_platform=platform,
            is_bankr_launch=is_bankr,
            launched_at=launched_at,
            discovered_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Breakout Scanner — delayed mover detection via DexScreener
# ---------------------------------------------------------------------------

# DexScreener API base URLs (different from the /latest/dex/* endpoints)
_DS_BOOSTS_TOP = "https://api.dexscreener.com/token-boosts/top/v1"
_DS_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
_DS_PROFILES_LATEST = "https://api.dexscreener.com/token-profiles/latest/v1"
_DS_TOKENS_V1 = "https://api.dexscreener.com/tokens/v1"  # /{chainId}/{addresses}


class BreakoutScanner:
    """Detects delayed movers on Base via DexScreener trending signals.

    Catches tokens that:
    - Were launched days ago but are now gaining momentum
    - Are paying for DexScreener boosts (marketing spend = effort)
    - Have custom profiles (branding investment)

    These tokens are invisible to the firehose scanner which only
    watches for NEW launches, but they can be massive winners (e.g.
    $SEVEN launched 5 days ago, now +412% with $85K liquidity).

    Endpoints used:
    - GET /token-boosts/top/v1     → tokens with most active boosts
    - GET /token-boosts/latest/v1  → recently boosted tokens
    - GET /token-profiles/latest/v1 → tokens with custom profiles
    - GET /tokens/v1/base/{addrs}  → batch metrics (chain-specific)
    """

    def __init__(self, config: AppConfig, http: httpx.AsyncClient) -> None:
        self.cfg = config.breakout
        self.dex_cfg = config.dexscreener
        self._http = http
        # Track recently scanned addresses to avoid hammering
        self._recent_scans: dict[str, datetime] = {}

    async def scan(self, session: AsyncSession) -> list[Token]:
        """Run one breakout scan cycle.

        Returns list of new or updated Token objects that qualify as
        breakout candidates (high liquidity + momentum on Base).
        """
        if not self.cfg.enabled:
            return []

        # Step 1: Gather candidate addresses from DexScreener signals
        candidates = await self._gather_candidates()
        if not candidates:
            logger.debug("breakout.no_candidates")
            return []

        logger.info(
            "breakout.candidates_found",
            total=len(candidates),
            sources=list({c["source"] for c in candidates}),
        )

        # Step 2: Filter out recently scanned + already high-scored in DB
        fresh = await self._filter_fresh(candidates, session)
        if not fresh:
            logger.debug("breakout.all_filtered")
            return []

        # Step 3: Batch-fetch full DEX metrics
        addrs = [c["address"] for c in fresh]
        metrics_map = await self._batch_fetch_metrics(addrs)

        # Step 4: Apply breakout quality filters
        qualified: list[dict] = []
        for cand in fresh:
            addr = cand["address"]
            metrics = metrics_map.get(addr)
            if not metrics:
                continue

            pair = metrics  # raw pair data
            liq = (pair.get("liquidity") or {}).get("usd") or 0
            vol24 = (pair.get("volume") or {}).get("h24") or 0
            buys_1h = (pair.get("txns") or {}).get("h1", {}).get("buys", 0)
            pc_1h = (pair.get("priceChange") or {}).get("h1") or 0

            # Age check: skip tokens older than max_age_days
            pair_created = pair.get("pairCreatedAt")
            if pair_created:
                created_dt = datetime.fromtimestamp(
                    pair_created / 1000, tz=timezone.utc,
                )
                age = datetime.now(timezone.utc) - created_dt
                if age > timedelta(days=self.cfg.max_age_days):
                    continue

            # Minimum thresholds for breakout detection
            if liq < self.cfg.min_liquidity_usd:
                continue
            if vol24 < self.cfg.min_volume_24h_usd:
                continue

            # At least one momentum signal required
            has_momentum = (
                buys_1h >= self.cfg.min_buys_1h
                or (isinstance(pc_1h, (int, float)) and pc_1h >= self.cfg.min_price_change_1h_pct)
            )
            if not has_momentum:
                continue

            cand["pair_data"] = pair
            qualified.append(cand)

        if not qualified:
            logger.info("breakout.none_qualified", checked=len(fresh))
            return []

        # Step 5: Persist as Token objects (create or update)
        new_tokens: list[Token] = []
        for cand in qualified:
            token = await self._persist_breakout(cand, session)
            if token:
                new_tokens.append(token)
                self._recent_scans[cand["address"]] = datetime.now(timezone.utc)

        if new_tokens:
            await session.flush()
            logger.info(
                "breakout.new_tokens",
                count=len(new_tokens),
                names=[t.symbol or t.name for t in new_tokens],
            )

        return new_tokens

    # ------------------------------------------------------------------
    # Step 1: Gather candidates from DexScreener signals
    # ------------------------------------------------------------------

    async def _gather_candidates(self) -> list[dict]:
        """Fetch Base tokens from DexScreener boost/profile endpoints.

        Returns list of {address, source, boost_amount, ...} dicts.
        """
        candidates: dict[str, dict] = {}  # addr → candidate dict

        # Fetch all three endpoints concurrently
        results = await asyncio.gather(
            self._fetch_endpoint(_DS_BOOSTS_TOP, "boost_top"),
            self._fetch_endpoint(_DS_BOOSTS_LATEST, "boost_latest"),
            self._fetch_endpoint(_DS_PROFILES_LATEST, "profile"),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                logger.warning("breakout.endpoint_error", error=str(result))
                continue
            for item in result:
                addr = item.get("address", "").lower()
                if addr and addr not in candidates:
                    candidates[addr] = item
                elif addr in candidates:
                    # Merge: token appears in multiple signals = stronger
                    existing = candidates[addr]
                    existing.setdefault("sources", set()).add(item.get("source", ""))
                    existing["boost_total"] = max(
                        existing.get("boost_total", 0),
                        item.get("boost_total", 0),
                    )

        return list(candidates.values())

    async def _fetch_endpoint(
        self, url: str, source_label: str,
    ) -> list[dict]:
        """Fetch a single DexScreener endpoint, filter to Base chain."""
        try:
            resp = await self._http.get(url, timeout=15)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("breakout.fetch_failed", url=url, error=str(exc))
            return []

        data = resp.json()
        # API may return a single object or an array
        if isinstance(data, dict):
            data = [data]
        elif not isinstance(data, list):
            return []

        base_tokens: list[dict] = []
        for item in data:
            chain = (item.get("chainId") or "").lower()
            if chain != "base":
                continue

            addr = (item.get("tokenAddress") or "").lower()
            if not addr:
                continue

            base_tokens.append({
                "address": addr,
                "source": source_label,
                "sources": {source_label},
                "boost_total": item.get("totalAmount") or item.get("amount") or 0,
                "description": item.get("description") or "",
                "links": item.get("links") or [],
            })

        logger.debug(
            "breakout.endpoint_fetched",
            url=url,
            total=len(data) if isinstance(data, list) else 1,
            base_tokens=len(base_tokens),
        )
        return base_tokens

    # ------------------------------------------------------------------
    # Step 2: Filter recently scanned and already known high-scorers
    # ------------------------------------------------------------------

    async def _filter_fresh(
        self, candidates: list[dict], session: AsyncSession,
    ) -> list[dict]:
        """Remove tokens we've recently scanned or that already scored well."""
        cooldown = timedelta(seconds=self.cfg.rescan_cooldown_seconds)
        now = datetime.now(timezone.utc)
        fresh: list[dict] = []

        for cand in candidates:
            addr = cand["address"]

            # Skip if scanned recently
            last_scan = self._recent_scans.get(addr)
            if last_scan and (now - last_scan) < cooldown:
                continue

            # Check if already in DB with a high score
            result = await session.execute(
                select(Token.quality_score, Token.is_breakout)
                .where(Token.contract_address == addr)
            )
            row = result.first()
            if row is not None:
                score, already_breakout = row
                # Skip if already alerted as breakout
                if already_breakout:
                    continue
                # Skip if already scored above threshold (firehose caught it)
                if score is not None and score >= 0.45:
                    continue

            fresh.append(cand)

        logger.debug(
            "breakout.filter_fresh",
            input=len(candidates),
            output=len(fresh),
        )
        return fresh

    # ------------------------------------------------------------------
    # Step 3: Batch-fetch full DEX metrics
    # ------------------------------------------------------------------

    async def _batch_fetch_metrics(
        self, addresses: list[str],
    ) -> dict[str, dict]:
        """Fetch full pair data for Base tokens via DexScreener v1 API.

        Uses: GET /tokens/v1/base/{addr1},{addr2},...
        Returns: dict of addr → best pair data
        """
        result: dict[str, dict] = {}
        batch_size = self.dex_cfg.batch_size

        for i in range(0, len(addresses), batch_size):
            batch = addresses[i: i + batch_size]
            batch_str = ",".join(batch)
            try:
                resp = await self._http.get(
                    f"{_DS_TOKENS_V1}/base/{batch_str}",
                    timeout=15,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning(
                    "breakout.metrics_batch_failed",
                    batch_size=len(batch),
                    error=str(exc),
                )
                continue

            data = resp.json()
            # v1 returns array of pair objects directly
            pairs = data if isinstance(data, list) else data.get("pairs") or []
            for pair in pairs:
                addr = (
                    pair.get("baseToken", {}).get("address", "").lower()
                )
                if not addr:
                    continue
                # Keep pair with highest liquidity
                existing_liq = (
                    result.get(addr, {})
                    .get("liquidity", {})
                    .get("usd", 0)
                    or 0
                )
                new_liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
                if addr not in result or new_liq > existing_liq:
                    result[addr] = pair

            await asyncio.sleep(0.3)  # Rate-limit courtesy

        return result

    # ------------------------------------------------------------------
    # Step 5: Persist breakout token to DB
    # ------------------------------------------------------------------

    async def _persist_breakout(
        self, cand: dict, session: AsyncSession,
    ) -> Optional[Token]:
        """Create or update a Token record for a breakout candidate."""
        addr = cand["address"]
        pair = cand["pair_data"]
        source = cand.get("source", "breakout_boost")

        # Check if token already exists in DB
        result = await session.execute(
            select(Token).where(Token.contract_address == addr)
        )
        existing = result.scalar_one_or_none()

        if existing:
            # Update existing token with breakout flag
            existing.is_breakout = True
            existing.discovery_source = source
            # Reset score so eval loop re-processes it
            existing.quality_score = None
            existing.filter_stage_reached = None
            existing.rejection_reason = None
            existing.alert_sent = False
            logger.info(
                "breakout.updated_existing",
                token=existing.symbol,
                address=addr[:10],
                source=source,
            )
            return existing

        # Create new token from DexScreener pair data
        base_token = pair.get("baseToken", {})
        info = pair.get("info") or {}
        socials = info.get("socials") or []
        websites = info.get("websites") or []

        # Build social links from DexScreener info
        social_links: list[dict] = []
        for s in socials:
            social_links.append({
                "name": s.get("platform", "unknown"),
                "link": s.get("handle", ""),
            })
        for w in websites:
            social_links.append({
                "name": "website",
                "link": w.get("url", ""),
            })
        # Also include links from boost/profile data
        for link in cand.get("links") or []:
            social_links.append({
                "name": link.get("type") or link.get("label") or "link",
                "link": link.get("url", ""),
            })

        social_json = json.dumps(social_links) if social_links else None

        # Parse pair creation time
        launched_at = None
        pair_created = pair.get("pairCreatedAt")
        if pair_created:
            try:
                launched_at = datetime.fromtimestamp(
                    pair_created / 1000, tz=timezone.utc,
                )
            except (ValueError, OSError):
                pass

        token = Token(
            contract_address=addr,
            chain="base",
            name=base_token.get("name"),
            symbol=base_token.get("symbol"),
            image_url=info.get("imageUrl"),
            description=cand.get("description") or None,
            social_media_urls=social_json,
            is_breakout=True,
            discovery_source=source,
            is_champagne=False,
            is_verified=False,
            launch_platform="unknown",
            is_bankr_launch=False,
            launched_at=launched_at,
            discovered_at=datetime.now(timezone.utc),
        )
        session.add(token)
        logger.info(
            "breakout.new_token",
            name=token.name,
            symbol=token.symbol,
            address=addr[:10],
            source=source,
        )
        return token


# ---------------------------------------------------------------------------
# Gainers Scanner — DexScreener search + community takeovers
# ---------------------------------------------------------------------------

_DS_SEARCH = "https://api.dexscreener.com/latest/dex/search"
_DS_CTO_LATEST = "https://api.dexscreener.com/community-takeovers/latest/v1"


class GainersScanner:
    """Discovers high-momentum Base tokens via DexScreener search + CTO.

    Uses two discovery strategies:
    1. **Search scanner** — rotates through keywords (e.g. "clanker",
       "base agent", "base meme") using /latest/dex/search?q=, then
       filters for Base-chain pairs with high volume / price change.
    2. **Community takeover (CTO)** — checks /community-takeovers/latest/v1
       for Base tokens with active CTO (community signal).

    These catch tokens that:
    - Were never on the boost/profile endpoints (breakout scanner blind spot)
    - Launched before the bot started (backfill might have also missed them)
    - Are organically trending via search volume / community activity
    """

    def __init__(self, config: AppConfig, http: httpx.AsyncClient) -> None:
        self.cfg = config.gainers
        self.dex_cfg = config.dexscreener
        self._http = http
        self._keyword_index = 0  # Rotate through search keywords
        self._recent_scans: dict[str, datetime] = {}
        # Pre-build set for O(1) blacklist lookups
        self._skip_set: set[str] = {
            a.lower() for a in self.cfg.skip_addresses
        }

    async def scan(self, session: AsyncSession) -> list[Token]:
        """Run one gainers scan cycle.

        Returns list of new Token objects that qualify as gainers.
        """
        if not self.cfg.enabled:
            return []

        # Step 1: Gather candidates from search + CTO
        candidates = await self._gather_candidates()
        if not candidates:
            logger.debug("gainers.no_candidates")
            return []

        logger.info(
            "gainers.candidates_found",
            total=len(candidates),
            sources=list({c["source"] for c in candidates}),
        )

        # Step 2: Filter fresh (not recently scanned, not already high-scored)
        fresh = await self._filter_fresh(candidates, session)
        if not fresh:
            logger.debug("gainers.all_filtered")
            return []

        # Step 3: Persist qualifying tokens
        new_tokens: list[Token] = []
        for cand in fresh:
            token = await self._persist_gainer(cand, session)
            if token:
                new_tokens.append(token)
                self._recent_scans[cand["address"]] = datetime.now(timezone.utc)

        if new_tokens:
            await session.flush()
            logger.info(
                "gainers.new_tokens",
                count=len(new_tokens),
                names=[t.symbol or t.name for t in new_tokens],
            )

        return new_tokens

    async def _gather_candidates(self) -> list[dict]:
        """Fetch Base token candidates from DexScreener search + CTO."""
        candidates: dict[str, dict] = {}

        # Rotate through 2 keywords per cycle for broader coverage
        keywords = self.cfg.search_keywords
        if keywords:
            for _ in range(2):
                kw = keywords[self._keyword_index % len(keywords)]
                self._keyword_index += 1
                search_results = await self._search_dexscreener(kw)
                for item in search_results:
                    addr = item["address"]
                    if addr not in candidates:
                        candidates[addr] = item

        # Also check community takeovers
        cto_results = await self._fetch_cto()
        for item in cto_results:
            addr = item["address"]
            if addr not in candidates:
                candidates[addr] = item

        return list(candidates.values())

    async def _search_dexscreener(self, query: str) -> list[dict]:
        """Search DexScreener for Base tokens matching a keyword."""
        try:
            resp = await self._http.get(
                _DS_SEARCH,
                params={"q": query},
                timeout=15,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug(
                "gainers.search_failed", query=query, error=str(exc),
            )
            return []

        data = resp.json()
        pairs = data.get("pairs") or []

        results: list[dict] = []
        now = datetime.now(timezone.utc)
        max_age = timedelta(days=self.cfg.max_age_days)

        for pair in pairs:
            if (pair.get("chainId") or "").lower() != "base":
                continue

            addr = (
                pair.get("baseToken", {}).get("address", "").lower()
            )
            if not addr:
                continue

            # Skip known blue-chip / infrastructure tokens
            if addr in self._skip_set:
                continue

            # FDV cap — established tokens are not hidden gems
            fdv = pair.get("fdv") or 0
            if fdv > self.cfg.max_fdv_usd:
                continue

            # Age filter — reject if missing (established tokens often lack it)
            pair_created = pair.get("pairCreatedAt")
            if not pair_created:
                continue
            try:
                created_dt = datetime.fromtimestamp(
                    pair_created / 1000, tz=timezone.utc,
                )
                if (now - created_dt) > max_age:
                    continue
            except (ValueError, OSError):
                continue

            # Quality filters
            liq = (pair.get("liquidity") or {}).get("usd") or 0
            vol_1h = (pair.get("volume") or {}).get("h1") or 0
            buys_1h = (pair.get("txns") or {}).get("h1", {}).get("buys", 0)
            pc_1h = (pair.get("priceChange") or {}).get("h1") or 0

            if liq < self.cfg.min_liquidity_usd:
                continue
            if vol_1h < self.cfg.min_volume_1h_usd:
                continue

            # At least one momentum signal
            has_momentum = (
                buys_1h >= self.cfg.min_buys_1h
                or (isinstance(pc_1h, (int, float))
                    and pc_1h >= self.cfg.min_price_change_1h_pct)
            )
            if not has_momentum:
                continue

            results.append({
                "address": addr,
                "source": f"search_{query.replace(' ', '_')}",
                "pair_data": pair,
            })

        logger.debug(
            "gainers.search_results",
            query=query,
            total_pairs=len(pairs),
            base_qualified=len(results),
        )
        return results

    async def _fetch_cto(self) -> list[dict]:
        """Fetch Base tokens from DexScreener community takeovers."""
        try:
            resp = await self._http.get(_DS_CTO_LATEST, timeout=15)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("gainers.cto_failed", error=str(exc))
            return []

        data = resp.json()
        if not isinstance(data, list):
            return []

        results: list[dict] = []
        for item in data:
            if (item.get("chainId") or "").lower() != "base":
                continue
            addr = (item.get("tokenAddress") or "").lower()
            if not addr:
                continue
            if addr in self._skip_set:
                continue
            results.append({
                "address": addr,
                "source": "community_takeover",
                "description": item.get("description") or "",
                "links": item.get("links") or [],
            })

        logger.debug(
            "gainers.cto_results",
            total=len(data),
            base_tokens=len(results),
        )
        return results

    async def _filter_fresh(
        self, candidates: list[dict], session: AsyncSession,
    ) -> list[dict]:
        """Remove recently scanned or already high-scored tokens."""
        cooldown = timedelta(seconds=self.cfg.rescan_cooldown_seconds)
        now = datetime.now(timezone.utc)
        fresh: list[dict] = []

        for cand in candidates:
            addr = cand["address"]

            # Skip recently scanned
            last_scan = self._recent_scans.get(addr)
            if last_scan and (now - last_scan) < cooldown:
                continue

            # Check DB
            result = await session.execute(
                select(Token.quality_score, Token.is_breakout, Token.alert_sent)
                .where(Token.contract_address == addr)
            )
            row = result.first()
            if row is not None:
                score, is_breakout, alert_sent = row
                # Skip if already alerted
                if alert_sent:
                    continue
                # Skip if already scored above threshold
                if score is not None and score >= 0.45:
                    continue

            fresh.append(cand)

        return fresh

    async def _persist_gainer(
        self, cand: dict, session: AsyncSession,
    ) -> Optional[Token]:
        """Create or update a Token record for a gainer candidate."""
        addr = cand["address"]
        source = cand.get("source", "search_gainer")
        pair = cand.get("pair_data") or {}

        # Check if already in DB
        result = await session.execute(
            select(Token).where(Token.contract_address == addr)
        )
        existing = result.scalar_one_or_none()

        if existing:
            # If it was previously rejected, give it another chance
            if existing.quality_score is not None and existing.quality_score < 0.45:
                existing.quality_score = None
                existing.filter_stage_reached = None
                existing.rejection_reason = None
                existing.alert_sent = False
                existing.discovery_source = source
                logger.info(
                    "gainers.reset_existing",
                    token=existing.symbol,
                    address=addr[:10],
                    source=source,
                )
                return existing
            return None

        # Create new token from pair data
        base_token = pair.get("baseToken") or {}
        info = pair.get("info") or {}
        socials = info.get("socials") or []
        websites = info.get("websites") or []

        social_links: list[dict] = []
        for s in socials:
            social_links.append({
                "name": s.get("platform", "unknown"),
                "link": s.get("handle", ""),
            })
        for w in websites:
            social_links.append({
                "name": "website",
                "link": w.get("url", ""),
            })
        for link in cand.get("links") or []:
            social_links.append({
                "name": link.get("type") or link.get("label") or "link",
                "link": link.get("url", ""),
            })

        social_json = json.dumps(social_links) if social_links else None

        # Parse pair creation time
        launched_at = None
        pair_created = pair.get("pairCreatedAt")
        if pair_created:
            try:
                launched_at = datetime.fromtimestamp(
                    pair_created / 1000, tz=timezone.utc,
                )
            except (ValueError, OSError):
                pass

        token = Token(
            contract_address=addr,
            chain="base",
            name=base_token.get("name"),
            symbol=base_token.get("symbol"),
            image_url=info.get("imageUrl"),
            description=cand.get("description") or None,
            social_media_urls=social_json,
            is_breakout=False,
            discovery_source=source,
            is_champagne=False,
            is_verified=False,
            launch_platform="unknown",
            is_bankr_launch=False,
            launched_at=launched_at,
            discovered_at=datetime.now(timezone.utc),
        )
        session.add(token)
        logger.info(
            "gainers.new_token",
            name=token.name,
            symbol=token.symbol,
            address=addr[:10],
            source=source,
        )
        return token


# Needed for asyncio.gather in BreakoutScanner._gather_candidates
import asyncio
