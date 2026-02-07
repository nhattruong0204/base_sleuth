"""Clanker API client for polling new token launches."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import structlog

from .config import AppConfig
from .models import Token, get_session_factory

logger = structlog.get_logger(__name__)


class ClankerClient:
    """Polls clanker.world public API for newly deployed tokens."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.base_url = config.clanker.base_url.rstrip("/")
        self.page_size = config.clanker.page_size
        self._http = httpx.AsyncClient(
            timeout=30.0,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )
        # Track the newest clanker_id we've seen to avoid reprocessing
        self._last_seen_id: Optional[int] = None

    async def close(self):
        await self._http.aclose()

    async def fetch_latest_tokens(self, page: int = 1) -> list[dict[str, Any]]:
        """
        GET /tokens — paginated, sorted by newest first.

        The public Clanker API returns deployed tokens with metadata
        including name, symbol, contract address, deployer, pool info,
        warnings, and social URLs.
        """
        url = f"{self.base_url}/tokens"
        params = {
            "page": page,
            "pageSize": self.page_size,
            "sort": "desc",  # newest first
        }

        try:
            resp = await self._http.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            # Clanker API typically returns { "data": [...], "hasMore": bool }
            # or just a list — handle both
            if isinstance(data, dict):
                return data.get("data", data.get("tokens", []))
            if isinstance(data, list):
                return data
            return []

        except httpx.HTTPStatusError as e:
            logger.error("clanker_api_http_error", status=e.response.status_code, url=url)
            return []
        except Exception as e:
            logger.error("clanker_api_error", error=str(e), url=url)
            return []

    async def fetch_token_by_address(self, contract_address: str) -> Optional[dict]:
        """GET /get-clanker-by-address — fetch single token details."""
        url = f"{self.base_url}/get-clanker-by-address"
        params = {"address": contract_address}

        try:
            resp = await self._http.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning("clanker_fetch_single_error", address=contract_address, error=str(e))
            return None

    def parse_token(self, raw: dict[str, Any]) -> Optional[Token]:
        """
        Convert raw API response dict into a Token model instance.

        Clanker API response fields (observed, may vary):
        - id, name, symbol, img, contract_address, deployer, requestor_address
        - pool_address, paired_token_address, starting_market_cap
        - created_at, description, social_media_urls, warnings, pool_config
        """
        contract = raw.get("contract_address") or raw.get("address")
        if not contract:
            return None

        # Parse timestamps
        deployed_at = None
        raw_ts = raw.get("created_at") or raw.get("deployed_at")
        if raw_ts:
            try:
                deployed_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        # Social URLs — could be a list or JSON string
        social_urls = raw.get("social_media_urls") or raw.get("socialMediaUrls")
        if isinstance(social_urls, list):
            social_urls = json.dumps(social_urls)

        # Warnings
        warnings = raw.get("warnings")
        if isinstance(warnings, list):
            warnings = json.dumps(warnings)

        # Determine if Bankr-originated
        requestor = raw.get("requestor_address") or raw.get("requestorAddress") or ""
        is_bankr = self.config.bankr.is_bankr_deployer(requestor)

        # Chain ID
        chain_id = raw.get("chain_id") or raw.get("chainId") or 8453

        return Token(
            clanker_id=raw.get("id"),
            contract_address=contract.lower(),
            name=raw.get("name", "Unknown"),
            symbol=raw.get("symbol", "???"),
            deployer_address=(raw.get("deployer") or raw.get("deployer_address") or "").lower(),
            chain_id=chain_id,
            image_url=raw.get("img") or raw.get("image"),
            description=raw.get("description"),
            social_urls=social_urls,
            pool_address=(raw.get("pool_address") or "").lower() or None,
            paired_token=(raw.get("paired_token_address") or "").lower() or None,
            starting_mcap_eth=raw.get("starting_market_cap"),
            warnings=warnings,
            is_bankr_origin=is_bankr,
            requestor_address=requestor.lower() if requestor else None,
            deployed_at=deployed_at,
        )

    async def poll_new_tokens(self) -> list[Token]:
        """
        Fetch latest tokens, deduplicate against DB, return only new ones.

        Returns list of Token model instances (not yet committed to DB).
        """
        raw_tokens = await self.fetch_latest_tokens(page=1)
        if not raw_tokens:
            return []

        session_factory = get_session_factory()
        new_tokens: list[Token] = []

        async with session_factory() as session:
            for raw in raw_tokens:
                token = self.parse_token(raw)
                if token is None:
                    continue

                # Skip if we've already seen this clanker_id
                if self._last_seen_id and token.clanker_id and token.clanker_id <= self._last_seen_id:
                    continue

                # Check DB for existing contract
                from sqlalchemy import select
                stmt = select(Token.id).where(
                    Token.contract_address == token.contract_address
                )
                result = await session.execute(stmt)
                if result.scalar_one_or_none() is not None:
                    continue

                new_tokens.append(token)

        # Update watermark
        if new_tokens:
            max_id = max(
                (t.clanker_id for t in new_tokens if t.clanker_id), default=None
            )
            if max_id and (self._last_seen_id is None or max_id > self._last_seen_id):
                self._last_seen_id = max_id

            logger.info(
                "new_tokens_detected",
                count=len(new_tokens),
                symbols=[t.symbol for t in new_tokens[:5]],
            )

        return new_tokens

    async def save_tokens(self, tokens: list[Token]) -> None:
        """Persist new tokens to the database."""
        if not tokens:
            return

        session_factory = get_session_factory()
        async with session_factory() as session:
            async with session.begin():
                for token in tokens:
                    session.add(token)
            logger.info("tokens_saved", count=len(tokens))
