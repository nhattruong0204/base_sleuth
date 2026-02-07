"""Async orchestrator for the Clanker Token Tracker — 24/7 daemon.

Runs five concurrent loops:
- Firehose loop   : streams ALL new tokens via cursor pagination (30s poll)
- Champagne loop  : scans champagne-tagged curated gems (120s poll)
- Breakout loop   : detects delayed movers via DexScreener trending (180s poll)
- Gainers loop    : searches DexScreener for high-momentum Base tokens (120s poll)
- Eval loop       : pre-filters → batch DexScreener → scores → alerts

On first run, performs a backfill to catch tokens from the last 12 hours.

Designed for unattended 24/7 operation with PostgreSQL persistence.
Handles graceful shutdown via SIGINT / SIGTERM, exponential backoff on
transient errors, and periodic heartbeat logging.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from pathlib import Path

import httpx
import structlog

from .bot_commands import build_telegram_app
from .clanker_client import ClankerClient, BreakoutScanner, GainersScanner
from .config import AppConfig, load_config
from .context_resolver import ContextResolver
from .filters import TokenFilter
from .models import Token, create_engine, create_session_factory, init_db
from .notifier import TelegramNotifier

logger = structlog.get_logger(__name__)


class Tracker:
    """Top-level orchestrator that coordinates all components."""

    def __init__(self, config: AppConfig) -> None:
        self.cfg = config
        self._shutdown = asyncio.Event()
        self._start_time: float = 0.0

        # Per-loop error backoff state (consecutive failure count)
        self._backoff: dict[str, int] = {
            "poll": 0, "champagne": 0, "breakout": 0, "gainers": 0, "eval": 0,
        }
        self._MAX_BACKOFF = 300  # Cap backoff at 5 min

        # Stats counters
        self._stats = {
            "tokens_discovered": 0,
            "tokens_scored": 0,
            "alerts_sent": 0,
            "errors": 0,
        }

        # Core components (initialised in .start())
        self._http: httpx.AsyncClient | None = None
        self._engine = None
        self._session_factory = None
        self._client: ClankerClient | None = None
        self._breakout: BreakoutScanner | None = None
        self._gainers: GainersScanner | None = None
        self._resolver: ContextResolver | None = None
        self._filter: TokenFilter | None = None
        self._notifier: TelegramNotifier | None = None
        self._telegram_app = None  # python-telegram-bot Application

    async def start(self) -> None:
        """Initialise resources and run the main loops."""
        self._configure_logging()
        self._start_time = time.monotonic()
        logger.info("tracker.starting", database="PostgreSQL")

        # HTTP client
        self._http = httpx.AsyncClient(
            headers={"User-Agent": "BaseSleuth/1.0"},
            follow_redirects=True,
            timeout=20.0,
        )

        # Database (PostgreSQL via asyncpg)
        db = self.cfg.database
        self._engine = create_engine(
            db.url,
            echo=db.echo,
            pool_size=db.pool_size,
            max_overflow=db.max_overflow,
            pool_recycle=db.pool_recycle,
        )
        await init_db(self._engine)
        self._session_factory = create_session_factory(self._engine)

        # Components
        self._client = ClankerClient(self.cfg, self._http)
        self._breakout = BreakoutScanner(self.cfg, self._http)
        self._gainers = GainersScanner(self.cfg, self._http)
        self._resolver = ContextResolver(self.cfg, self._http)
        self._filter = TokenFilter(self.cfg, self._http)
        self._notifier = TelegramNotifier(self.cfg)

        # Watermark from DB + backfill on first run
        async with self._session_factory() as session:
            await self._client.init_watermark(session)

            if self._client.is_first_run and self.cfg.backfill.enabled:
                logger.info(
                    "backfill.starting",
                    hours=self.cfg.backfill.hours,
                    max_pages=self.cfg.backfill.max_pages,
                )
                bf_count = await self._client.backfill(
                    session,
                    hours=self.cfg.backfill.hours,
                    max_pages=self.cfg.backfill.max_pages,
                )
                await session.commit()
                self._stats["tokens_discovered"] += bf_count
                logger.info("backfill.complete", tokens=bf_count)

        # Signal handling
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_signal, sig)

        # Start interactive Telegram bot (non-blocking)
        await self._start_telegram_bot()

        # Startup notification
        await self._send_startup_ping()

        # Run loops concurrently (+ heartbeat)
        logger.info("tracker.running", loops=5, telegram_bot="active")
        await asyncio.gather(
            self._poll_loop(),
            self._champagne_loop(),
            self._breakout_loop(),
            self._gainers_loop(),
            self._eval_loop(),
            self._heartbeat_loop(),
        )

        await self._send_shutdown_ping()
        await self._stop_telegram_bot()
        await self._cleanup()
        logger.info("tracker.stopped")

    # ------------------------------------------------------------------
    # Firehose poll loop — discover ALL new tokens
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        interval = self.cfg.clanker.poll_interval_seconds
        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    new_tokens = await self._client.poll(session)
                    await session.commit()
                    if new_tokens:
                        bankr_ct = sum(1 for t in new_tokens if t.is_bankr_launch)
                        self._stats["tokens_discovered"] += len(new_tokens)
                        logger.info(
                            "poll.batch",
                            count=len(new_tokens),
                            bankr=bankr_ct,
                            other=len(new_tokens) - bankr_ct,
                        )
                self._backoff["poll"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["poll"] = min(self._backoff["poll"] + 1, 10)
                logger.exception("poll.error", backoff=self._backoff["poll"])

            wait = interval + self._backoff_delay("poll")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Champagne scan loop — curated gems only
    # ------------------------------------------------------------------

    async def _champagne_loop(self) -> None:
        """Periodically fetch champagne-tagged tokens (Clanker's curated tier).

        These are extremely rare (~0.02% of all tokens) but have a 58%
        chance of having real liquidity — vs <1% for regular tokens.
        """
        interval = self.cfg.clanker.champagne_poll_interval_seconds
        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    gems = await self._client.poll_champagne(session)
                    await session.commit()
                    if gems:
                        self._stats["tokens_discovered"] += len(gems)
                        logger.info(
                            "champagne.found",
                            count=len(gems),
                            names=[t.name for t in gems],
                        )
                self._backoff["champagne"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["champagne"] = min(self._backoff["champagne"] + 1, 10)
                logger.exception("champagne.error", backoff=self._backoff["champagne"])

            wait = interval + self._backoff_delay("champagne")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Breakout scanner loop — delayed movers via DexScreener
    # ------------------------------------------------------------------

    async def _breakout_loop(self) -> None:
        """Periodically scan DexScreener for Base tokens gaining momentum.

        Uses boosted/trending/profile endpoints to detect tokens that
        were launched days ago but are now showing strong signals.
        Catches delayed movers that the firehose missed.
        """
        if not self.cfg.breakout.enabled:
            logger.info("breakout.disabled")
            return

        interval = self.cfg.breakout.poll_interval_seconds
        # Initial delay to let firehose populate first
        await asyncio.sleep(30)

        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    breakouts = await self._breakout.scan(session)
                    await session.commit()
                    if breakouts:
                        self._stats["tokens_discovered"] += len(breakouts)
                        logger.info(
                            "breakout.batch",
                            count=len(breakouts),
                            names=[t.symbol or t.name for t in breakouts],
                        )
                self._backoff["breakout"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["breakout"] = min(self._backoff["breakout"] + 1, 10)
                logger.exception("breakout.error", backoff=self._backoff["breakout"])

            wait = interval + self._backoff_delay("breakout")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Gainers scanner loop — DexScreener search + community takeovers
    # ------------------------------------------------------------------

    async def _gainers_loop(self) -> None:
        """Periodically search DexScreener for high-momentum Base tokens.

        Uses two strategies:
        1. Search endpoint with rotating keywords (e.g. 'clanker', 'base meme')
        2. Community takeovers feed for organic signals

        Catches tokens the firehose and breakout scanner missed — e.g. tokens
        launched hours ago that are now trending via search volume.
        """
        if not self.cfg.gainers.enabled:
            logger.info("gainers.disabled")
            return

        interval = self.cfg.gainers.poll_interval_seconds
        # Initial delay to let other scanners populate first
        await asyncio.sleep(45)

        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    new_tokens = await self._gainers.scan(session)
                    await session.commit()
                    if new_tokens:
                        self._stats["tokens_discovered"] += len(new_tokens)
                        logger.info(
                            "gainers.batch",
                            count=len(new_tokens),
                            names=[t.symbol or t.name for t in new_tokens],
                        )
                self._backoff["gainers"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["gainers"] = min(self._backoff["gainers"] + 1, 10)
                logger.exception("gainers.error", backoff=self._backoff["gainers"])

            wait = interval + self._backoff_delay("gainers")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Eval loop — pre-filter → batch DEX → score → notify
    # ------------------------------------------------------------------

    async def _eval_loop(self) -> None:
        """Process tokens that have not been scored yet.

        Pipeline:
        1. Fetch unscored tokens from DB
        2. Pre-filter: skip obvious trash (Bankr, no socials, etc.)
        3. Batch DexScreener lookup for survivors
        4. Full scoring pipeline for tokens with DEX data
        5. Telegram alert for gems above threshold
        """
        # Small initial delay so poll loops can populate the DB
        await asyncio.sleep(5)
        recheck_delay = self.cfg.filtering.recheck_delay_seconds

        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    from datetime import datetime, timezone, timedelta
                    from sqlalchemy import select

                    # Only evaluate tokens that are old enough for
                    # DexScreener to have indexed them
                    cutoff = datetime.now(timezone.utc) - timedelta(seconds=recheck_delay)
                    stmt = (
                        select(Token)
                        .where(Token.quality_score.is_(None))
                        .where(Token.discovered_at <= cutoff)
                        .order_by(
                            # Champagne tokens first (priority queue)
                            Token.is_champagne.desc(),
                            Token.is_breakout.desc(),
                            Token.discovered_at.asc(),
                        )
                        .limit(30)  # Match DexScreener batch size
                    )
                    result = await session.execute(stmt)
                    tokens = list(result.scalars().all())

                    if not tokens:
                        # Nothing to process
                        try:
                            await asyncio.wait_for(
                                self._shutdown.wait(), timeout=10,
                            )
                            break
                        except asyncio.TimeoutError:
                            continue

                    # Step 1: Pre-filter
                    candidates: list[Token] = []
                    for token in tokens:
                        passed, reason = self._filter.pre_filter(token)
                        if not passed:
                            # Mark as rejected so we don't re-process
                            token.quality_score = 0.0
                            token.filter_stage_reached = 0
                            token.rejection_reason = reason
                            logger.debug(
                                "pre_filter.rejected",
                                token=token.symbol,
                                reason=reason,
                            )
                        else:
                            candidates.append(token)

                    if candidates:
                        logger.info(
                            "eval.batch",
                            total=len(tokens),
                            passed_prefilter=len(candidates),
                            champagne=sum(
                                1 for t in candidates if t.is_champagne
                            ),
                            breakout=sum(
                                1 for t in candidates if t.is_breakout
                            ),
                        )

                    # Step 2: Full scoring for candidates
                    for token in candidates:
                        if self._shutdown.is_set():
                            break

                        # Resolve context
                        ctx = await self._resolver.resolve(token, session)

                        # Score
                        filt_result = await self._filter.evaluate(
                            token, session,
                        )
                        self._stats["tokens_scored"] += 1

                        # Notify if above threshold
                        if not filt_result.rejected and not token.alert_sent:
                            sent = await self._notifier.notify(
                                token, ctx, filt_result,
                            )
                            if sent:
                                token.alert_sent = True
                                self._stats["alerts_sent"] += 1
                                logger.info(
                                    "alert.sent",
                                    token=token.symbol,
                                    score=round(filt_result.final_score, 3),
                                    breakout=token.is_breakout,
                                    champagne=token.is_champagne,
                                )

                    await session.commit()

                self._backoff["eval"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["eval"] = min(self._backoff["eval"] + 1, 10)
                logger.exception("eval.error", backoff=self._backoff["eval"])

            # Interruptible sleep — evaluate every 10s (+ backoff)
            wait = 10 + self._backoff_delay("eval")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Heartbeat — periodic health logging + stats
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Log health status every 5 minutes for monitoring."""
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=300)
                break
            except asyncio.TimeoutError:
                pass

            uptime = time.monotonic() - self._start_time
            hours = int(uptime // 3600)
            minutes = int((uptime % 3600) // 60)
            logger.info(
                "heartbeat",
                uptime=f"{hours}h{minutes}m",
                tokens_discovered=self._stats["tokens_discovered"],
                tokens_scored=self._stats["tokens_scored"],
                alerts_sent=self._stats["alerts_sent"],
                errors=self._stats["errors"],
                backoff={k: v for k, v in self._backoff.items() if v > 0},
            )

    # ------------------------------------------------------------------
    # Interactive Telegram bot (non-blocking alongside scanner loops)
    # ------------------------------------------------------------------

    async def _start_telegram_bot(self) -> None:
        """Start the Telegram bot for interactive commands and buttons.

        Uses python-telegram-bot's non-blocking mode so it runs
        alongside the scanner loops in the same event loop.
        """
        if not self.cfg.telegram.bot_token:
            logger.warning("telegram_bot.disabled", reason="no bot_token")
            return

        try:
            self._telegram_app = build_telegram_app(
                self.cfg.telegram.bot_token, self,
            )
            await self._telegram_app.initialize()
            await self._telegram_app.start()
            await self._telegram_app.updater.start_polling(
                drop_pending_updates=True,
            )
            logger.info("telegram_bot.started", commands=[
                "start", "menu", "status", "stats", "gems",
                "top", "scan", "config", "help",
            ])
        except Exception as exc:
            logger.error("telegram_bot.start_failed", error=str(exc))
            self._telegram_app = None

    async def _stop_telegram_bot(self) -> None:
        """Gracefully stop the Telegram bot."""
        if self._telegram_app is None:
            return
        try:
            await self._telegram_app.updater.stop()
            await self._telegram_app.stop()
            await self._telegram_app.shutdown()
            logger.info("telegram_bot.stopped")
        except Exception as exc:
            logger.warning("telegram_bot.stop_error", error=str(exc))

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def _backoff_delay(self, loop_name: str) -> float:
        """Exponential backoff: 2^n seconds, capped at _MAX_BACKOFF."""
        n = self._backoff.get(loop_name, 0)
        if n == 0:
            return 0.0
        return min(2 ** n, self._MAX_BACKOFF)

    def _handle_signal(self, sig: signal.Signals) -> None:
        logger.info("tracker.signal", signal=sig.name)
        self._shutdown.set()

    async def _send_startup_ping(self) -> None:
        """Send a Telegram message when the bot starts."""
        if not self._notifier or not self._notifier.enabled:
            return
        try:
            from telegram import Bot
            from telegram.constants import ParseMode
            async with Bot(token=self._notifier.cfg.bot_token) as bot:
                backfill_line = ""
                if self._client and self._client.is_first_run and self.cfg.backfill.enabled:
                    backfill_line = f"📦 Backfill: last {self.cfg.backfill.hours}h\n"
                await bot.send_message(
                    chat_id=self._notifier.cfg.chat_id,
                    text=(
                        "🟢 <b>Base Sleuth Online</b>\n\n"
                        f"{backfill_line}"
                        "5 scanning loops active:\n"
                        "• Firehose (30s)\n"
                        "• Champagne (120s)\n"
                        "• Breakout (180s)\n"
                        "• Gainers (120s)\n"
                        "• Eval pipeline (10s)\n\n"
                        "🤖 Interactive bot: /menu\n"
                        "Database: PostgreSQL\n"
                        "Alerts will fire when gems are detected."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            logger.info("startup_ping.sent")
        except Exception as exc:
            logger.warning("startup_ping.failed", error=str(exc))

    async def _send_shutdown_ping(self) -> None:
        """Send a Telegram message when the bot stops gracefully."""
        if not self._notifier or not self._notifier.enabled:
            return
        uptime = time.monotonic() - self._start_time
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        try:
            from telegram import Bot
            from telegram.constants import ParseMode
            async with Bot(token=self._notifier.cfg.bot_token) as bot:
                await bot.send_message(
                    chat_id=self._notifier.cfg.chat_id,
                    text=(
                        f"🔴 <b>Base Sleuth Offline</b>\n\n"
                        f"Uptime: {hours}h {minutes}m\n"
                        f"Tokens discovered: {self._stats['tokens_discovered']}\n"
                        f"Alerts sent: {self._stats['alerts_sent']}\n"
                        f"Errors: {self._stats['errors']}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            logger.info("shutdown_ping.sent")
        except Exception as exc:
            logger.warning("shutdown_ping.failed", error=str(exc))

    async def _cleanup(self) -> None:
        if self._http:
            await self._http.aclose()
        if self._engine:
            await self._engine.dispose()

    def _configure_logging(self) -> None:
        import logging
        level = getattr(logging, self.cfg.log_level.upper(), logging.INFO)
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.StackInfoRenderer(),
                structlog.dev.set_exc_info,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.dev.ConsoleRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    config = load_config(config_path)
    tracker = Tracker(config)
    asyncio.run(tracker.start())


if __name__ == "__main__":
    main()
