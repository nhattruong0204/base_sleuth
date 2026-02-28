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
from .models import AlertOutcome, SmartWallet, Token, TokenMetrics, WalletSwap, create_engine, create_session_factory, init_db
from .notifier import TelegramNotifier
from .arkham_client import ArkhamClient, WalletTracker
from .wallet_monitor import WalletMonitor
from .nansen_listener import NansenTelethonListener

logger = structlog.get_logger(__name__)


class Tracker:
    """Top-level orchestrator that coordinates all components."""

    def __init__(self, config: AppConfig) -> None:
        self.cfg = config
        self._shutdown = asyncio.Event()
        self._start_time: float = 0.0

        # Per-loop error backoff state (consecutive failure count)
        self._backoff: dict[str, int] = {
            "poll": 0, "champagne": 0, "breakout": 0, "gainers": 0, "eval": 0, "outcome": 0,
            "wallet_sync": 0, "wallet_monitor": 0, "wallet_watch": 0, "nansen": 0,
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
        self._arkham: ArkhamClient | None = None
        self._wallet_tracker: WalletTracker | None = None
        self._wallet_monitor: WalletMonitor | None = None
        self._nansen_listener: NansenTelethonListener | None = None
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

        # Arkham Intel — smart wallet tracking
        self._arkham = ArkhamClient(self.cfg, self._http)
        self._wallet_tracker = WalletTracker(
            self.cfg, self._arkham, self._session_factory,
        )
        if self._arkham.enabled:
            await self._wallet_tracker.load_tracked_wallets()
            logger.info(
                "arkham.initialized",
                wallets_loaded=len(self._wallet_tracker.tracked_wallets),
            )
        else:
            logger.info("arkham.disabled", reason="no api_key or not enabled")

        # BaseScan wallet monitor — independent of Arkham
        self._wallet_monitor = WalletMonitor(
            self.cfg, self._http, self._session_factory,
        )
        if self.cfg.wallet_monitor.enabled:
            wm_count = await self._wallet_monitor.load_wallets()
            logger.info(
                "wallet_monitor.initialized",
                wallets_loaded=wm_count,
            )
            # Merge wallet addresses into filter's smart wallet set
            if self._filter and self._wallet_monitor.tracked_wallets:
                self._filter._smart_wallets |= self._wallet_monitor.tracked_wallets
        else:
            logger.info("wallet_monitor.disabled")

        # Nansen Telethon listener — actively monitor @NansenBot
        if self.cfg.nansen.enabled:
            self._nansen_listener = NansenTelethonListener(
                self.cfg.nansen,
                self._session_factory,
                filter_obj=self._filter,
                wallet_monitor=self._wallet_monitor,
                notifier=self._notifier,
            )
            try:
                await self._nansen_listener.start()
            except Exception as exc:
                logger.error("nansen_telethon.start_failed", error=str(exc))
                self._nansen_listener = None
        else:
            logger.info("nansen_telethon.disabled")

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

        # Run loops concurrently (+ heartbeat + wallet tracking)
        loops = [
            self._poll_loop(),
            self._champagne_loop(),
            self._breakout_loop(),
            self._gainers_loop(),
            self._eval_loop(),
            self._outcome_loop(),
            self._heartbeat_loop(),
        ]
        if self._arkham.enabled:
            loops.append(self._wallet_sync_loop())
            loops.append(self._wallet_monitor_loop())
        if self.cfg.wallet_monitor.enabled:
            loops.append(self._wallet_watch_loop())
        if self._nansen_listener and self._nansen_listener._running:
            loops.append(self._nansen_telethon_loop())

        loop_count = len(loops)
        logger.info("tracker.running", loops=loop_count, telegram_bot="active")
        await asyncio.gather(*loops)

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
    # Nansen buy query helper
    # ------------------------------------------------------------------

    async def _query_nansen_buys(
        self, session, token_address: str,
    ) -> list[dict]:
        """Return Nansen-tagged wallet buys for *token_address*."""
        from sqlalchemy import select

        stmt = (
            select(
                WalletSwap.wallet_address,
                SmartWallet.arkham_label,
                WalletSwap.usd_value,
                WalletSwap.token_symbol,
            )
            .join(SmartWallet, SmartWallet.address == WalletSwap.wallet_address)
            .where(SmartWallet.tag == "nansen")
            .where(
                WalletSwap.token_address == token_address.lower(),
            )
            .where(WalletSwap.action == "buy")
        )
        rows = (await session.execute(stmt)).all()
        return [
            {
                "wallet_address": r[0],
                "wallet_label": r[1] or r[0][:10],
                "usd_value": r[2] or 0.0,
                "token_symbol": r[3] or "?",
            }
            for r in rows
        ]

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
                            metrics = filt_result.metrics

                            # Hard MCap gate
                            min_mcap = self.cfg.filtering.min_alert_mcap_usd
                            alert_mcap = (metrics.market_cap_usd or 0) if metrics else 0
                            if alert_mcap < min_mcap:
                                logger.info(
                                    "alert.blocked.low_mcap",
                                    token=token.symbol,
                                    mcap=alert_mcap,
                                    threshold=min_mcap,
                                )
                                token.rejection_reason = (
                                    f"MCap ${alert_mcap:,.0f} below "
                                    f"${min_mcap:,.0f} gate"
                                )
                                continue

                            # Hard liquidity gate
                            min_liq = self.cfg.filtering.min_alert_liquidity_usd
                            alert_liq = (metrics.liquidity_usd or 0) if metrics else 0
                            if alert_liq < min_liq:
                                logger.info(
                                    "alert.blocked.low_liquidity",
                                    token=token.symbol,
                                    liquidity=alert_liq,
                                    threshold=min_liq,
                                )
                                token.rejection_reason = (
                                    f"Liq ${alert_liq:,.0f} below "
                                    f"${min_liq:,.0f} gate"
                                )
                                continue

                            # Duplicate symbol cooldown
                            cooldown = self.cfg.filtering.duplicate_symbol_cooldown_seconds
                            if cooldown > 0 and token.symbol:
                                dup_cutoff = datetime.now(timezone.utc) - timedelta(
                                    seconds=cooldown,
                                )
                                dup_stmt = (
                                    select(Token.id)
                                    .where(Token.symbol == token.symbol)
                                    .where(Token.alert_sent.is_(True))
                                    .where(Token.updated_at >= dup_cutoff)
                                    .where(Token.id != token.id)
                                    .limit(1)
                                )
                                dup = (await session.execute(dup_stmt)).scalar_one_or_none()
                                if dup is not None:
                                    logger.info(
                                        "alert.blocked.duplicate_symbol",
                                        token=token.symbol,
                                        existing_id=dup,
                                    )
                                    token.rejection_reason = (
                                        f"Duplicate symbol ${token.symbol} "
                                        f"(cooldown {cooldown}s)"
                                    )
                                    continue

                            # Query Nansen wallet buys for this token
                            nansen_buys = await self._query_nansen_buys(
                                session, token.contract_address,
                            )

                            sent = await self._notifier.notify(
                                token, ctx, filt_result,
                                nansen_buys=nansen_buys or None,
                            )
                            if sent:
                                token.alert_sent = True
                                self._stats["alerts_sent"] += 1
                                logger.info(
                                    "alert.sent",
                                    token=token.symbol,
                                    score=round(filt_result.final_score, 3),
                                    mcap=alert_mcap,
                                    breakout=token.is_breakout,
                                    champagne=token.is_champagne,
                                )

                                # Record alert outcome for PID loop
                                outcome = AlertOutcome(
                                    token_id=token.id,
                                    alert_score=filt_result.final_score,
                                    alert_mcap=(
                                        metrics.market_cap_usd if metrics else None
                                    ),
                                    alert_fdv=(
                                        metrics.fdv_usd if metrics else None
                                    ),
                                    alert_liq=(
                                        metrics.liquidity_usd if metrics else None
                                    ),
                                    alert_vol_1h=(
                                        metrics.volume_1h_usd if metrics else None
                                    ),
                                    alert_buys_1h=(
                                        metrics.buys_1h if metrics else None
                                    ),
                                )
                                session.add(outcome)

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
    # PID outcome tracker — re-check alerted tokens at 1h / 6h / 24h
    # ------------------------------------------------------------------

    async def _outcome_loop(self) -> None:
        """Re-check alerted tokens and classify outcomes for PID feedback.

        For each AlertOutcome that still has un-filled checkpoints:
        1. Fetch current DEX data from DexScreener
        2. Fill the appropriate checkpoint column (1h / 6h / 24h)
        3. After the final checkpoint, classify: gem / survivor / dead
        4. Run PID auto-tune on score_threshold if enough samples
        """
        pid_cfg = self.cfg.pid
        if not pid_cfg.enabled:
            logger.info("outcome_loop.disabled")
            return

        interval = pid_cfg.check_interval_seconds
        # Wait for some alerts to accumulate
        await asyncio.sleep(60)

        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    from datetime import datetime, timezone, timedelta
                    from sqlalchemy import select, or_

                    now = datetime.now(timezone.utc)
                    tol = timedelta(seconds=pid_cfg.check_tolerance_seconds)

                    # Find outcomes with pending checkpoints
                    stmt = (
                        select(AlertOutcome)
                        .where(
                            or_(
                                AlertOutcome.checked_1h_at.is_(None),
                                AlertOutcome.checked_6h_at.is_(None),
                                AlertOutcome.checked_24h_at.is_(None),
                            )
                        )
                        .order_by(AlertOutcome.alerted_at.asc())
                        .limit(30)  # Batch
                    )
                    rows = list((await session.execute(stmt)).scalars().all())

                    if rows:
                        # Collect addresses for batch DexScreener
                        token_ids = [r.token_id for r in rows]
                        token_stmt = (
                            select(Token)
                            .where(Token.id.in_(token_ids))
                        )
                        tokens = {
                            t.id: t
                            for t in (await session.execute(token_stmt)).scalars().all()
                        }

                        # Batch fetch DEX metrics
                        token_list = [t for t in tokens.values() if t.contract_address]
                        dex_map = await self._filter.fetch_dex_metrics_batch(token_list)

                        for outcome in rows:
                            token = tokens.get(outcome.token_id)
                            if not token:
                                continue
                            addr = token.contract_address.lower()
                            m = dex_map.get(addr)

                            age = (now - outcome.alerted_at).total_seconds()
                            windows = pid_cfg.check_windows  # [3600, 21600, 86400]

                            # 1h check
                            if (
                                outcome.checked_1h_at is None
                                and age >= windows[0] - tol.total_seconds()
                            ):
                                outcome.check_1h_mcap = m.market_cap_usd if m else None
                                outcome.check_1h_liq = m.liquidity_usd if m else None
                                outcome.check_1h_vol = m.volume_1h_usd if m else None
                                outcome.check_1h_buys = m.buys_1h if m else None
                                outcome.checked_1h_at = now
                                logger.debug(
                                    "outcome.check_1h",
                                    token=token.symbol,
                                    mcap=outcome.check_1h_mcap,
                                )

                            # 6h check
                            if (
                                outcome.checked_6h_at is None
                                and age >= windows[1] - tol.total_seconds()
                            ):
                                outcome.check_6h_mcap = m.market_cap_usd if m else None
                                outcome.check_6h_liq = m.liquidity_usd if m else None
                                outcome.check_6h_vol = m.volume_1h_usd if m else None
                                outcome.check_6h_buys = m.buys_1h if m else None
                                outcome.checked_6h_at = now
                                logger.debug(
                                    "outcome.check_6h",
                                    token=token.symbol,
                                    mcap=outcome.check_6h_mcap,
                                )

                            # 24h check — final → classify
                            if (
                                outcome.checked_24h_at is None
                                and age >= windows[2] - tol.total_seconds()
                            ):
                                outcome.check_24h_mcap = m.market_cap_usd if m else None
                                outcome.check_24h_liq = m.liquidity_usd if m else None
                                outcome.check_24h_vol = m.volume_1h_usd if m else None
                                outcome.check_24h_buys = m.buys_1h if m else None
                                outcome.checked_24h_at = now

                                # Classify outcome
                                outcome.outcome = self._classify_outcome(outcome, pid_cfg)
                                logger.info(
                                    "outcome.classified",
                                    token=token.symbol,
                                    outcome=outcome.outcome,
                                    alert_mcap=outcome.alert_mcap,
                                    best_mcap=max(
                                        filter(None, [
                                            outcome.check_1h_mcap,
                                            outcome.check_6h_mcap,
                                            outcome.check_24h_mcap,
                                        ]),
                                        default=0,
                                    ),
                                )

                    await session.commit()

                    # Auto-tune threshold if enabled
                    if pid_cfg.auto_tune_enabled:
                        await self._pid_auto_tune(session, pid_cfg)

                self._backoff["outcome"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["outcome"] = min(self._backoff["outcome"] + 1, 10)
                logger.exception("outcome.error", backoff=self._backoff["outcome"])

            wait = interval + self._backoff_delay("outcome")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _classify_outcome(outcome: AlertOutcome, pid_cfg) -> str:
        """Classify an alert as 'gem', 'survivor', or 'dead'."""
        alert_mcap = outcome.alert_mcap or 0
        best_mcap = max(
            filter(None, [
                outcome.check_1h_mcap,
                outcome.check_6h_mcap,
                outcome.check_24h_mcap,
            ]),
            default=0,
        )

        # Compute MCap change %
        if alert_mcap > 0 and best_mcap > 0:
            outcome.mcap_change_pct = ((best_mcap - alert_mcap) / alert_mcap) * 100
        else:
            outcome.mcap_change_pct = -100.0

        # Gem: MCap grew by configured multiplier
        if alert_mcap > 0 and best_mcap >= alert_mcap * pid_cfg.gem_mcap_multiplier:
            return "gem"

        # Dead: no DEX data at any checkpoint, or liquidity below threshold
        latest_liq = next(
            filter(None, [
                outcome.check_24h_liq,
                outcome.check_6h_liq,
                outcome.check_1h_liq,
            ]),
            None,
        )
        if latest_liq is None or latest_liq < pid_cfg.dead_liq_threshold_usd:
            return "dead"

        return "survivor"

    async def _pid_auto_tune(self, session, pid_cfg) -> None:
        """PID controller: adjust score_threshold based on outcome history.

        Includes dead-man's switch: if no alerts have been sent within
        ``no_alert_decay_hours``, the threshold auto-lowers by one step
        per cycle to prevent runaway silencing.
        """
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select, func

        step = pid_cfg.threshold_adjust_step
        current = self.cfg.filtering.score_threshold

        # ── Dead-man's switch ──
        # If no alerts sent recently, the PID has likely run away.
        # Decay the threshold toward min until alerts resume.
        decay_hours = getattr(pid_cfg, "no_alert_decay_hours", 6)
        decay_cutoff = datetime.now(timezone.utc) - timedelta(hours=decay_hours)
        recent_alerts_stmt = (
            select(func.count(AlertOutcome.id))
            .where(AlertOutcome.alerted_at >= decay_cutoff)
        )
        recent_alerts = (await session.execute(recent_alerts_stmt)).scalar() or 0

        if recent_alerts == 0 and current > pid_cfg.threshold_min:
            new_threshold = round(max(pid_cfg.threshold_min, current - step), 3)
            logger.warning(
                "pid.dead_man_decay",
                old=current,
                new=new_threshold,
                hours_without_alerts=decay_hours,
                reason="No alerts sent recently — lowering threshold to prevent runaway",
            )
            self.cfg.filtering.score_threshold = new_threshold
            return

        cutoff = datetime.now(timezone.utc) - timedelta(hours=pid_cfg.lookback_hours)

        # Count classified outcomes in the lookback window
        total_stmt = (
            select(func.count(AlertOutcome.id))
            .where(AlertOutcome.outcome.isnot(None))
            .where(AlertOutcome.alerted_at >= cutoff)
        )
        total = (await session.execute(total_stmt)).scalar() or 0

        if total < pid_cfg.min_samples:
            return  # Not enough data yet

        gem_stmt = (
            select(func.count(AlertOutcome.id))
            .where(AlertOutcome.outcome == "gem")
            .where(AlertOutcome.alerted_at >= cutoff)
        )
        gems = (await session.execute(gem_stmt)).scalar() or 0

        dead_stmt = (
            select(func.count(AlertOutcome.id))
            .where(AlertOutcome.outcome == "dead")
            .where(AlertOutcome.alerted_at >= cutoff)
        )
        deads = (await session.execute(dead_stmt)).scalar() or 0

        gem_rate = gems / total
        dead_rate = deads / total

        # PID-style adjustment:
        # - Too many dead → raise threshold (proportional to dead_rate)
        # - Gem rate above target → lower threshold
        # - Moderate zone → small downward nudge to avoid stagnation
        adjustment = 0.0

        if dead_rate > 0.60:
            # Aggressive: lots of trash getting through
            adjustment = step * 2
        elif dead_rate > 0.40:
            adjustment = step
        elif gem_rate >= pid_cfg.target_gem_rate:
            # Meeting target — lower threshold to find more gems
            adjustment = -step
        elif dead_rate < 0.30 and current > pid_cfg.threshold_min:
            # Low dead rate but low gem rate too — cautiously lower
            adjustment = -step * 0.5

        new_threshold = round(
            max(pid_cfg.threshold_min, min(pid_cfg.threshold_max, current + adjustment)),
            3,
        )

        if new_threshold != current:
            self.cfg.filtering.score_threshold = new_threshold
            logger.info(
                "pid.threshold_adjusted",
                old=current,
                new=new_threshold,
                gem_rate=round(gem_rate, 3),
                dead_rate=round(dead_rate, 3),
                total_outcomes=total,
            )
        else:
            logger.debug(
                "pid.no_change",
                threshold=current,
                gem_rate=round(gem_rate, 3),
                dead_rate=round(dead_rate, 3),
            )

    # ------------------------------------------------------------------
    # Wallet sync loop — periodic Arkham wallet refresh
    # ------------------------------------------------------------------

    async def _wallet_sync_loop(self) -> None:
        """Periodically sync smart wallets from Arkham Intel.

        Fetches fomo-tagged wallets, analyzes performance, and updates
        the SmartWallet table. Default: every 3600s (1 hour).
        """
        interval = self.cfg.arkham.wallet_sync_interval_seconds

        # Run first sync immediately
        while not self._shutdown.is_set():
            try:
                count = await self._wallet_tracker.sync_wallets()
                logger.info(
                    "wallet_sync.done",
                    profitable=count,
                    tracked=len(self._wallet_tracker.tracked_wallets),
                )
                # Pass wallet set to filter for Stage 4
                if self._filter:
                    self._filter._smart_wallets = self._wallet_tracker.tracked_wallets
                self._backoff["wallet_sync"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["wallet_sync"] = min(self._backoff["wallet_sync"] + 1, 10)
                logger.exception("wallet_sync.error", backoff=self._backoff["wallet_sync"])

            wait = interval + self._backoff_delay("wallet_sync")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Wallet monitor loop — detect smart wallet buys + conviction
    # ------------------------------------------------------------------

    async def _wallet_monitor_loop(self) -> None:
        """Poll Arkham for recent swaps by tracked wallets.

        For each new buy:
        1. Send wallet buy alert via Telegram
        2. Check if token exists in our DB (conviction signal)
        3. Send conviction alert if match found

        Default: every 60s (heavy endpoint).
        """
        interval = self.cfg.arkham.swap_poll_interval_seconds
        # Initial delay to let wallet sync populate first
        await asyncio.sleep(90)

        while not self._shutdown.is_set():
            try:
                if self._wallet_tracker.tracked_wallets:
                    # Step 1: Check for new swaps
                    new_buys = await self._wallet_tracker.check_swaps()

                    # Step 2: Send wallet buy alerts
                    for buy in new_buys:
                        wallet = None
                        async with self._session_factory() as session:
                            from sqlalchemy import select
                            from .models import SmartWallet
                            stmt = (
                                select(SmartWallet)
                                .where(SmartWallet.address == buy["wallet_address"])
                                .limit(1)
                            )
                            wallet = (await session.execute(stmt)).scalar_one_or_none()

                        await self._notifier.notify_wallet_buy(buy, wallet)

                    # Step 3: Detect and alert convictions
                    if self.cfg.arkham.conviction_enabled and new_buys:
                        convictions = await self._wallet_tracker.detect_convictions(new_buys)
                        for conv in convictions:
                            await self._notifier.notify_conviction(conv)

                self._backoff["wallet_monitor"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["wallet_monitor"] = min(self._backoff["wallet_monitor"] + 1, 10)
                logger.exception("wallet_monitor.error", backoff=self._backoff["wallet_monitor"])

            wait = interval + self._backoff_delay("wallet_monitor")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # BaseScan wallet watch loop — on-chain buy detection
    # ------------------------------------------------------------------

    async def _wallet_watch_loop(self) -> None:
        """Poll BaseScan for buys by tracked wallets (independent of Arkham).

        Uses the BaseScan ERC-20 tokentx endpoint to detect when any
        watched wallet receives tokens from a DEX router.

        For each new buy:
        1. Record in wallet_swaps table (dedup by tx_hash)
        2. Send wallet buy alert via Telegram
        3. Check if token is in our DB → conviction signal + alert

        Default: every 60s, configurable via wallet_monitor.poll_interval_seconds.
        """
        interval = self.cfg.wallet_monitor.poll_interval_seconds
        # Initial delay to let startup settle
        await asyncio.sleep(30)

        while not self._shutdown.is_set():
            try:
                if self._wallet_monitor and self._wallet_monitor.tracked_wallets:
                    # Step 1: Poll for new buys
                    new_buys = await self._wallet_monitor.poll_transfers()

                    # Step 2: Send wallet buy alerts
                    for buy in new_buys:
                        wallet = None
                        async with self._session_factory() as session:
                            from sqlalchemy import select as sa_select
                            from .models import SmartWallet as SW
                            stmt = (
                                sa_select(SW)
                                .where(SW.address == buy["wallet_address"])
                                .limit(1)
                            )
                            wallet = (await session.execute(stmt)).scalar_one_or_none()

                        await self._notifier.notify_wallet_buy(buy, wallet)
                        self._stats["alerts_sent"] += 1

                    # Step 3: Detect and alert convictions
                    if new_buys:
                        convictions = await self._wallet_monitor.detect_convictions(new_buys)
                        for conv in convictions:
                            await self._notifier.notify_conviction(conv)
                            self._stats["alerts_sent"] += 1

                self._backoff["wallet_watch"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["wallet_watch"] = min(self._backoff["wallet_watch"] + 1, 10)
                logger.exception("wallet_watch.error", backoff=self._backoff["wallet_watch"])

            wait = interval + self._backoff_delay("wallet_watch")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Nansen Telethon loop — runs Telethon event loop alongside others
    # ------------------------------------------------------------------

    async def _nansen_telethon_loop(self) -> None:
        """Run the Telethon client event loop for NansenBot listening.

        Telethon needs its own ``run_until_disconnected`` which we wrap
        in a task that respects the shutdown event.
        """
        if not self._nansen_listener or not self._nansen_listener._running:
            return

        logger.info("nansen_telethon.loop_started")
        try:
            # run_until_disconnected blocks until the client is disconnected.
            # We race it against the shutdown event so a graceful stop works.
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(self._nansen_listener.run_until_disconnected()),
                    asyncio.create_task(self._shutdown.wait()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
        except Exception:
            logger.exception("nansen_telethon.loop_error")
        finally:
            await self._nansen_listener.stop()
            logger.info(
                "nansen_telethon.loop_stopped",
                stats=self._nansen_listener.stats,
            )

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
                "top", "scan", "config", "help", "realpnl", "analysis",
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

                wallet_line = ""
                if self._arkham and self._arkham.enabled:
                    wcount = len(self._wallet_tracker.tracked_wallets)
                    wallet_line = f"👛 Wallet tracking: {wcount} wallets\n"

                nansen_line = ""
                if self._nansen_listener and self._nansen_listener._running:
                    nansen_line = "🔍 Nansen listener: active (Telethon)\n"

                await bot.send_message(
                    chat_id=self._notifier.cfg.chat_id,
                    text=(
                        "🟢 <b>Base Sleuth Online</b>\n\n"
                        f"{backfill_line}"
                        f"{wallet_line}"
                        f"{nansen_line}"
                        "Scanning loops active:\n"
                        "• Firehose (30s)\n"
                        "• Champagne (120s)\n"
                        "• Breakout (180s)\n"
                        "• Gainers (120s)\n"
                        "• Eval pipeline (10s)\n"
                        "• PID outcome tracker (5m)\n"
                        "• Wallet sync (1h)\n"
                        "• Wallet monitor (60s)\n\n"
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
        if self._nansen_listener:
            await self._nansen_listener.stop()
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
