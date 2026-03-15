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
from .models import AlertOutcome, PaperPosition, SmartWallet, Token, TokenMetrics, WalletSwap, create_engine, create_session_factory, init_db
from .notifier import TelegramNotifier
from .arkham_client import ArkhamClient, TokenFlowMonitor, WalletTracker
from .wallet_monitor import WalletMonitor
from .nansen_listener import NansenTelethonListener
from .binance_client import BinanceSkillsClient, BinanceTrendingScanner

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
            "champagne_eval": 0, "paper_trading": 0, "multi_conviction": 0,
            "binance_trending": 0,
            "token_flow": 0,
            "portfolio_watch": 0,
            "milestone_tracker": 0,
            "gate_pending": 0,
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
        self._binance: BinanceSkillsClient | None = None
        self._binance_scanner: BinanceTrendingScanner | None = None
        self._flow_monitor: TokenFlowMonitor | None = None
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

        # Binance Skills Hub — trending scanner + audit + enrichment
        self._binance = BinanceSkillsClient(self.cfg, self._http)
        if self._binance.enabled:
            self._binance_scanner = BinanceTrendingScanner(
                self.cfg, self._binance, self._http,
            )
            self._filter.set_binance_client(self._binance)
            logger.info("binance_skills.initialized", audit=self.cfg.binance_skills.audit_enabled, enrich=self.cfg.binance_skills.enrich_enabled)
        else:
            logger.info("binance_skills.disabled")

        # Arkham Intel — smart wallet tracking
        self._arkham = ArkhamClient(self.cfg, self._http)
        self._wallet_tracker = WalletTracker(
            self.cfg, self._arkham, self._session_factory,
        )
        if self._arkham.enabled:
            await self._wallet_tracker.load_tracked_wallets()
            # Inject Arkham client into filter for holder + deployer scoring
            self._filter.set_arkham_client(self._arkham)
            # Token flow monitor for dump/accumulation detection
            if self.cfg.arkham.flow_monitoring_enabled:
                self._flow_monitor = TokenFlowMonitor(
                    self.cfg, self._arkham, self._session_factory,
                )
            logger.info(
                "arkham.initialized",
                wallets_loaded=len(self._wallet_tracker.tracked_wallets),
                holder_intel=self.cfg.arkham.holder_intel_enabled,
                deployer_profiling=self.cfg.arkham.deployer_profiling_enabled,
                flow_monitoring=self.cfg.arkham.flow_monitoring_enabled,
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
            if self._flow_monitor:
                loops.append(self._token_flow_loop())
            if self.cfg.arkham.portfolio_watch_enabled:
                loops.append(self._portfolio_watch_loop())
        if self.cfg.wallet_monitor.enabled:
            loops.append(self._wallet_watch_loop())
        if self._nansen_listener and self._nansen_listener._running:
            loops.append(self._nansen_telethon_loop())

        # New v0.7 loops
        if self.cfg.champagne_eval.enabled:
            loops.append(self._champagne_eval_loop())
        if self.cfg.paper_trading.enabled:
            loops.append(self._paper_trading_loop())
        if self.cfg.multi_conviction.enabled:
            loops.append(self._multi_conviction_loop())

        # v0.8 — Binance Skills Hub trending scanner
        if self._binance and self._binance.enabled:
            loops.append(self._binance_trending_loop())

        # v1.0 — Milestone tracker (ATH + multiplier notifications + dead cleanup)
        if self.cfg.milestone_tracker.enabled:
            loops.append(self._milestone_tracker_loop())

        # v1.1 — Gate-pending re-scan (catch tokens that grow past MCap/Liq gates)
        if self.cfg.gate_pending.enabled:
            loops.append(self._gate_pending_loop())

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
    # Binance trending scanner loop — Base tokens trending on Binance
    # ------------------------------------------------------------------

    async def _binance_trending_loop(self) -> None:
        """Periodically fetch trending / top-search / socially hyped tokens
        from Binance Skills Hub and ingest new ones into the DB.

        Uses three Binance data sources:
        1. Unified Token Rank (trending rank_type=10, top search rank_type=11)
        2. Social Hype Leaderboard (sentiment + social buzz)

        Tokens found here enter the eval pipeline for full scoring,
        including Binance security audit and enriched holder data.
        """
        if not self._binance or not self._binance.enabled:
            logger.info("binance_trending.disabled")
            return

        interval = self.cfg.binance_skills.trending_poll_interval_seconds
        # Initial delay to let firehose + breakout populate first
        await asyncio.sleep(60)

        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    new_tokens = await self._binance_scanner.scan(session)
                    await session.commit()
                    if new_tokens:
                        self._stats["tokens_discovered"] += len(new_tokens)
                        logger.info(
                            "binance_trending.batch",
                            count=len(new_tokens),
                            names=[t.symbol or t.name for t in new_tokens],
                        )
                self._backoff["binance_trending"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["binance_trending"] = min(
                    self._backoff["binance_trending"] + 1, 10,
                )
                logger.exception(
                    "binance_trending.error",
                    backoff=self._backoff["binance_trending"],
                )

            wait = interval + self._backoff_delay("binance_trending")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Token flow monitor loop — detect dump/accumulation patterns
    # ------------------------------------------------------------------

    async def _token_flow_loop(self) -> None:
        """Periodically check token flows for alerted tokens with open positions.

        Uses Arkham /token/top_flow (heavy endpoint, 1 req/sec) to detect:
        - Dump warnings: large outflows from known whales/funds
        - Accumulation signals: sustained inflows from smart money

        Default: every 300s (5 min), configurable via arkham.flow_poll_interval_seconds.
        """
        if not self._flow_monitor:
            return

        interval = self.cfg.arkham.flow_poll_interval_seconds
        # Initial delay to let eval pipeline generate some alerts first
        await asyncio.sleep(120)

        while not self._shutdown.is_set():
            try:
                events = await self._flow_monitor.check_flows(max_tokens=5)
                for event in events:
                    await self._notifier.notify_flow_alert(event)
                if events:
                    logger.info(
                        "token_flow.events",
                        count=len(events),
                        types=[e.get("type") for e in events],
                    )
                self._backoff["token_flow"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["token_flow"] = min(
                    self._backoff["token_flow"] + 1, 10,
                )
                logger.exception(
                    "token_flow.error",
                    backoff=self._backoff["token_flow"],
                )

            wait = interval + self._backoff_delay("token_flow")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Portfolio watch loop — monitor Tier 1 wallet holdings
    # ------------------------------------------------------------------

    async def _portfolio_watch_loop(self) -> None:
        """Periodically check portfolios of top-tier tracked wallets.

        Uses Arkham /balances/address to detect new Base token positions
        taken by our highest-confidence wallets.

        Default: every 600s (10 min), configurable via arkham.portfolio_poll_interval_seconds.
        """
        interval = self.cfg.arkham.portfolio_poll_interval_seconds
        min_position = self.cfg.arkham.portfolio_min_position_usd
        # Initial delay — wait for wallet sync
        await asyncio.sleep(180)

        # Track known positions to detect NEW entries only
        _seen_positions: dict[str, set[str]] = {}  # wallet -> set of token addresses

        while not self._shutdown.is_set():
            try:
                if self._wallet_tracker and self._wallet_tracker.tracked_wallets:
                    # Check top 30 wallets per cycle (20 req/sec limit, 600s interval)
                    top_wallets = list(self._wallet_tracker.tracked_wallets)[:30]
                    for wallet_addr in top_wallets:
                        portfolio = await self._arkham.fetch_wallet_portfolio(
                            wallet_addr, chain="base",
                        )
                        if not portfolio:
                            continue

                        prev = _seen_positions.get(wallet_addr, set())
                        current_tokens = set()

                        for holding in portfolio:
                            token_addr = (holding.get("token", {}).get("address") or "").lower()
                            usd_val = holding.get("usdValue") or 0.0
                            if not token_addr or usd_val < min_position:
                                continue
                            current_tokens.add(token_addr)

                            # New position detected
                            if token_addr not in prev:
                                token_name = holding.get("token", {}).get("symbol") or "?"
                                logger.info(
                                    "portfolio_watch.new_position",
                                    wallet=wallet_addr[:10],
                                    token=token_name,
                                    usd=round(usd_val, 2),
                                )
                                # Check if we already track this token
                                async with self._session_factory() as session:
                                    from sqlalchemy import select
                                    stmt = (
                                        select(Token)
                                        .where(Token.contract_address == token_addr)
                                        .limit(1)
                                    )
                                    existing = (await session.execute(stmt)).scalar_one_or_none()
                                    if existing:
                                        # Conviction — smart wallet holds our tracked token
                                        conv = {
                                            "wallet_address": wallet_addr,
                                            "wallet_label": wallet_addr[:10],
                                            "token_name": existing.name,
                                            "token_symbol": existing.symbol,
                                            "token_address": token_addr,
                                            "usd_value": usd_val,
                                            "conviction_type": "portfolio_hold",
                                        }
                                        await self._notifier.notify_conviction(conv)

                        _seen_positions[wallet_addr] = current_tokens

                self._backoff["portfolio_watch"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["portfolio_watch"] = min(
                    self._backoff["portfolio_watch"] + 1, 10,
                )
                logger.exception(
                    "portfolio_watch.error",
                    backoff=self._backoff["portfolio_watch"],
                )

            wait = interval + self._backoff_delay("portfolio_watch")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Milestone tracker loop — ATH / multiplier notifications + dead cleanup
    # ------------------------------------------------------------------

    async def _milestone_tracker_loop(self) -> None:
        """Scan all alerted, non-dead tokens for milestones every 5 min.

        For each token with an AlertOutcome:
        1. Fetch live DEX data (batch, 30 per call)
        2. Compare current MCap with alert-time MCap/FDV
        3. Detect:
           - New ATH (all-time high MCap)
           - New multiplier milestone (2x, 3x, 4x, ... of alert FDV)
        4. Mark dead tokens (liq < $200 or mcap < $500 for 24h+)
        5. Clean up: stop tracking tokens older than max_token_age_days
        """
        cfg = self.cfg.milestone_tracker
        interval = cfg.scan_interval_seconds

        # Initial delay — let eval pipeline generate some alerts first
        await asyncio.sleep(120)

        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    from datetime import datetime, timezone, timedelta
                    from sqlalchemy import select
                    import math

                    now = datetime.now(timezone.utc)
                    max_age_cutoff = now - timedelta(days=cfg.max_token_age_days)

                    # Fetch all alerted, non-dead tokens with outcomes
                    stmt = (
                        select(AlertOutcome, Token)
                        .join(Token, Token.id == AlertOutcome.token_id)
                        .where(Token.alert_sent.is_(True))
                        .where(Token.is_dead.is_not(True))
                        .where(AlertOutcome.alerted_at >= max_age_cutoff)
                        .order_by(AlertOutcome.alerted_at.desc())
                    )
                    rows = (await session.execute(stmt)).all()

                    if not rows:
                        self._backoff["milestone_tracker"] = 0
                        try:
                            await asyncio.wait_for(
                                self._shutdown.wait(), timeout=interval,
                            )
                            break
                        except asyncio.TimeoutError:
                            continue

                    # Batch fetch DEX data (30 per call)
                    all_tokens = [row[1] for row in rows]
                    dex_map: dict[str, object] = {}
                    for i in range(0, len(all_tokens), cfg.batch_size):
                        batch = all_tokens[i:i + cfg.batch_size]
                        batch_dex = await self._filter.fetch_dex_metrics_batch(batch)
                        dex_map.update(batch_dex)
                        # Respect DexScreener rate limits
                        if i + cfg.batch_size < len(all_tokens):
                            await asyncio.sleep(1.0)

                    milestones_sent = 0
                    dead_marked = 0

                    for outcome, token in rows:
                        if self._shutdown.is_set():
                            break

                        addr = token.contract_address.lower()
                        m = dex_map.get(addr)

                        current_mcap = m.market_cap_usd if m else None
                        current_liq = m.liquidity_usd if m else None
                        current_fdv = m.fdv_usd if m else None
                        current_price = m.price_usd if m else None

                        # ── Dead token detection ──
                        if (
                            current_mcap is not None
                            and current_liq is not None
                            and (current_liq < cfg.dead_liq_threshold_usd
                                 or current_mcap < cfg.dead_mcap_threshold_usd)
                        ):
                            if token.dead_since is None:
                                # First time below threshold — start countdown
                                token.dead_since = now
                            elif (now - token.dead_since).total_seconds() >= cfg.dead_confirmation_hours * 3600:
                                # Confirmed dead
                                token.is_dead = True
                                dead_marked += 1
                                alert_ref = outcome.alert_fdv or outcome.alert_mcap or 0
                                days_since = (now - outcome.alerted_at).total_seconds() / 86400
                                logger.info(
                                    "milestone.dead",
                                    token=token.symbol,
                                    mcap=current_mcap,
                                    liq=current_liq,
                                    days=round(days_since, 1),
                                )
                                await self._notifier.notify_dead_token({
                                    "token_name": token.name,
                                    "token_symbol": token.symbol,
                                    "token_address": token.contract_address,
                                    "alert_mcap": alert_ref,
                                    "current_mcap": current_mcap or 0,
                                    "current_liq": current_liq or 0,
                                    "days_since_alert": days_since,
                                }, reply_to_message_id=outcome.alert_message_id)
                                continue
                        else:
                            # Token recovered — reset dead countdown
                            if token.dead_since is not None:
                                token.dead_since = None

                        # ── No DEX data — skip milestones ──
                        if current_mcap is None or current_mcap <= 0:
                            continue

                        # Reference value: prefer alert FDV, fallback to alert MCap
                        alert_ref = outcome.alert_fdv or outcome.alert_mcap
                        if not alert_ref or alert_ref <= 0:
                            continue

                        # ── ATH tracking ──
                        prev_ath = outcome.ath_mcap or 0
                        if current_mcap > prev_ath:
                            outcome.ath_mcap = current_mcap

                            # Notify ATH (with cooldown)
                            if cfg.notify_ath and current_mcap > prev_ath * 1.1:
                                # Only notify if >10% above previous ATH (avoid spam)
                                last_notified = outcome.milestone_notified_at
                                cooldown_ok = (
                                    last_notified is None
                                    or (now - last_notified).total_seconds() >= cfg.ath_cooldown_seconds
                                )
                                if cooldown_ok and prev_ath > 0:
                                    pnl_pct = ((current_mcap / alert_ref) - 1) * 100
                                    await self._notifier.notify_milestone({
                                        "event_type": "ath",
                                        "token_name": token.name,
                                        "token_symbol": token.symbol,
                                        "token_address": token.contract_address,
                                        "alert_mcap": outcome.alert_mcap,
                                        "alert_fdv": outcome.alert_fdv,
                                        "current_mcap": current_mcap,
                                        "current_fdv": current_fdv,
                                        "current_liq": current_liq,
                                        "current_price": current_price,
                                        "ath_mcap": current_mcap,
                                        "multiplier": int(current_mcap / alert_ref),
                                        "pnl_pct": pnl_pct,
                                    }, reply_to_message_id=outcome.alert_message_id)
                                    outcome.milestone_notified_at = now
                                    milestones_sent += 1

                        # ── Multiplier milestone tracking ──
                        if cfg.notify_multiplier:
                            current_x = int(current_mcap / alert_ref)
                            prev_x = outcome.last_milestone_x or 0

                            if current_x >= cfg.min_multiplier_notify and current_x > prev_x:
                                pnl_pct = ((current_mcap / alert_ref) - 1) * 100
                                await self._notifier.notify_milestone({
                                    "event_type": "multiplier",
                                    "token_name": token.name,
                                    "token_symbol": token.symbol,
                                    "token_address": token.contract_address,
                                    "alert_mcap": outcome.alert_mcap,
                                    "alert_fdv": outcome.alert_fdv,
                                    "current_mcap": current_mcap,
                                    "current_fdv": current_fdv,
                                    "current_liq": current_liq,
                                    "current_price": current_price,
                                    "ath_mcap": outcome.ath_mcap,
                                    "multiplier": current_x,
                                    "pnl_pct": pnl_pct,
                                }, reply_to_message_id=outcome.alert_message_id)
                                outcome.last_milestone_x = current_x
                                outcome.milestone_notified_at = now
                                milestones_sent += 1
                                logger.info(
                                    "milestone.multiplier",
                                    token=token.symbol,
                                    x=current_x,
                                    mcap=current_mcap,
                                    alert_ref=alert_ref,
                                )

                    await session.commit()

                    if milestones_sent > 0 or dead_marked > 0:
                        logger.info(
                            "milestone_tracker.cycle",
                            tokens_checked=len(rows),
                            milestones=milestones_sent,
                            dead=dead_marked,
                        )

                self._backoff["milestone_tracker"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["milestone_tracker"] = min(
                    self._backoff["milestone_tracker"] + 1, 10,
                )
                logger.exception(
                    "milestone_tracker.error",
                    backoff=self._backoff["milestone_tracker"],
                )

            wait = interval + self._backoff_delay("milestone_tracker")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Gate-pending re-scan — catch tokens that grow past MCap/Liq gates
    # ------------------------------------------------------------------

    async def _gate_pending_loop(self) -> None:
        """Re-check tokens that passed scoring but failed hard MCap/Liq gates.

        Tokens with gate_pending=True are re-checked every N minutes.
        If MCap/Liq now exceeds the gate → send the alert.
        After max_rechecks attempts → give up.
        """
        cfg = self.cfg.gate_pending
        interval = cfg.recheck_interval_seconds

        await asyncio.sleep(30)  # Let eval pipeline run first

        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    from datetime import datetime, timezone, timedelta
                    from sqlalchemy import select

                    now = datetime.now(timezone.utc)

                    # Fetch gate-pending tokens, oldest first
                    stmt = (
                        select(Token)
                        .where(Token.gate_pending.is_(True))
                        .where(Token.alert_sent.is_(False))
                        .where(Token.gate_check_count < cfg.max_rechecks)
                        .order_by(Token.discovered_at.asc())
                        .limit(cfg.batch_size)
                    )
                    tokens = list((await session.execute(stmt)).scalars().all())

                    if not tokens:
                        try:
                            await asyncio.wait_for(
                                self._shutdown.wait(), timeout=interval,
                            )
                            break
                        except asyncio.TimeoutError:
                            continue

                    # Batch fetch DEX data
                    dex_map = await self._filter.fetch_dex_metrics_batch(tokens)

                    min_mcap = self.cfg.filtering.min_alert_mcap_usd
                    min_liq = self.cfg.filtering.min_alert_liquidity_usd
                    alerts_sent = 0
                    gave_up = 0

                    for token in tokens:
                        if self._shutdown.is_set():
                            break

                        addr = token.contract_address.lower()
                        m = dex_map.get(addr)
                        token.gate_check_count += 1
                        token.last_gate_check = now

                        if not m:
                            # No DEX data — skip
                            if token.gate_check_count >= cfg.max_rechecks:
                                token.gate_pending = False
                                gave_up += 1
                            continue

                        current_mcap = m.market_cap_usd or 0
                        current_liq = m.liquidity_usd or 0

                        # Check if gates are now passed
                        mcap_ok = current_mcap >= min_mcap
                        liq_ok = current_liq >= min_liq

                        if mcap_ok and liq_ok:
                            # Gates passed! Send the alert
                            token.gate_pending = False
                            token.rejection_reason = None

                            # Resolve context
                            ctx = await self._resolver.resolve(token, session)

                            # Re-score to get fresh FilterResult for notification
                            filt_result = await self._filter.evaluate(
                                token, session,
                            )
                            # Override rejection — we're forcing this through
                            filt_result.rejected = False
                            filt_result.rejection_reason = ""
                            # Use fresh metrics
                            filt_result.metrics = m

                            # Duplicate contract check
                            cooldown = self.cfg.filtering.duplicate_symbol_cooldown_seconds
                            if cooldown > 0 and token.symbol:
                                dup_cutoff = now - timedelta(seconds=cooldown)
                                dup_stmt = (
                                    select(Token.id, Token.contract_address)
                                    .where(Token.symbol == token.symbol)
                                    .where(Token.alert_sent.is_(True))
                                    .where(Token.updated_at >= dup_cutoff)
                                    .where(Token.id != token.id)
                                    .limit(1)
                                )
                                dup_row = (await session.execute(dup_stmt)).first()
                                if dup_row and dup_row[1] and dup_row[1].lower() == addr:
                                    token.rejection_reason = f"Duplicate contract ${token.symbol}"
                                    continue

                            # Query Nansen buys
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
                                alerts_sent += 1
                                logger.info(
                                    "gate_pending.alert_sent",
                                    token=token.symbol,
                                    score=round(filt_result.final_score, 3),
                                    mcap=current_mcap,
                                    liq=current_liq,
                                    rechecks=token.gate_check_count,
                                )

                                # Record alert outcome
                                outcome = AlertOutcome(
                                    token_id=token.id,
                                    alert_score=filt_result.final_score,
                                    alert_mcap=m.market_cap_usd,
                                    alert_fdv=m.fdv_usd,
                                    alert_liq=m.liquidity_usd,
                                    alert_vol_1h=m.volume_1h_usd,
                                    alert_buys_1h=m.buys_1h,
                                    alert_message_id=(
                                        sent if isinstance(sent, int) and not isinstance(sent, bool) else None
                                    ),
                                )
                                session.add(outcome)

                                # Auto paper trading
                                if self.cfg.paper_trading.enabled:
                                    await self._open_paper_position(
                                        session, token, m,
                                    )
                        else:
                            # Still below gate
                            if token.gate_check_count >= cfg.max_rechecks:
                                token.gate_pending = False
                                gave_up += 1
                                logger.info(
                                    "gate_pending.gave_up",
                                    token=token.symbol,
                                    mcap=current_mcap,
                                    liq=current_liq,
                                    rechecks=token.gate_check_count,
                                )

                    await session.commit()

                    if alerts_sent > 0 or gave_up > 0:
                        logger.info(
                            "gate_pending.cycle",
                            pending=len(tokens),
                            alerts=alerts_sent,
                            gave_up=gave_up,
                        )

                self._backoff["gate_pending"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["gate_pending"] = min(
                    self._backoff["gate_pending"] + 1, 10,
                )
                logger.exception(
                    "gate_pending.error",
                    backoff=self._backoff["gate_pending"],
                )

            wait = interval + self._backoff_delay("gate_pending")
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
        1. Fetch unscored tokens from DB (skip firehose if configured)
        2. Pre-filter: skip obvious trash (Bankr, no socials, etc.)
        3. Batch DexScreener lookup for survivors
        4. Full scoring pipeline for tokens with DEX data
        5. Telegram alert for gems above threshold (champagne uses lower threshold)
        """
        # Small initial delay so poll loops can populate the DB
        await asyncio.sleep(5)
        recheck_delay = self.cfg.filtering.recheck_delay_seconds
        skip_firehose = self.cfg.filtering.skip_firehose_scoring

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
                    )

                    # Fix 1: Skip firehose scoring — firehose has 0% gem rate
                    # Only score tokens from profile/boost/champagne/community_takeover/breakout
                    if skip_firehose:
                        stmt = stmt.where(Token.discovery_source != "firehose")

                    stmt = (
                        stmt.order_by(
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

                        # Fix 2: Champagne tokens use lower threshold
                        # Override rejection if champagne and score >= champagne threshold
                        if (
                            filt_result.rejected
                            and token.is_champagne
                            and filt_result.final_score >= self.cfg.filtering.champagne_score_threshold
                        ):
                            filt_result.rejected = False
                            filt_result.rejection_reason = ""
                            logger.info(
                                "champagne.threshold_override",
                                token=token.symbol,
                                score=filt_result.final_score,
                                champagne_threshold=self.cfg.filtering.champagne_score_threshold,
                            )

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
                                # Mark for gate-pending re-scan
                                if self.cfg.gate_pending.enabled:
                                    token.gate_pending = True
                                    token.gate_check_count = 0
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
                                # Mark for gate-pending re-scan
                                if self.cfg.gate_pending.enabled:
                                    token.gate_pending = True
                                    token.gate_check_count = 0
                                continue

                            # Duplicate symbol cooldown — contract-address aware
                            # Only block if the SAME contract was already alerted,
                            # or a truly different token with matching symbol
                            # was alerted within the cooldown window AND has
                            # the same deployer (likely impersonation).
                            cooldown = self.cfg.filtering.duplicate_symbol_cooldown_seconds
                            if cooldown > 0 and token.symbol:
                                dup_cutoff = datetime.now(timezone.utc) - timedelta(
                                    seconds=cooldown,
                                )
                                dup_stmt = (
                                    select(Token.id, Token.contract_address)
                                    .where(Token.symbol == token.symbol)
                                    .where(Token.alert_sent.is_(True))
                                    .where(Token.updated_at >= dup_cutoff)
                                    .where(Token.id != token.id)
                                    .limit(1)
                                )
                                dup_row = (await session.execute(dup_stmt)).first()
                                if dup_row is not None:
                                    dup_id, dup_addr = dup_row
                                    # Skip only if same contract (true
                                    # duplicate) — different contracts with
                                    # same symbol are different projects
                                    if dup_addr and dup_addr.lower() == token.contract_address.lower():
                                        logger.info(
                                            "alert.blocked.duplicate_contract",
                                            token=token.symbol,
                                            existing_id=dup_id,
                                        )
                                        token.rejection_reason = (
                                            f"Duplicate contract ${token.symbol}"
                                        )
                                        continue
                                    else:
                                        logger.info(
                                            "alert.same_symbol_different_contract",
                                            token=token.symbol,
                                            existing_id=dup_id,
                                            note="Allowing — different contract",
                                        )

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
                                # sent is the Telegram message_id (int) on success
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
                                    alert_message_id=(
                                        sent if isinstance(sent, int) and not isinstance(sent, bool) else None
                                    ),
                                )
                                session.add(outcome)

                                # Fix 5: Auto paper trading
                                if self.cfg.paper_trading.enabled:
                                    await self._open_paper_position(
                                        session, token, metrics,
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
        """Fixed PID controller: conservative threshold adjustment.

        Improvements over v1:
        - min_samples raised to 50 (was 10 — too reactive)
        - step halved to 0.005 (was 0.01 — too aggressive)
        - Hard ceiling at threshold_max (0.60)
        - Asymmetric: easier to lower than to raise
        - Always log at INFO level (debug was invisible)
        """
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import select, func

        step = pid_cfg.threshold_adjust_step  # Now 0.005
        current = self.cfg.filtering.score_threshold

        # ── Dead-man's switch ──
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
            logger.info(
                "pid.insufficient_samples",
                total=total,
                required=pid_cfg.min_samples,
                threshold=current,
            )
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

        # Asymmetric PID:
        # - Raising is harder (step * 1.0) — we don't want to silence the bot
        # - Lowering is easier (step * 1.5) — we DO want to find more gems
        # - Max single adjustment capped at step (never 2x step anymore)
        adjustment = 0.0

        if dead_rate > 0.60:
            adjustment = step  # Was step * 2 — too aggressive
        elif dead_rate > 0.40:
            adjustment = step * 0.5
        elif gem_rate >= pid_cfg.target_gem_rate:
            adjustment = -step * 1.5  # Asymmetric: lower more easily
        elif dead_rate < 0.30 and current > pid_cfg.threshold_min:
            adjustment = -step

        new_threshold = round(
            max(pid_cfg.threshold_min, min(pid_cfg.threshold_max, current + adjustment)),
            3,
        )

        # Always log at INFO so we can see PID history
        logger.info(
            "pid.auto_tune",
            old=current,
            new=new_threshold,
            adjustment=round(adjustment, 4),
            gem_rate=round(gem_rate, 3),
            dead_rate=round(dead_rate, 3),
            total_outcomes=total,
            gems=gems,
            deads=deads,
        )

        if new_threshold != current:
            self.cfg.filtering.score_threshold = new_threshold

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
    # Champagne re-evaluation loop — catch slow-starting champagne gems
    # ------------------------------------------------------------------

    async def _champagne_eval_loop(self) -> None:
        """Re-evaluate champagne tokens that scored low or had no DEX data.

        Only 3/390 champagne tokens were alerted. Most lacked DEX data at
        first scoring. This loop re-checks them at intervals with the lower
        champagne_score_threshold until max_age_hours is reached.
        """
        ce_cfg = self.cfg.champagne_eval
        if not ce_cfg.enabled:
            logger.info("champagne_eval.disabled")
            return

        interval = ce_cfg.reeval_interval_seconds
        await asyncio.sleep(120)  # Let other loops warm up

        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    from datetime import datetime, timezone, timedelta
                    from sqlalchemy import select, or_

                    now = datetime.now(timezone.utc)
                    max_age = timedelta(hours=ce_cfg.max_age_hours)
                    cutoff = now - max_age

                    # Find champagne tokens that:
                    # - Were scored but below alert threshold
                    # - Have not been alerted
                    # - Are younger than max_age_hours
                    stmt = (
                        select(Token)
                        .where(Token.is_champagne.is_(True))
                        .where(Token.alert_sent.is_(False))
                        .where(Token.discovered_at >= cutoff)
                        .where(
                            or_(
                                Token.quality_score.is_(None),
                                Token.quality_score < self.cfg.filtering.score_threshold,
                            )
                        )
                        .order_by(Token.discovered_at.desc())
                        .limit(10)
                    )
                    tokens = list((await session.execute(stmt)).scalars().all())

                    if tokens:
                        logger.info(
                            "champagne_eval.batch",
                            count=len(tokens),
                            names=[t.symbol for t in tokens],
                        )

                        for token in tokens:
                            if self._shutdown.is_set():
                                break

                            # Re-score — reset quality_score to force re-evaluation
                            token.quality_score = None
                            token.rejection_reason = None

                            ctx = await self._resolver.resolve(token, session)
                            filt_result = await self._filter.evaluate(token, session)
                            self._stats["tokens_scored"] += 1

                            # Use champagne-specific lower threshold
                            threshold = self.cfg.filtering.champagne_score_threshold
                            if (
                                filt_result.final_score >= threshold
                                and not token.alert_sent
                            ):
                                metrics = filt_result.metrics
                                alert_mcap = (metrics.market_cap_usd or 0) if metrics else 0
                                alert_liq = (metrics.liquidity_usd or 0) if metrics else 0

                                # Still enforce hard gates
                                if alert_mcap < self.cfg.filtering.min_alert_mcap_usd:
                                    continue
                                if alert_liq < self.cfg.filtering.min_alert_liquidity_usd:
                                    continue

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
                                        "champagne_eval.alert_sent",
                                        token=token.symbol,
                                        score=filt_result.final_score,
                                        mcap=alert_mcap,
                                    )
                                    # Record outcome
                                    outcome = AlertOutcome(
                                        token_id=token.id,
                                        alert_score=filt_result.final_score,
                                        alert_mcap=alert_mcap,
                                        alert_fdv=(metrics.fdv_usd if metrics else None),
                                        alert_liq=alert_liq,
                                        alert_vol_1h=(metrics.volume_1h_usd if metrics else None),
                                        alert_buys_1h=(metrics.buys_1h if metrics else None),
                                    )
                                    session.add(outcome)

                                    if self.cfg.paper_trading.enabled:
                                        await self._open_paper_position(
                                            session, token, metrics,
                                        )

                    await session.commit()

                self._backoff["champagne_eval"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["champagne_eval"] = min(
                    self._backoff.get("champagne_eval", 0) + 1, 10,
                )
                logger.exception("champagne_eval.error")

            wait = interval + self._backoff_delay("champagne_eval")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Paper trading loop — manage simulated positions
    # ------------------------------------------------------------------

    async def _open_paper_position(
        self, session, token: Token, metrics,
    ) -> None:
        """Open a simulated paper position for a token alert."""
        pt_cfg = self.cfg.paper_trading
        if not pt_cfg.enabled:
            return

        # Check max open positions
        from sqlalchemy import select, func
        open_count = await session.scalar(
            select(func.count(PaperPosition.id))
            .where(PaperPosition.status == "open")
        )
        if (open_count or 0) >= pt_cfg.max_open_positions:
            logger.info(
                "paper.max_positions_reached",
                open=open_count,
                max=pt_cfg.max_open_positions,
            )
            return

        # Check if we already have a position for this token
        existing = await session.scalar(
            select(func.count(PaperPosition.id))
            .where(PaperPosition.token_id == token.id)
        )
        if existing:
            return

        entry_price = (metrics.price_usd or 0) if metrics else 0
        entry_mcap = (metrics.market_cap_usd or 0) if metrics else 0
        entry_fdv = (metrics.fdv_usd or 0) if metrics else 0
        entry_liq = (metrics.liquidity_usd or 0) if metrics else 0

        position = PaperPosition(
            token_id=token.id,
            entry_price_usd=entry_price,
            entry_mcap=entry_mcap,
            entry_fdv=entry_fdv,
            entry_liq=entry_liq,
            position_size_usd=pt_cfg.position_size_usd,
            remaining_size_pct=100.0,
            status="open",
            highest_price_usd=entry_price,
            lowest_price_usd=entry_price,
            current_price_usd=entry_price,
        )
        session.add(position)
        logger.info(
            "paper.position_opened",
            token=token.symbol,
            entry_price=entry_price,
            size=pt_cfg.position_size_usd,
            mcap=entry_mcap,
        )

    async def _paper_trading_loop(self) -> None:
        """Check open paper positions for SL/TP triggers.

        For each open position:
        1. Fetch live price from DexScreener
        2. Check stop-loss (-30%)
        3. Check take-profit levels (TP1 +50%, TP2 +100%, TP3 +300%)
        4. Check time stop (24h flat)
        5. Update PnL + highest/lowest
        """
        pt_cfg = self.cfg.paper_trading
        if not pt_cfg.enabled:
            logger.info("paper_trading.disabled")
            return

        interval = pt_cfg.check_interval_seconds
        await asyncio.sleep(60)

        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    from datetime import datetime, timezone, timedelta
                    from sqlalchemy import select

                    now = datetime.now(timezone.utc)

                    # Get all open positions
                    stmt = (
                        select(PaperPosition)
                        .where(PaperPosition.status.in_(["open", "tp1", "tp2"]))
                        .order_by(PaperPosition.opened_at.asc())
                    )
                    positions = list((await session.execute(stmt)).scalars().all())

                    if not positions:
                        try:
                            await asyncio.wait_for(
                                self._shutdown.wait(), timeout=interval,
                            )
                            break
                        except asyncio.TimeoutError:
                            continue

                    # Get token addresses for batch DexScreener
                    token_ids = [p.token_id for p in positions]
                    token_stmt = select(Token).where(Token.id.in_(token_ids))
                    tokens = {
                        t.id: t
                        for t in (await session.execute(token_stmt)).scalars().all()
                    }

                    token_list = [t for t in tokens.values() if t.contract_address]
                    dex_map = await self._filter.fetch_dex_metrics_batch(token_list)

                    closed_count = 0
                    for pos in positions:
                        token = tokens.get(pos.token_id)
                        if not token:
                            continue
                        addr = token.contract_address.lower()
                        m = dex_map.get(addr)

                        if not m or not m.price_usd:
                            # No DEX data — might be dead
                            age_hours = (now - pos.opened_at).total_seconds() / 3600
                            if age_hours >= pt_cfg.time_stop_hours:
                                pos.status = "sl"
                                pos.closed_at = now
                                pos.realized_pnl_usd -= pos.position_size_usd * (pos.remaining_size_pct / 100)
                                pos.remaining_size_pct = 0
                                closed_count += 1
                            continue

                        current_price = m.price_usd
                        entry_price = pos.entry_price_usd or 0

                        if entry_price <= 0:
                            continue

                        pos.current_price_usd = current_price
                        pos.last_check_mcap = m.market_cap_usd
                        pos.last_checked_at = now

                        # Track high/low
                        if pos.highest_price_usd is None or current_price > pos.highest_price_usd:
                            pos.highest_price_usd = current_price
                        if pos.lowest_price_usd is None or current_price < pos.lowest_price_usd:
                            pos.lowest_price_usd = current_price

                        pnl_pct = ((current_price - entry_price) / entry_price) * 100
                        pos.unrealized_pnl_usd = pos.position_size_usd * (pos.remaining_size_pct / 100) * (pnl_pct / 100)

                        # ── Stop loss ──
                        if pnl_pct <= pt_cfg.stop_loss_pct:
                            loss_usd = pos.position_size_usd * (pos.remaining_size_pct / 100) * (pnl_pct / 100)
                            pos.realized_pnl_usd += loss_usd
                            pos.remaining_size_pct = 0
                            pos.status = "sl"
                            pos.closed_at = now
                            closed_count += 1
                            logger.info(
                                "paper.stop_loss",
                                token=token.symbol,
                                pnl_pct=round(pnl_pct, 1),
                                realized=round(pos.realized_pnl_usd, 2),
                            )
                            continue

                        # ── Take profit levels ──
                        sell_pct_per_tp = 33.33

                        if pos.status == "open" and pnl_pct >= pt_cfg.tp1_pct:
                            sell_size = pos.position_size_usd * (sell_pct_per_tp / 100)
                            profit = sell_size * (pnl_pct / 100)
                            pos.realized_pnl_usd += profit
                            pos.remaining_size_pct -= sell_pct_per_tp
                            pos.status = "tp1"
                            logger.info(
                                "paper.tp1_hit",
                                token=token.symbol,
                                pnl_pct=round(pnl_pct, 1),
                                realized=round(pos.realized_pnl_usd, 2),
                            )

                        if pos.status == "tp1" and pnl_pct >= pt_cfg.tp2_pct:
                            sell_size = pos.position_size_usd * (sell_pct_per_tp / 100)
                            profit = sell_size * (pnl_pct / 100)
                            pos.realized_pnl_usd += profit
                            pos.remaining_size_pct -= sell_pct_per_tp
                            pos.status = "tp2"
                            logger.info(
                                "paper.tp2_hit",
                                token=token.symbol,
                                pnl_pct=round(pnl_pct, 1),
                                realized=round(pos.realized_pnl_usd, 2),
                            )

                        if pos.status == "tp2" and pnl_pct >= pt_cfg.tp3_pct:
                            sell_size = pos.position_size_usd * (pos.remaining_size_pct / 100)
                            profit = sell_size * (pnl_pct / 100)
                            pos.realized_pnl_usd += profit
                            pos.remaining_size_pct = 0
                            pos.status = "tp3"
                            pos.closed_at = now
                            closed_count += 1
                            logger.info(
                                "paper.tp3_hit",
                                token=token.symbol,
                                pnl_pct=round(pnl_pct, 1),
                                realized=round(pos.realized_pnl_usd, 2),
                            )

                        # ── Time stop ──
                        age_hours = (now - pos.opened_at).total_seconds() / 3600
                        if age_hours >= pt_cfg.time_stop_hours and pos.remaining_size_pct > 0:
                            remaining_value = pos.position_size_usd * (pos.remaining_size_pct / 100)
                            final_pnl = remaining_value * (pnl_pct / 100)
                            pos.realized_pnl_usd += final_pnl
                            pos.remaining_size_pct = 0
                            pos.status = "time_stop"
                            pos.closed_at = now
                            closed_count += 1
                            logger.info(
                                "paper.time_stop",
                                token=token.symbol,
                                pnl_pct=round(pnl_pct, 1),
                                age_hours=round(age_hours, 1),
                                realized=round(pos.realized_pnl_usd, 2),
                            )

                    if closed_count > 0:
                        logger.info("paper.positions_closed", count=closed_count)

                    await session.commit()

                self._backoff["paper_trading"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["paper_trading"] = min(
                    self._backoff.get("paper_trading", 0) + 1, 10,
                )
                logger.exception("paper_trading.error")

            wait = interval + self._backoff_delay("paper_trading")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Multi-wallet conviction detection — cross-scanner overlap
    # ------------------------------------------------------------------

    async def _multi_conviction_loop(self) -> None:
        """Detect multiple tracked wallets buying the same token.

        When 2+ wallets buy the same token within a time window, fire
        a high-priority conviction alert. This is the strongest signal.

        Also: reverse-lookup — when a wallet buys a token not in DB,
        auto-ingest it if it meets min_liq requirements.
        """
        mc_cfg = self.cfg.multi_conviction
        if not mc_cfg.enabled:
            logger.info("multi_conviction.disabled")
            return

        interval = 120  # Check every 2 minutes
        await asyncio.sleep(60)

        while not self._shutdown.is_set():
            try:
                async with self._session_factory() as session:
                    from datetime import datetime, timezone, timedelta
                    from sqlalchemy import select, func

                    now = datetime.now(timezone.utc)
                    window = timedelta(hours=mc_cfg.window_hours)
                    cutoff = now - window

                    # Find tokens bought by 2+ distinct wallets within window
                    stmt = (
                        select(
                            WalletSwap.token_address,
                            func.count(func.distinct(WalletSwap.wallet_address)).label("wallet_count"),
                        )
                        .where(WalletSwap.action == "buy")
                        .where(WalletSwap.recorded_at >= cutoff)
                        .where(WalletSwap.conviction_sent.is_(False))
                        .group_by(WalletSwap.token_address)
                        .having(
                            func.count(func.distinct(WalletSwap.wallet_address)) >= mc_cfg.min_wallets
                        )
                    )
                    rows = (await session.execute(stmt)).all()

                    for token_addr, wallet_count in rows:
                        # Check if token is in our DB
                        token = (
                            await session.execute(
                                select(Token)
                                .where(Token.contract_address == token_addr)
                                .limit(1)
                            )
                        ).scalar_one_or_none()

                        if token:
                            # Fire multi-wallet conviction
                            swaps_stmt = (
                                select(WalletSwap)
                                .where(WalletSwap.token_address == token_addr)
                                .where(WalletSwap.action == "buy")
                                .where(WalletSwap.recorded_at >= cutoff)
                            )
                            swaps = list((await session.execute(swaps_stmt)).scalars().all())

                            wallet = None
                            if swaps:
                                w_stmt = (
                                    select(SmartWallet)
                                    .where(SmartWallet.address == swaps[0].wallet_address)
                                    .limit(1)
                                )
                                wallet = (await session.execute(w_stmt)).scalar_one_or_none()

                            conviction = {
                                "token": token,
                                "wallet": wallet,
                                "buy": {
                                    "wallet_address": swaps[0].wallet_address if swaps else "",
                                    "usd_value": sum(s.usd_value or 0 for s in swaps),
                                    "tx_hash": swaps[0].tx_hash if swaps else "",
                                    "wallet_alias": f"{wallet_count} wallets",
                                },
                                "is_alerted": token.alert_sent,
                            }
                            await self._notifier.notify_conviction(conviction)
                            logger.info(
                                "multi_conviction.fired",
                                token=token.symbol,
                                wallets=wallet_count,
                                total_usd=sum(s.usd_value or 0 for s in swaps),
                            )

                            # Mark swaps as conviction-sent
                            for swap in swaps:
                                swap.conviction_sent = True
                        elif mc_cfg.auto_ingest_from_wallet_buy:
                            # Token not in DB — auto-ingest via DexScreener
                            dex_url = self.cfg.dexscreener.base_url
                            try:
                                resp = await self._http.get(
                                    f"{dex_url}/tokens/{token_addr}", timeout=10,
                                )
                                resp.raise_for_status()
                                pairs = resp.json().get("pairs") or []
                                if pairs:
                                    pair = pairs[0]
                                    liq = float((pair.get("liquidity") or {}).get("usd", 0))
                                    if liq >= mc_cfg.auto_ingest_min_liq:
                                        new_token = Token(
                                            contract_address=token_addr,
                                            name=pair.get("baseToken", {}).get("name", ""),
                                            symbol=pair.get("baseToken", {}).get("symbol", ""),
                                            chain="base",
                                            discovery_source="wallet_conviction",
                                            is_breakout=True,
                                        )
                                        session.add(new_token)
                                        logger.info(
                                            "multi_conviction.auto_ingest",
                                            token=new_token.symbol,
                                            address=token_addr[:12],
                                            liq=liq,
                                            wallets=wallet_count,
                                        )
                            except Exception as exc:
                                logger.warning(
                                    "multi_conviction.ingest_failed",
                                    addr=token_addr[:12],
                                    error=str(exc),
                                )

                    await session.commit()

                self._backoff["multi_conviction"] = 0
            except Exception:
                self._stats["errors"] += 1
                self._backoff["multi_conviction"] = min(
                    self._backoff.get("multi_conviction", 0) + 1, 10,
                )
                logger.exception("multi_conviction.error")

            wait = interval + self._backoff_delay("multi_conviction")
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

                champagne_eval_line = ""
                if self.cfg.champagne_eval.enabled:
                    champagne_eval_line = "• Champagne eval (10m)\n"
                paper_line = ""
                if self.cfg.paper_trading.enabled:
                    champagne_eval_line = champagne_eval_line  # keep
                    paper_line = "• Paper trading (5m)\n"
                conviction_line = ""
                if self.cfg.multi_conviction.enabled:
                    conviction_line = "• Multi-wallet conviction (5m)\n"

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
                        "• Wallet monitor (60s)\n"
                        f"{champagne_eval_line}"
                        f"{paper_line}"
                        f"{conviction_line}"
                        "\n🤖 Interactive bot: /menu\n"
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
