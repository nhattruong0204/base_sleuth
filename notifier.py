"""Telegram notification sender for qualifying tokens."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx
import structlog

from .config import TelegramConfig
from .filters import FilterResult
from .models import Token, TokenContext

logger = structlog.get_logger(__name__)


class TelegramNotifier:
    """Sends formatted alerts to a Telegram chat/channel."""

    def __init__(self, config: TelegramConfig):
        self.config = config
        self._http = httpx.AsyncClient(timeout=15.0)
        self._last_sent_at: float = 0.0

    async def close(self):
        await self._http.aclose()

    async def notify(
        self,
        token: Token,
        result: FilterResult,
        context: Optional[TokenContext] = None,
    ) -> bool:
        """
        Send a Telegram alert for a token that passed filtering.

        Returns True if message was sent successfully.
        """
        if not self.config.enabled:
            return False

        if result.final_score < self.config.min_score_to_notify:
            return False

        # Cooldown
        now = time.monotonic()
        if now - self._last_sent_at < self.config.cooldown_seconds:
            remaining = self.config.cooldown_seconds - (now - self._last_sent_at)
            logger.debug("telegram_cooldown", remaining=f"{remaining:.0f}s")
            await asyncio.sleep(remaining)

        message = self._format_message(token, result, context)

        try:
            url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
            payload = {
                "chat_id": self.config.chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            }
            resp = await self._http.post(url, json=payload)
            resp.raise_for_status()
            self._last_sent_at = time.monotonic()
            logger.info("telegram_sent", symbol=token.symbol, score=f"{result.final_score:.0f}")
            return True

        except Exception as e:
            logger.error("telegram_send_error", symbol=token.symbol, error=str(e))
            return False

    def _format_message(
        self,
        token: Token,
        result: FilterResult,
        context: Optional[TokenContext],
    ) -> str:
        """Build a rich Telegram message with token info + context."""

        # Score emoji
        if result.final_score >= 80:
            score_emoji = "💎"
        elif result.final_score >= 60:
            score_emoji = "🔥"
        else:
            score_emoji = "👀"

        # Origin badge
        origin = "🤖 Bankr" if token.is_bankr_origin else "🔧 Clanker"

        lines = [
            f"{score_emoji} <b>New Token Alert: ${token.symbol}</b>",
            f"",
            f"<b>Name:</b> {token.name}",
            f"<b>Score:</b> {result.final_score:.0f}/100",
            f"<b>Origin:</b> {origin}",
            f"<b>Chain:</b> Base",
        ]

        if token.starting_mcap_eth:
            lines.append(f"<b>Starting MCap:</b> {token.starting_mcap_eth} ETH")

        # Contract + links
        ca = token.contract_address
        lines.extend([
            f"",
            f"<b>Contract:</b> <code>{ca}</code>",
            f"",
            f"📊 <a href='https://www.clanker.world/clanker/{ca}'>Clanker</a>"
            f" | <a href='https://dexscreener.com/base/{ca}'>DexScreener</a>"
            f" | <a href='https://basescan.org/token/{ca}'>BaseScan</a>",
        ])

        # Scoring breakdown
        lines.extend([
            f"",
            f"<b>Scores:</b> Metrics {result.stage2_score:.0f}"
            f" | Smart💰 {result.stage3_score:.0f}"
            f" | Context {result.stage4_score:.0f}",
        ])

        # Context traceback
        if context:
            lines.append(f"")
            lines.append(f"<b>── Origin Context ──</b>")

            if context.x_tweet_url:
                lines.append(f"🐦 <a href='{context.x_tweet_url}'>Original Tweet</a>")
            if context.x_author_username:
                lines.append(f"👤 @{context.x_author_username}")
            if context.x_tweet_text:
                # Truncate and escape HTML
                text = context.x_tweet_text[:200]
                text = text.replace("<", "&lt;").replace(">", "&gt;")
                lines.append(f"💬 {text}")

            if context.farcaster_cast_url:
                lines.append(f"🟣 <a href='{context.farcaster_cast_url}'>Farcaster Cast</a>")
            if context.farcaster_text:
                text = context.farcaster_text[:200]
                text = text.replace("<", "&lt;").replace(">", "&gt;")
                lines.append(f"💬 {text}")

            if context.project_idea:
                idea = context.project_idea[:300]
                idea = idea.replace("<", "&lt;").replace(">", "&gt;")
                lines.append(f"")
                lines.append(f"<b>Project:</b> {idea}")

        # Warnings
        if result.rejection_reasons:
            lines.append(f"")
            lines.append(f"⚠️ <b>Flags:</b> {', '.join(result.rejection_reasons[:3])}")

        # Disclaimer
        lines.extend([
            f"",
            f"<i>DYOR — this is automated detection, not financial advice.</i>",
        ])

        return "\n".join(lines)
