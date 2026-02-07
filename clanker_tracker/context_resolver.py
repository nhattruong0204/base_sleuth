"""Traces Clanker tokens back to the original X tweet or Farcaster cast.

Resolution strategies (tried in order):
1. Parse social_media_urls / socialLinks from the Clanker API response
2. Scrape clanker.world/clanker/{address} token page for origin links
3. DuckDuckGo fallback search: "$SYMBOL bankrbot site:x.com"

No X (Twitter) API is required.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

import httpx
import structlog
from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession

from .config import AppConfig
from .models import Token, TokenContext

logger = structlog.get_logger(__name__)

# Regex patterns for origin URLs
X_URL_RE = re.compile(r"https?://(?:x|twitter)\.com/\w+/status/\d+", re.IGNORECASE)
FARCASTER_URL_RE = re.compile(
    r"https?://(?:warpcast\.com/\w+/0x[a-f0-9]+|far\.quest/\w+)", re.IGNORECASE,
)


class ContextResolver:
    """Async resolver that traces token origins using 3 strategies."""

    def __init__(self, config: AppConfig, http: httpx.AsyncClient) -> None:
        self.cfg = config.scraping
        self._http = http

    async def resolve(self, token: Token, session: AsyncSession) -> TokenContext:
        """Attempt to resolve origin context for *token*.

        Always returns a TokenContext (persisted) — even if unresolved.
        """
        ctx = TokenContext(token_id=token.id)

        # Strategy 1 — social_media_urls from API payload
        url, platform = self._try_social_urls(token)
        if url:
            ctx.origin_url = url
            ctx.origin_platform = platform
            ctx.resolution_strategy = "social_urls"
            logger.info("context.resolved", strategy="social_urls", token=token.symbol)
        else:
            # Strategy 2 — scrape the Clanker token page
            url, platform = await self._try_page_scrape(token)
            if url:
                ctx.origin_url = url
                ctx.origin_platform = platform
                ctx.resolution_strategy = "page_scrape"
                logger.info("context.resolved", strategy="page_scrape", token=token.symbol)
            else:
                # Strategy 3 — DuckDuckGo search
                url, platform = await self._try_ddg_search(token)
                if url:
                    ctx.origin_url = url
                    ctx.origin_platform = platform
                    ctx.resolution_strategy = "ddg_search"
                    logger.info("context.resolved", strategy="ddg_search", token=token.symbol)
                else:
                    ctx.resolution_strategy = "unresolved"
                    logger.warning("context.unresolved", token=token.symbol)

        ctx.resolved_at = datetime.now(timezone.utc)
        session.add(ctx)
        await session.flush()
        return ctx

    # ------------------------------------------------------------------
    # Strategy 1: Parse social_media_urls from Clanker API
    # ------------------------------------------------------------------

    @staticmethod
    def _try_social_urls(token: Token) -> tuple[Optional[str], Optional[str]]:
        """Extract origin URL from social_media_urls field.

        Handles two formats:
        - Legacy: JSON array of URL strings  ["https://x.com/..."]
        - New socialLinks format stored as JSON array of dicts:
          [{"name":"x","link":"https://x.com/..."}]
        """
        if not token.social_media_urls:
            return None, None
        try:
            raw = json.loads(token.social_media_urls)
        except (json.JSONDecodeError, TypeError):
            return None, None

        urls: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    urls.append(item)
                elif isinstance(item, dict):
                    # socialLinks format: {"name": "x", "link": "https://..."}
                    link = item.get("link") or item.get("url") or ""
                    if link:
                        urls.append(link)

        for url in urls:
            if X_URL_RE.search(url):
                return url, "x"
            if FARCASTER_URL_RE.search(url):
                return url, "farcaster"
        # Return first URL even if platform unrecognised
        if urls:
            return urls[0], "unknown"
        return None, None

    # ------------------------------------------------------------------
    # Strategy 2: Scrape clanker.world token page
    # ------------------------------------------------------------------

    async def _try_page_scrape(
        self, token: Token,
    ) -> tuple[Optional[str], Optional[str]]:
        if not token.contract_address:
            return None, None

        page_url = self.cfg.clanker_page_url.format(address=token.contract_address)
        try:
            resp = await self._http.get(
                page_url,
                timeout=self.cfg.request_timeout_seconds,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("scrape.failed", url=page_url, error=str(exc))
            return None, None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Look for anchor tags containing tweet / cast URLs
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if X_URL_RE.search(href):
                return href, "x"
            if FARCASTER_URL_RE.search(href):
                return href, "farcaster"

        return None, None

    # ------------------------------------------------------------------
    # Strategy 3: DuckDuckGo HTML search
    # ------------------------------------------------------------------

    async def _try_ddg_search(
        self, token: Token,
    ) -> tuple[Optional[str], Optional[str]]:
        if not token.symbol:
            return None, None

        query = self.cfg.ddg_search_template.replace("${symbol}", f"${token.symbol}")
        ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

        try:
            resp = await self._http.get(
                ddg_url,
                timeout=self.cfg.request_timeout_seconds,
                headers={"User-Agent": "Mozilla/5.0"},
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("ddg.search.failed", query=query, error=str(exc))
            return None, None

        soup = BeautifulSoup(resp.text, "html.parser")

        # DuckDuckGo HTML results have class "result__a"
        for link in soup.select("a.result__a"):
            href = link.get("href", "")
            if X_URL_RE.search(href):
                return href, "x"
            if FARCASTER_URL_RE.search(href):
                return href, "farcaster"

        return None, None
