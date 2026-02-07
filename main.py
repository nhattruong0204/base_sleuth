"""
Main entrypoint: async event loop orchestrating all components.

Flow:
1. Poll Clanker API for new tokens (every N seconds)
2. For each new token:
   a. Run Stage 1 filter (instant reject)
   b. Queue context resolution (async, non-blocking)
   c. Save to DB
3. Periodically re-evaluate tokens that passed Stage 1:
   a. Fetch metrics (placeholder — needs DEX data integration)
   b. Run Stages 2-4
   c. Compute final score
   d. Notify if score >= threshold
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import structlog

from .clanker_client import ClankerClient
from .config import load_config
from .context_resolver import ContextResolver
from .filters import TokenFilter
from .models import Token, init_db, get_session_factory
from .notifier import TelegramNotifier


def setup_logging(config):
    """Configure structlog."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if not config.logging.json_format
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


logger = structlog.get_logger(__name__)


class Tracker:
    """Main orchestrator."""

    def __init__(self, config_path: str = "config.yaml"):
        self.config = load_config(config_path)
        self.clanker = ClankerClient(self.config)
        self.resolver = ContextResolver(self.config)
        self.filter = TokenFilter(self.config.filtering)
        self.notifier = TelegramNotifier(self.config.telegram)
        self._shutdown = asyncio.Event()

        # Tokens pending Stage 2+ evaluation
        self._pending_evaluation: dict[str, Token] = {}

    async def start(self):
        """Initialize DB and start polling loops."""
        setup_logging(self.config)
        logger.info("tracker_starting", config_path="config.yaml")

        # Ensure directories exist
        Path(self.config.database.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.config.logging.file).parent.mkdir(parents=True, exist_ok=True)

        await init_db(self.config)
        logger.info("database_initialized", backend=self.config.database.backend)

        # Run concurrent loops
        try:
            await asyncio.gather(
                self._poll_loop(),
                self._evaluation_loop(),
                self._wait_for_shutdown(),
            )
        finally:
            await self._cleanup()

    async def _poll_loop(self):
        """Continuously poll Clanker API for new tokens."""
        interval = self.config.clanker.poll_interval_seconds
        logger.info("poll_loop_started", interval=interval)

        while not self._shutdown.is_set():
            try:
                new_tokens = await self.clanker.poll_new_tokens()

                for token in new_tokens:
                    # Stage 1: instant filter
                    result = self.filter.stage1_instant_filter(token)

                    if result.stage1_pass:
                        # Save to DB
                        await self.clanker.save_tokens([token])

                        # Queue for context resolution (fire-and-forget)
                        asyncio.create_task(
                            self._resolve_context_safe(token)
                        )

                        # Queue for Stage 2+ evaluation
                        self._pending_evaluation[token.contract_address] = token

                        logger.info(
                            "token_passed_stage1",
                            symbol=token.symbol,
                            contract=token.contract_address[:10] + "...",
                            bankr=token.is_bankr_origin,
                        )
                    else:
                        # Still save rejected tokens for record-keeping
                        token.quality_score = 0.0
                        await self.clanker.save_tokens([token])

            except Exception as e:
                logger.error("poll_loop_error", error=str(e))

            # Wait for next poll cycle
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=interval
                )
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Normal — continue polling

    async def _evaluation_loop(self):
        """
        Periodically re-evaluate pending tokens with fresh metrics.

        In production, this would:
        1. Fetch live metrics from DexScreener/DEXTools API
        2. Check onchain holder data
        3. Cross-reference smart money wallets

        For now, this runs context-based scoring (Stages 3-4).
        Metrics integration (Stage 2) is a TODO that requires
        DEX API integration (DexScreener, defined, etc.).
        """
        logger.info("evaluation_loop_started")

        while not self._shutdown.is_set():
            try:
                # Process pending tokens
                to_evaluate = list(self._pending_evaluation.values())
                self._pending_evaluation.clear()

                session_factory = get_session_factory()

                for token in to_evaluate:
                    # Get context if resolved
                    context = None
                    async with session_factory() as session:
                        from sqlalchemy import select
                        from .models import TokenContext
                        stmt = select(TokenContext).where(
                            TokenContext.contract_address == token.contract_address
                        )
                        row = await session.execute(stmt)
                        context = row.scalar_one_or_none()

                    # Run filter pipeline (Stage 2 placeholder, Stage 3-4 real)
                    result = self.filter.stage1_instant_filter(token)
                    if result.stage1_pass:
                        # Stage 2: metrics (TODO — integrate DEX API)
                        # result = self.filter.stage2_early_metrics(result, metrics)

                        # Stage 3: smart money (TODO — need buyer list from onchain)
                        # result = self.filter.stage3_smart_money(result, buyer_addresses)

                        # Stage 4: context quality
                        result = self.filter.stage4_context_quality(result, context, token)

                        # Compute final score (currently context-only until metrics added)
                        result = self.filter.compute_final_score(result)

                        # Update DB
                        async with session_factory() as session:
                            async with session.begin():
                                from sqlalchemy import update
                                stmt = (
                                    update(Token)
                                    .where(Token.contract_address == token.contract_address)
                                    .values(quality_score=result.final_score)
                                )
                                await session.execute(stmt)

                        # Notify if gem
                        if result.final_score >= self.config.telegram.min_score_to_notify:
                            result.is_gem = True
                            await self.notifier.notify(token, result, context)

            except Exception as e:
                logger.error("evaluation_loop_error", error=str(e))

            # Run every 60 seconds
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=60)
                break
            except asyncio.TimeoutError:
                pass

    async def _resolve_context_safe(self, token: Token):
        """Wrapper to catch exceptions in context resolution tasks."""
        try:
            await self.resolver.resolve(token)
        except Exception as e:
            logger.warning(
                "context_resolution_failed",
                symbol=token.symbol,
                error=str(e),
            )

    async def _wait_for_shutdown(self):
        """Wait for SIGINT/SIGTERM."""
        loop = asyncio.get_running_loop()

        def _signal_handler():
            logger.info("shutdown_signal_received")
            self._shutdown.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        await self._shutdown.wait()

    async def _cleanup(self):
        """Graceful shutdown."""
        logger.info("cleaning_up")
        await self.clanker.close()
        await self.resolver.close()
        await self.notifier.close()
        logger.info("tracker_stopped")


def main():
    """CLI entrypoint."""
    import argparse

    parser = argparse.ArgumentParser(description="Clanker Token Tracker")
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to config YAML (default: config.yaml)",
    )
    args = parser.parse_args()

    tracker = Tracker(config_path=args.config)
    asyncio.run(tracker.start())


if __name__ == "__main__":
    main()
