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
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
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
            logger.info("watermark.init", watermark=self._watermark)
        else:
            logger.info("watermark.init", watermark="none — first run")

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
            # Check if already tracked
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
