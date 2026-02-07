"""Async orchestrator for the Clanker Token Tracker.

Runs two concurrent loops:
- Poll loop    : fetches new tokens from Clanker API every N seconds
- Eval loop    : scores unprocessed tokens and sends Telegram alerts

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
            self._eval_loop(),
        )

        await self._cleanup()
        logger.info("tracker.stopped")

    # ------------------------------------------------------------------
    # Poll loop — discover new tokens
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        interval = self.cfg.clanker.poll_interval_seconds
        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    new_tokens = await self._client.poll(session)
                    await session.commit()
                    if new_tokens:
                        logger.info("poll.batch", count=len(new_tokens))
            except Exception:
                logger.exception("poll.error")

            # Interruptible sleep
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
                break  # shutdown signalled
            except asyncio.TimeoutError:
                pass  # normal timeout → next iteration

    # ------------------------------------------------------------------
    # Eval loop — score & notify
    # ------------------------------------------------------------------

    async def _eval_loop(self) -> None:
        """Process tokens that have not been scored yet."""
        # Small initial delay so the poll loop can populate the DB first
        await asyncio.sleep(5)

        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    from sqlalchemy import select

                    stmt = (
                        select(Token)
                        .where(Token.quality_score.is_(None))
                        .order_by(Token.discovered_at.asc())
                        .limit(20)
                    )
                    result = await session.execute(stmt)
                    tokens = list(result.scalars().all())

                    for token in tokens:
                        if self._shutdown.is_set():
                            break

                        # Resolve context
                        ctx = await self._resolver.resolve(token, session)

                        # Score
                        filt_result = await self._filter.evaluate(token, session)

                        # Notify if above threshold
                        if not filt_result.rejected and not token.alert_sent:
                            sent = await self._notifier.notify(token, ctx, filt_result)
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
