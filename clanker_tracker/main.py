"""Async orchestrator for the Clanker Token Tracker.

Runs three concurrent loops:
- Firehose loop   : streams ALL new tokens via cursor pagination (30s poll)
- Champagne loop  : scans champagne-tagged curated gems (120s poll)
- Eval loop       : pre-filters → batch DexScreener → scores → alerts

Handles graceful shutdown via SIGINT / SIGTERM.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import httpx
import structlog

from .clanker_client import ClankerClient
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

        # Core components (initialised in .start())
        self._http: httpx.AsyncClient | None = None
        self._engine = None
        self._session_factory = None
        self._client: ClankerClient | None = None
        self._resolver: ContextResolver | None = None
        self._filter: TokenFilter | None = None
        self._notifier: TelegramNotifier | None = None

    async def start(self) -> None:
        """Initialise resources and run the main loops."""
        self._configure_logging()
        logger.info("tracker.starting")

        # HTTP client
        self._http = httpx.AsyncClient(
            headers={"User-Agent": "ClankerTracker/0.1"},
            follow_redirects=True,
            timeout=20.0,
        )

        # Database
        self._engine = create_engine(self.cfg.database.url, echo=self.cfg.database.echo)
        await init_db(self._engine)
        self._session_factory = create_session_factory(self._engine)

        # Components
        self._client = ClankerClient(self.cfg, self._http)
        self._resolver = ContextResolver(self.cfg, self._http)
        self._filter = TokenFilter(self.cfg, self._http)
        self._notifier = TelegramNotifier(self.cfg)

        # Watermark from DB
        async with self._session_factory() as session:
            await self._client.init_watermark(session)

        # Signal handling
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_signal, sig)

        # Run loops concurrently
        logger.info("tracker.running")
        await asyncio.gather(
            self._poll_loop(),
            self._champagne_loop(),
            self._eval_loop(),
        )

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
                        logger.info(
                            "poll.batch",
                            count=len(new_tokens),
                            bankr=bankr_ct,
                            other=len(new_tokens) - bankr_ct,
                        )
            except Exception:
                logger.exception("poll.error")

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
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
                        logger.info(
                            "champagne.found",
                            count=len(gems),
                            names=[t.name for t in gems],
                        )
            except Exception:
                logger.exception("champagne.error")

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
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

                        # Notify if above threshold
                        if not filt_result.rejected and not token.alert_sent:
                            sent = await self._notifier.notify(
                                token, ctx, filt_result,
                            )
                            if sent:
                                token.alert_sent = True

                    await session.commit()

            except Exception:
                logger.exception("eval.error")

            # Interruptible sleep — evaluate every 10s
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=10)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def _handle_signal(self, sig: signal.Signals) -> None:
        logger.info("tracker.signal", signal=sig.name)
        self._shutdown.set()

    async def _cleanup(self) -> None:
        if self._http:
            await self._http.aclose()
        if self._engine:
            await self._engine.dispose()

    def _configure_logging(self) -> None:
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.StackInfoRenderer(),
                structlog.dev.set_exc_info,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.dev.ConsoleRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(
                structlog.get_level_from_name(self.cfg.log_level),
            ),
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
