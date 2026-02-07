"""
Context resolver: traces a Clanker token back to its X/Farcaster origin.

Strategy (no X API needed):
1. Scrape the clanker.world token page — often links to the originating post
2. Check token's social_media_urls field for X/Farcaster links
3. Web search fallback: search "$SYMBOL bankrbot" to find the originating tweet
4. Farcaster search via Neynar API (generous free tier)
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

import httpx
import structlog
from bs4 import BeautifulSoup

from .config import AppConfig
from .models import Token, TokenContext, get_session_factory

logger = structlog.get_logger(__name__)

# Regex patterns for extracting X/Farcaster URLs
X_URL_PATTERN = re.compile(r"https?://(?:x\.com|twitter\.com)/\w+/status/\d+")
FARCASTER_URL_PATTERN = re.compile(r"https?://(?:warpcast\.com)/\w+/0x[a-fA-F0-9]+")


class ContextResolver:
    """Async context resolver that enriches tokens with origin info."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._http = httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; ClankerTracker/1.0)"
            },
        )
        self._semaphore = asyncio.Semaphore(config.context.max_concurrency)

    async def close(self):
        await self._http.aclose()

    async def resolve(self, token: Token) -> Optional[TokenContext]:
        """
        Attempt to trace a token back to its origin.
        Tries multiple strategies in order of reliability.
        """
        async with self._semaphore:
            ctx = TokenContext(contract_address=token.contract_address)

            # Strategy 1: Check existing social URLs from Clanker API
            if token.social_urls:
                self._extract_urls_from_social(token.social_urls, ctx)
                if ctx.x_tweet_url or ctx.farcaster_cast_url:
                    ctx.resolution_method = "clanker_social_urls"
                    ctx.resolved_at = datetime.now(timezone.utc)
                    logger.info("context_resolved_social_urls", symbol=token.symbol)
                    return await self._save_and_return(ctx)

            # Strategy 2: Scrape clanker.world token page
            page_ctx = await self._scrape_clanker_page(token.contract_address)
            if page_ctx:
                self._merge_context(ctx, page_ctx)
                if ctx.x_tweet_url or ctx.farcaster_cast_url or ctx.project_idea:
                    ctx.resolution_method = "clanker_page_scrape"
                    ctx.resolved_at = datetime.now(timezone.utc)
                    logger.info("context_resolved_page_scrape", symbol=token.symbol)
                    return await self._save_and_return(ctx)

            # Strategy 3: Web search fallback (DuckDuckGo HTML, no API key)
            if self.config.context.web_search_fallback:
                search_ctx = await self._web_search_context(token.symbol, token.name)
                if search_ctx:
                    self._merge_context(ctx, search_ctx)
                    if ctx.x_tweet_url or ctx.farcaster_cast_url:
                        ctx.resolution_method = "web_search"
                        ctx.resolved_at = datetime.now(timezone.utc)
                        logger.info("context_resolved_web_search", symbol=token.symbol)
                        return await self._save_and_return(ctx)

            # If we got nothing, still save a stub so we don't re-process
            ctx.resolution_method = "unresolved"
            ctx.resolved_at = datetime.now(timezone.utc)
            logger.debug("context_unresolved", symbol=token.symbol)
            return await self._save_and_return(ctx)

    def _extract_urls_from_social(self, social_urls_raw: str, ctx: TokenContext):
        """Parse social URLs and extract X/Farcaster links."""
        try:
            urls = json.loads(social_urls_raw) if isinstance(social_urls_raw, str) else social_urls_raw
        except (json.JSONDecodeError, TypeError):
            urls = [social_urls_raw] if social_urls_raw else []

        if not isinstance(urls, list):
            urls = [urls]

        for url in urls:
            if not isinstance(url, str):
                continue
            x_match = X_URL_PATTERN.search(url)
            if x_match and not ctx.x_tweet_url:
                ctx.x_tweet_url = x_match.group(0)
                ctx.source_platform = "x"

            fc_match = FARCASTER_URL_PATTERN.search(url)
            if fc_match and not ctx.farcaster_cast_url:
                ctx.farcaster_cast_url = fc_match.group(0)
                ctx.source_platform = ctx.source_platform or "farcaster"

    async def _scrape_clanker_page(self, contract_address: str) -> Optional[TokenContext]:
        """
        Scrape clanker.world/clanker/{address} for origin context.

        The token page often contains:
        - Link to the originating Farcaster cast
        - Creator info
        - Description with project context
        """
        url = f"https://www.clanker.world/clanker/{contract_address}"
        try:
            await asyncio.sleep(self.config.context.request_delay_seconds)
            resp = await self._http.get(url)
            if resp.status_code != 200:
                return None

            soup = BeautifulSoup(resp.text, "lxml")
            ctx = TokenContext(contract_address=contract_address)

            # Look for X/Twitter links in the page
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                x_match = X_URL_PATTERN.search(href)
                if x_match and not ctx.x_tweet_url:
                    ctx.x_tweet_url = x_match.group(0)
                    # Try to get tweet text from surrounding elements
                    parent = a_tag.find_parent(["div", "section", "article"])
                    if parent:
                        text_content = parent.get_text(strip=True, separator=" ")[:500]
                        if text_content:
                            ctx.x_tweet_text = text_content

                fc_match = FARCASTER_URL_PATTERN.search(href)
                if fc_match and not ctx.farcaster_cast_url:
                    ctx.farcaster_cast_url = fc_match.group(0)

            # Look for description / project context in meta tags or page content
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                ctx.project_idea = meta_desc["content"][:1000]

            # Try OG description as fallback
            og_desc = soup.find("meta", attrs={"property": "og:description"})
            if og_desc and og_desc.get("content") and not ctx.project_idea:
                ctx.project_idea = og_desc["content"][:1000]

            return ctx if (ctx.x_tweet_url or ctx.farcaster_cast_url or ctx.project_idea) else None

        except Exception as e:
            logger.warning("clanker_page_scrape_error", address=contract_address, error=str(e))
            return None

    async def _web_search_context(self, symbol: str, name: str) -> Optional[TokenContext]:
        """
        DuckDuckGo HTML search for "$SYMBOL bankrbot" to find origin tweet.
        No API key required. Fragile but functional.
        """
        query = f'"{symbol}" bankrbot site:x.com'
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

        try:
            await asyncio.sleep(self.config.context.request_delay_seconds)
            resp = await self._http.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            if resp.status_code != 200:
                return None

            soup = BeautifulSoup(resp.text, "lxml")
            ctx = TokenContext(contract_address="")

            # DDG results are in <a class="result__a"> tags
            for result in soup.select("a.result__a, a.result__url"):
                href = result.get("href", "")
                x_match = X_URL_PATTERN.search(href)
                if x_match:
                    ctx.x_tweet_url = x_match.group(0)
                    # Get snippet text
                    snippet = result.find_parent("div")
                    if snippet:
                        snippet_text = snippet.get_text(strip=True, separator=" ")[:500]
                        ctx.x_tweet_text = snippet_text
                    ctx.source_platform = "x"
                    return ctx

            return None

        except Exception as e:
            logger.warning("web_search_error", symbol=symbol, error=str(e))
            return None

    def _merge_context(self, target: TokenContext, source: TokenContext):
        """Merge non-None fields from source into target."""
        for field in [
            "x_tweet_url", "x_author_username", "x_author_display_name",
            "x_tweet_text", "farcaster_cast_url", "farcaster_author",
            "farcaster_text", "project_idea", "source_platform",
        ]:
            src_val = getattr(source, field, None)
            if src_val and not getattr(target, field, None):
                setattr(target, field, src_val)

    async def _save_and_return(self, ctx: TokenContext) -> TokenContext:
        """Persist context to DB."""
        session_factory = get_session_factory()
        async with session_factory() as session:
            async with session.begin():
                session.add(ctx)
        return ctx
