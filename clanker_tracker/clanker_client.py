"""Polls the Clanker public API for new token launches on Base.

Endpoint: GET https://www.clanker.world/api/tokens?sort=desc&page=1&pageSize=50
Response shape (observed):
{
  "data": [
    {
      "id": 12345,
      "name": "TokenName",
      "symbol": "TKN",
      "img_url": "...",
      "contract_address": "0x...",
      "pool_address": "0x...",
      "requestor_fid": ...,
      "requestorAddress": "0x...",
      "created_at": "2025-...",
      "social_media_urls": ["https://x.com/..."],
      ...
    }, ...
  ]
}
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


class ClankerClient:
    """Polls the Clanker public API and persists new tokens to the database."""

    def __init__(self, config: AppConfig, http: httpx.AsyncClient) -> None:
        self.cfg = config.clanker
        self.bankr_addresses = {
            addr.lower() for addr in config.bankr.deployer_addresses
        }
        self._http = http
        # Watermark: highest clanker_id we've seen so far
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

    async def poll(self, session: AsyncSession) -> list[Token]:
        """Fetch the latest tokens page and persist any new ones.

        Returns the list of newly-inserted Token objects.
        """
        url = f"{self.cfg.base_url}/tokens"
        params = {
            "sort": "desc",
            "page": 1,
            "pageSize": self.cfg.page_size,
        }
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
            # Advance watermark
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

    def _parse_token(self, item: dict) -> Token:
        """Map a Clanker API response item to a Token ORM object."""
        contract = (item.get("contract_address") or "").lower()
        requestor = (item.get("requestorAddress") or "").lower()
        deployer = (item.get("deployer_address") or requestor).lower()

        # Determine if this is a Bankr bot launch
        is_bankr = requestor in self.bankr_addresses or deployer in self.bankr_addresses

        # Parse timestamps
        launched_at = None
        raw_ts = item.get("created_at") or item.get("launched_at")
        if raw_ts:
            try:
                launched_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        # Social media URLs (store as JSON string)
        social_urls = item.get("social_media_urls")
        social_json = json.dumps(social_urls) if social_urls else None

        return Token(
            contract_address=contract,
            chain="base",
            deployer_address=deployer or None,
            pool_address=(item.get("pool_address") or "").lower() or None,
            clanker_id=item.get("id"),
            name=item.get("name"),
            symbol=item.get("symbol"),
            image_url=item.get("img_url"),
            requestor_address=requestor or None,
            social_media_urls=social_json,
            is_bankr_launch=is_bankr,
            launched_at=launched_at,
            discovered_at=datetime.now(timezone.utc),
        )
