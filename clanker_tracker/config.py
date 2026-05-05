"""Pydantic configuration models and YAML loader."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Section models
# ---------------------------------------------------------------------------

class DatabaseConfig(BaseModel):
    url: str = Field(
        default="postgresql+asyncpg://clanker:clanker@localhost:5432/clanker_tracker",
        description="Async SQLAlchemy database URL (PostgreSQL recommended for production)",
    )
    echo: bool = False
    pool_size: int = Field(default=10, ge=1, description="Connection pool size")
    max_overflow: int = Field(default=20, ge=0, description="Max overflow connections beyond pool_size")
    pool_recycle: int = Field(default=3600, description="Recycle connections after N seconds (prevent stale PG connections)")


class ClankerAPIConfig(BaseModel):
    base_url: str = "https://www.clanker.world/api"
    poll_interval_seconds: int = Field(default=30, ge=5)
    champagne_poll_interval_seconds: int = Field(
        default=120,
        ge=30,
        description="How often to scan for champagne-tagged tokens (curated gems)",
    )
    api_key: Optional[str] = Field(
        default=None,
        description="x-api-key header for authenticated endpoints (deploy, get-by-address)",
    )
    page_size: int = Field(default=50, ge=1, le=100)


class BackfillConfig(BaseModel):
    """Settings for backfilling tokens on first startup.

    On the very first run (empty DB), the firehose only sees the latest
    page of tokens.  Backfill pages backward through the Clanker API
    to catch tokens from the last N hours so nothing is missed.
    """

    enabled: bool = Field(
        default=True,
        description="Enable backfill on first run (empty database)",
    )
    hours: int = Field(
        default=12,
        ge=1,
        le=48,
        description="How many hours of history to backfill on first run",
    )
    max_pages: int = Field(
        default=200,
        ge=1,
        description="Safety cap: max pages to backfill (10 tokens per page)",
    )


class BankrConfig(BaseModel):
    """Known Bankr bot deployer addresses and SDK settings."""
    deployer_addresses: list[str] = Field(
        default_factory=list,
        description="Bankr deployer wallet addresses (lowercase, checksummed not required)",
    )
    sdk_endpoint: Optional[str] = None
    micropayment_amount_usd: float = 0.10

    class Config:
        # Allow None → default_factory for list fields
        validate_default = True

    from pydantic import field_validator

    @field_validator("deployer_addresses", mode="before")
    @classmethod
    def _coerce_none_to_list(cls, v):
        return v if v is not None else []


class DexScreenerConfig(BaseModel):
    base_url: str = "https://api.dexscreener.com/latest/dex"
    rate_limit_per_minute: int = 60
    batch_size: int = Field(
        default=30,
        ge=1,
        le=30,
        description="Max addresses per batch DexScreener lookup (API limit: 30)",
    )


class BaseRPCConfig(BaseModel):
    http_url: str = "https://mainnet.base.org"
    ws_url: Optional[str] = Field(
        default=None,
        description="WebSocket URL for real-time TokenCreated event monitoring",
    )


class FilteringConfig(BaseModel):
    """Thresholds for the multi-stage scoring pipeline.

    Reality check (from live data analysis, Feb 2026):
    - 38K+ tokens launch per day on Clanker
    - ~92% are Bankr bot launches, ~6% are Clawnch, ~2% other
    - 99%+ are completely dead (zero DexScreener data)
    - Only ~96 out of 431K+ tokens have the 'champagne' curated tag
    - Of champagne tokens, 58% have real liquidity ($5K+)
    """

    # ── Pre-filter: skip obvious trash before hitting DexScreener ──
    skip_bankr: bool = Field(
        default=False,
        description="Skip all Bankr bot launches (92% of tokens). "
                    "Set True to focus on organic / Clawnch / direct launches.",
    )
    require_social_links: bool = Field(
        default=False,
        description="Require at least one social link (Twitter/website) to proceed",
    )
    champagne_auto_pass_stage1: bool = Field(
        default=True,
        description="Champagne-tagged tokens skip Stage 1 instant-reject",
    )

    # ── Stage 1 — instant reject ──
    scam_keywords: list[str] = Field(
        default_factory=lambda: [
            "rug", "scam", "honeypot", "honey pot",
            "ponzi", "fake", "drain",
        ],
    )
    # Platform/ecosystem name impersonation — tokens using names of known
    # platforms are almost always scam tokens gaming wash-trade metrics.
    # These get hard-rejected in Stage 1 regardless of DEX metrics.
    impersonation_names: list[str] = Field(
        default_factory=lambda: [
            "clanker", "clawnch", "bankr", "uniswap", "dexscreener",
            "coinbase", "ethereum", "solana", "opensea", "metamask",
            "basescan", "etherscan", "aave", "compound", "aerodrome",
            "warpcast", "farcaster", "base chain", "basechain",
        ],
        description="Token names/symbols that match known platform names are rejected "
                    "as impersonation scams (case-insensitive exact match)",
    )
    min_mcap_usd: float = 1_000.0

    # ── Stage 2 — DEX metrics (DexScreener batch lookup) ──
    min_pool_liquidity_usd: float = Field(
        default=3_000.0,
        description="Min liquidity to score positively (raised from $1K — too many dead)",
    )
    min_volume_1h_usd: float = Field(
        default=500.0,
        description="Min 1h volume for positive score (raised from $50)",
    )
    min_buy_sell_ratio: float = 0.2
    min_holders: int = Field(
        default=5,
        description="Min holders for bonus (raised from 3)",
    )
    recheck_delay_seconds: int = Field(
        default=300,
        description="Wait N seconds after discovery before checking DEX metrics "
                    "(gives pools time to get indexed by DexScreener)",
    )

    # ── Bot detection — catches bot-sprayed tokens ──
    bot_buy_threshold: int = Field(
        default=250,
        description="If buys_1h exceeds this threshold, check avg buy size",
    )
    bot_avg_buy_max_usd: float = Field(
        default=100.0,
        description="If avg buy (vol/buys) < this $ with high buy count, flag as bot",
    )

    # ── Wash-trading detection ──
    wash_vol_liq_ratio: float = Field(
        default=2.0,
        description="If vol_1h/liquidity exceeds this AND buys > wash_min_buys, penalty applied",
    )
    wash_min_buys: int = Field(
        default=200,
        description="Minimum buys_1h to trigger wash-trading detection (with vol/liq ratio)",
    )
    wash_score_multiplier: float = Field(
        default=0.50,
        description="Multiply Stage 2 score by this when wash-trading detected (halved)",
    )

    # ── Stage 3 — momentum detection ──
    min_volume_5m_usd: float = 100.0
    min_buys_1h: int = Field(
        default=10,
        description="Min buys in 1h for momentum signal (raised from 5)",
    )
    price_surge_threshold_pct: float = Field(
        default=50.0,
        description="24h price increase % to flag as surging",
    )

    # ── Stage 4 — smart money ──
    smart_money_wallet_file: str = "data/smart_money_wallets.txt"
    smart_money_weight: float = 2.0

    # ── Stage 5 — context quality ──
    context_quality_weight: float = 1.5
    require_origin_url: bool = False

    # ── Scoring weights ──
    # Data-driven rebalance (Feb 2026 analysis of 88 alerts):
    # - Metrics up (2.0): liq + volume are the real survival signals
    # - Context down (0.3): social links alone don't predict success
    # - Added firehose penalty: 0% winners, 84% trash historically
    weight_metrics: float = Field(
        default=2.0,
        description="DEX metrics weight (raised from 1.0 — primary signal)",
    )
    weight_momentum: float = 1.5
    weight_smart_money: float = 2.0
    weight_context: float = Field(
        default=0.3,
        description="Context weight (lowered from 1.0 — social links not predictive)",
    )
    weight_champagne_bonus: float = Field(
        default=0.15,
        description="Flat score bonus for champagne-tagged tokens",
    )
    firehose_score_penalty: float = Field(
        default=0.70,
        description="Multiply firehose scores by this factor (0-1). Firehose has 0% win rate.",
    )

    # ── Alert gates ──
    score_threshold: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        description="Minimum weighted score to trigger an alert (lowered from 0.55 — "
                    "new weights + bot detection make scores more honest)",
    )
    champagne_score_threshold: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        description="Lower threshold for champagne tokens (only 3/390 were alerted — fix)",
    )
    skip_firehose_scoring: bool = Field(
        default=True,
        description="Skip full scoring for firehose tokens (0%% gem rate — ingestion only)",
    )
    min_alert_mcap_usd: float = Field(
        default=25_000.0,
        description="Hard MCap floor at alert time — tokens below this never alert",
    )
    min_alert_liquidity_usd: float = Field(
        default=5_000.0,
        description="Hard liquidity floor at alert time — tokens below this never alert",
    )
    duplicate_symbol_cooldown_seconds: int = Field(
        default=86400,
        description="Don't alert on a symbol already alerted within this window (24h default)",
    )


class GatePendingConfig(BaseModel):
    """Gate-pending re-scan — catches tokens that scored well but had low MCap/Liq.

    When a token passes the full scoring pipeline (>= threshold) but fails
    the hard MCap or liquidity gate, it's marked as 'gate_pending'. A
    dedicated loop re-checks these tokens periodically to see if MCap/Liq
    grew above the gate. If so, the alert fires.

    Data-driven rationale: 107 tokens scored 0.45-0.88 were blocked by the
    $50K MCap gate. Many gems start small and pump later. COOK had $48.5K
    MCap (score 0.75) — just $1,500 below the gate and was never re-checked.
    """

    enabled: bool = Field(
        default=True,
        description="Enable gate-pending re-scan loop",
    )
    recheck_interval_seconds: int = Field(
        default=600,
        ge=60,
        description="How often to re-check gate-pending tokens (10 min default)",
    )
    max_rechecks: int = Field(
        default=18,
        ge=1,
        description="Max re-check attempts before giving up (18 * 10min = 3 hours)",
    )
    batch_size: int = Field(
        default=30,
        ge=1,
        le=30,
        description="Max tokens per DexScreener batch (API limit: 30)",
    )


class BreakoutConfig(BaseModel):
    """Settings for the breakout scanner — detects delayed movers.

    Uses DexScreener trending/boosted/profiles endpoints to find Base
    tokens that are gaining momentum AFTER launch.  These are tokens
    the firehose may have already passed over but are now showing life.
    """

    enabled: bool = Field(
        default=True,
        description="Enable the breakout scanner loop",
    )
    poll_interval_seconds: int = Field(
        default=180,
        ge=60,
        description="How often to scan DexScreener for breakout tokens (seconds)",
    )
    min_liquidity_usd: float = Field(
        default=10_000.0,
        description="Minimum liquidity to consider a breakout candidate",
    )
    min_volume_24h_usd: float = Field(
        default=5_000.0,
        description="Minimum 24h volume to consider a breakout candidate",
    )
    min_buys_1h: int = Field(
        default=20,
        description="Minimum buys in last hour for breakout signal",
    )
    min_price_change_1h_pct: float = Field(
        default=50.0,
        description="Minimum 1h price change % to flag as breakout",
    )
    max_age_days: int = Field(
        default=14,
        description="Ignore tokens older than this (focus on recent launches)",
    )
    breakout_score_bonus: float = Field(
        default=0.05,
        description="Flat score bonus for tokens detected via breakout scanner",
    )
    rescan_cooldown_seconds: int = Field(
        default=3600,
        description="Don't re-evaluate a breakout token within this window",
    )


class GainersConfig(BaseModel):
    """Settings for the DexScreener gainers / search scanner.

    Uses the DexScreener search endpoint with rotating keywords to
    discover high-volume Base tokens the firehose and breakout scanner
    missed.  Also checks community-takeover tokens.
    """

    enabled: bool = Field(
        default=True,
        description="Enable the gainers scanner loop",
    )
    poll_interval_seconds: int = Field(
        default=120,
        ge=60,
        description="How often to scan DexScreener search for gainers (seconds)",
    )
    search_keywords: list[str] = Field(
        default_factory=lambda: [
            "clanker", "base agent", "base ai", "base meme",
            "clawnch", "farcaster token", "base new",
        ],
        description="Rotating search keywords for DexScreener /latest/dex/search",
    )
    skip_addresses: list[str] = Field(
        default_factory=lambda: [
            # Well-known blue-chip / infrastructure tokens on Base.
            # These are NOT newly launched tokens and must never trigger alerts.
            "0x4200000000000000000000000000000000000006",  # WETH
            "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
            "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
            "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",  # cbETH
            "0xb6fe221fe9eef5aba221c348ba20a1bf5e73624c",  # rETH
            "0x940181a94a35a4569e4529a3cdfb74e38fd98631",  # AERO
            "0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b",  # VIRTUAL
            "0x532f27101965dd16442e59d40670faf5ebb142e4",  # BRETT
            "0xac1bd2486aaf3b5c0fc3fd868558b082a531b2b4",  # TOSHI
            "0x0578d8a44db98b23bf096a382e016e29a5ce0ffe",  # HIGHER
            "0x768be13e1680b5ebe0024c42c896e3db59ec0149",  # MFER
        ],
        description="Known blue-chip / infrastructure token addresses to skip (lowercase)",
    )
    max_fdv_usd: float = Field(
        default=50_000_000.0,
        description="Skip tokens with FDV above this — clearly not hidden gems",
    )
    min_liquidity_usd: float = Field(
        default=5_000.0,
        description="Minimum liquidity to consider a gainer candidate",
    )
    min_volume_1h_usd: float = Field(
        default=1_000.0,
        description="Minimum 1h volume for gainer detection",
    )
    min_buys_1h: int = Field(
        default=10,
        description="Minimum buy txns in last hour",
    )
    min_price_change_1h_pct: float = Field(
        default=20.0,
        description="Minimum 1h price change % to flag as a gainer",
    )
    max_age_days: int = Field(
        default=7,
        description="Ignore tokens older than this many days",
    )
    gainer_score_bonus: float = Field(
        default=0.05,
        description="Flat score bonus for tokens detected via gainers scanner",
    )
    rescan_cooldown_seconds: int = Field(
        default=1800,
        description="Don't re-check the same gainer within this window",
    )


class PIDConfig(BaseModel):
    """PID feedback loop — self-improving alert quality.

    After sending an alert, the bot re-checks the token at configurable
    intervals to classify the outcome.  The rolling outcome ratio feeds
    a PID controller that auto-adjusts score_threshold.
    """

    enabled: bool = Field(
        default=True,
        description="Enable the outcome-tracking PID feedback loop",
    )
    check_interval_seconds: int = Field(
        default=300,
        ge=60,
        description="How often the outcome loop runs (seconds)",
    )
    check_windows: list[int] = Field(
        default_factory=lambda: [3600, 21600, 86400],
        description="Re-check at these offsets (1h, 6h, 24h) after alert",
    )
    check_tolerance_seconds: int = Field(
        default=600,
        description="Window tolerance — check if within ±tolerance of target",
    )

    # Outcome classification
    gem_mcap_multiplier: float = Field(
        default=2.0,
        description="MCap must grow by this factor vs alert-time to be 'gem'",
    )
    dead_liq_threshold_usd: float = Field(
        default=500.0,
        description="Liquidity below this at any checkpoint → 'dead'",
    )

    # Auto-tune
    auto_tune_enabled: bool = Field(
        default=True,
        description="Automatically adjust score_threshold based on outcome ratio",
    )
    target_gem_rate: float = Field(
        default=0.25,
        ge=0.05,
        le=0.80,
        description="Target proportion of alerts that should be gems",
    )
    threshold_adjust_step: float = Field(
        default=0.005,
        description="How much to adjust score_threshold per PID cycle (halved from 0.01)",
    )
    threshold_min: float = Field(
        default=0.40,
        description="Never lower score_threshold below this",
    )
    threshold_max: float = Field(
        default=0.60,
        description="Never raise score_threshold above this (must be < firehose_penalty)",
    )
    lookback_hours: int = Field(
        default=48,
        description="Rolling window for PID outcome ratio calculation",
    )
    min_samples: int = Field(
        default=50,
        description="Don't auto-tune until at least 50 outcomes are classified (was 10 — too reactive)",
    )
    no_alert_decay_hours: int = Field(
        default=6,
        description="If no alerts sent in this many hours, lower threshold by one step "
                    "per PID cycle to prevent runaway (dead-man's switch)",
    )


class ArkhamConfig(BaseModel):
    """Arkham Intel API integration for smart wallet tracking and on-chain intelligence.

    Uses the Arkham Intel API to:
    1. Fetch wallets tagged 'fomo-user' on Base
    2. Analyze wallet performance (PnL over 1d/7d/30d)
    3. Monitor wallet swaps for buy signals
    4. Generate conviction alerts when wallets buy known tokens
    5. Token Holder Intelligence — identify WHO holds a token (funds, VCs, whales)
    6. Deployer Profiling — identify WHO deployed a token (scammer? builder?)
    7. Token Flow Intelligence — detect smart money dumps/accumulation
    8. Portfolio Watch — detect new positions from tracked wallets
    """

    enabled: bool = Field(
        default=False,
        description="Enable smart wallet tracking via Arkham Intel API",
    )
    api_key: Optional[str] = Field(
        default=None,
        description="Arkham Intel API key (required). Apply at https://intel.arkm.com/api",
    )
    base_url: str = "https://api.arkm.com"
    tag_id: str = Field(
        default="fomo-user",
        description="Arkham tag ID to fetch wallets from",
    )

    # ── Wallet sync ──
    wallet_sync_interval_seconds: int = Field(
        default=3600,
        ge=300,
        description="How often to re-sync wallet list from Arkham (1h default)",
    )
    max_wallets: int = Field(
        default=200,
        ge=10,
        description="Maximum number of wallets to track",
    )

    # ── Performance filtering ──
    min_profit_pct_1d: float = Field(
        default=5.0,
        description="Minimum 1-day profit % to qualify as profitable wallet",
    )
    min_profit_pct_7d: float = Field(
        default=10.0,
        description="Minimum 7-day profit % to qualify",
    )
    min_profit_pct_30d: float = Field(
        default=20.0,
        description="Minimum 30-day profit % to qualify",
    )
    min_profitable_periods: int = Field(
        default=1,
        ge=1,
        le=3,
        description="Must be profitable in at least N of the 3 periods (1d/7d/30d)",
    )

    # ── Swap monitoring ──
    swap_poll_interval_seconds: int = Field(
        default=60,
        ge=30,
        description="How often to check tracked wallets for new swaps (1 min default)",
    )
    min_swap_usd: float = Field(
        default=100.0,
        description="Minimum swap size (USD) to consider as a buy signal",
    )

    # ── Conviction alerts ──
    conviction_enabled: bool = Field(
        default=True,
        description="Send conviction alerts when tracked wallets buy DB tokens",
    )
    conviction_min_wallets: int = Field(
        default=1,
        ge=1,
        description="Minimum wallet buys to trigger a conviction alert",
    )
    conviction_score_bonus: float = Field(
        default=0.20,
        description="Score bonus added when conviction signal fires",
    )

    # ── Rate limiting ──
    rate_limit_per_second: int = Field(
        default=15,
        description="Max requests per second (Arkham allows 20, leave headroom)",
    )
    heavy_endpoint_delay: float = Field(
        default=1.1,
        description="Seconds between heavy endpoint calls (swaps, transfers, top_flow)",
    )

    # ── Token Holder Intelligence (THI) ──
    holder_intel_enabled: bool = Field(
        default=True,
        description="Analyze top token holders via Arkham during scoring",
    )
    holder_known_entity_bonus: float = Field(
        default=0.08,
        ge=0.0,
        le=0.30,
        description="Score bonus when token has known Arkham-labeled holders",
    )
    holder_fund_vc_bonus: float = Field(
        default=0.12,
        ge=0.0,
        le=0.30,
        description="Score bonus when fund/VC entities hold the token",
    )
    holder_concentration_penalty: float = Field(
        default=0.10,
        ge=0.0,
        le=0.30,
        description="Score penalty when top 10 holders own >80% of supply",
    )
    holder_max_top10_pct: float = Field(
        default=80.0,
        ge=50.0,
        le=100.0,
        description="Concentration threshold: top 10 holders % to trigger penalty",
    )
    holder_exchange_risk_penalty: float = Field(
        default=0.05,
        ge=0.0,
        le=0.20,
        description="Penalty when 3+ exchange addresses hold the token (exit risk)",
    )

    # ── Deployer Profiling ──
    deployer_profiling_enabled: bool = Field(
        default=True,
        description="Profile token deployer via Arkham contract intelligence",
    )
    deployer_known_builder_bonus: float = Field(
        default=0.10,
        ge=0.0,
        le=0.30,
        description="Score bonus when deployer is a known/reputable entity",
    )
    deployer_scam_reject: bool = Field(
        default=True,
        description="Hard reject tokens whose deployer has scam/exploit/sanctioned tags",
    )
    deployer_proxy_penalty: float = Field(
        default=0.08,
        ge=0.0,
        le=0.20,
        description="Score penalty for upgradeable proxy contracts (rug vector)",
    )

    # ── Token Flow Monitoring ──
    flow_monitoring_enabled: bool = Field(
        default=True,
        description="Monitor token flows for dump/accumulation detection on alerted tokens",
    )
    flow_poll_interval_seconds: int = Field(
        default=300,
        ge=60,
        description="How often to check flows for monitored tokens (seconds)",
    )
    flow_min_usd: float = Field(
        default=5_000.0,
        description="Minimum USD flow from a known entity to consider significant",
    )
    flow_alert_threshold_usd: float = Field(
        default=10_000.0,
        description="Total known-entity flow above this triggers an alert",
    )

    # ── Portfolio Watch ──
    portfolio_watch_enabled: bool = Field(
        default=True,
        description="Periodically check Tier 1 wallet portfolios for new positions",
    )
    portfolio_poll_interval_seconds: int = Field(
        default=600,
        ge=120,
        description="How often to scan smart wallet portfolios (seconds)",
    )
    portfolio_min_position_usd: float = Field(
        default=500.0,
        description="Minimum position size in USD to consider as a signal",
    )


class TelegramConfig(BaseModel):
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    disable_notification: bool = False
    parse_mode: str = "HTML"


class NansenConfig(BaseModel):
    """NansenBot Telegram listener via Telethon.

    Actively listens to @NansenBot (user ID 1593218799) using the
    Telegram *client* API (Telethon).  Requires a user session with
    api_id + api_hash from https://my.telegram.org.

    The Telegram account must have an existing chat with @NansenBot.
    On first run, Telethon will interactively ask for phone + code.
    After that the session file is reused.
    """

    enabled: bool = Field(
        default=False,
        description="Enable NansenBot Telethon listener",
    )
    api_id: Optional[int] = Field(
        default=None,
        description="Telegram API ID from https://my.telegram.org (required)",
    )
    api_hash: Optional[str] = Field(
        default=None,
        description="Telegram API hash from https://my.telegram.org (required)",
    )
    session_path: str = Field(
        default="data/nansen_session",
        description="Path for the Telethon .session file (no extension)",
    )
    bot_user_id: int = Field(
        default=1593218799,
        description="@NansenBot Telegram user ID",
    )
    forward_alerts: bool = Field(
        default=False,
        description="Forward parsed Nansen signals as alerts (False = enrichment only)",
    )
    auto_add_wallets: bool = Field(
        default=True,
        description="Auto-add Nansen wallets to smart_wallets + watchlist",
    )
    base_only: bool = Field(
        default=True,
        description="Only process Base chain signals (ignore ETH/Polygon/etc)",
    )


class WalletMonitorConfig(BaseModel):
    """On-chain wallet monitoring via BaseScan API.

    Polls BaseScan for ERC-20 token transfers from tracked wallets.
    When a watched wallet buys a token:
    - Records the swap in wallet_swaps table
    - Sends a Telegram alert
    - If the token is in our DB: conviction signal → score boost
    """

    enabled: bool = Field(
        default=True,
        description="Enable on-chain wallet monitoring via BaseScan",
    )
    basescan_api_url: str = Field(
        default="https://api.basescan.org/api",
        description="BaseScan API base URL",
    )
    basescan_api_key: Optional[str] = Field(
        default=None,
        description="BaseScan API key (free tier: 5 req/s). Get from basescan.org",
    )
    wallet_file: str = Field(
        default="data/smart_money_wallets.txt",
        description="Path to wallet list file (one address per line)",
    )
    poll_interval_seconds: int = Field(
        default=60,
        ge=30,
        description="How often to poll BaseScan for new transfers (seconds)",
    )
    lookback_blocks: int = Field(
        default=150,
        ge=10,
        description="How many blocks back to check on each poll (~5 min at 2s/block)",
    )
    min_usd_value: float = Field(
        default=50.0,
        description="Minimum USD value of swap to consider (filter dust)",
    )
    weth_address: str = Field(
        default="0x4200000000000000000000000000000000000006",
        description="WETH contract on Base (used to identify buy direction)",
    )
    stable_addresses: list[str] = Field(
        default_factory=lambda: [
            "0x4200000000000000000000000000000000000006",  # WETH
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
            "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
        ],
        description="Base chain stablecoin/WETH addresses (outgoing = buy signal)",
    )
    conviction_score_bonus: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="Score bonus when a tracked wallet buys a token in our DB",
    )
    batch_size: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of wallets to poll per cycle (BaseScan rate limit)",
    )
    rate_delay_seconds: float = Field(
        default=0.25,
        description="Delay between BaseScan API calls (respect rate limit)",
    )


class ChampagneEvalConfig(BaseModel):
    """Champagne token re-evaluation — catch gems that lacked DEX data initially.

    Only 3/390 champagne tokens were alerted. Most had no DEX data when
    first scored. This loop re-evaluates champagne tokens at intervals
    up to max_age_hours after discovery with a lower threshold.
    """

    enabled: bool = Field(
        default=True,
        description="Enable champagne re-evaluation loop",
    )
    reeval_interval_seconds: int = Field(
        default=600,
        ge=60,
        description="How often to re-check champagne tokens (10 min default)",
    )
    max_age_hours: int = Field(
        default=3,
        ge=1,
        description="Stop re-evaluating champagne tokens older than this",
    )


class PaperTradingConfig(BaseModel):
    """Simulated paper trading — auto-enter positions on alerts.

    Tracks simulated positions with SL/TP rules to measure actual
    bot profitability without risking real capital.
    """

    enabled: bool = Field(
        default=True,
        description="Enable auto paper trading on every alert",
    )
    position_size_usd: float = Field(
        default=1000.0,
        description="Simulated investment per alert (USD)",
    )
    stop_loss_pct: float = Field(
        default=-30.0,
        description="Stop loss trigger % (negative number, e.g. -30)",
    )
    tp1_pct: float = Field(
        default=50.0,
        description="Take profit 1 trigger % — sell 33% of position",
    )
    tp2_pct: float = Field(
        default=100.0,
        description="Take profit 2 trigger % — sell 33% of position",
    )
    tp3_pct: float = Field(
        default=300.0,
        description="Take profit 3 trigger % — sell remaining",
    )
    time_stop_hours: int = Field(
        default=24,
        description="Close position if flat (no TP hit) after N hours",
    )
    check_interval_seconds: int = Field(
        default=300,
        ge=60,
        description="How often to check open positions (5 min default)",
    )
    max_open_positions: int = Field(
        default=20,
        ge=1,
        description="Maximum simultaneous open paper positions",
    )


class MultiWalletConvictionConfig(BaseModel):
    """Multi-wallet conviction detection.

    When 2+ tracked wallets buy the same token within a time window,
    fire a high-priority conviction alert. This is the strongest signal.
    """

    enabled: bool = Field(
        default=True,
        description="Enable multi-wallet conviction detection",
    )
    min_wallets: int = Field(
        default=2,
        ge=2,
        description="Minimum distinct wallets buying same token to trigger",
    )
    window_hours: int = Field(
        default=6,
        ge=1,
        description="Time window to look for multiple wallet buys",
    )
    auto_ingest_from_wallet_buy: bool = Field(
        default=True,
        description="When wallet buys a token not in DB, auto-ingest via DexScreener",
    )
    auto_ingest_min_liq: float = Field(
        default=10_000.0,
        description="Minimum liquidity for auto-ingested tokens from wallet buys",
    )


class MilestoneTrackerConfig(BaseModel):
    """Milestone tracker — monitors alerted tokens for ATH / multiplier milestones.

    Every scan_interval_seconds, fetches live DEX data for all non-dead
    alerted tokens. Notifies on:
    - New all-time-high MCap
    - Whole-number multiplier thresholds (1x, 2x, 3x, ...) vs alert FDV/MCap

    Marks tokens as dead when liquidity or MCap drops below threshold
    for dead_confirmation_hours, stopping further monitoring.
    """

    enabled: bool = Field(
        default=True,
        description="Enable milestone tracker loop",
    )
    scan_interval_seconds: int = Field(
        default=300,
        ge=60,
        description="How often to scan alerted tokens for milestones (5 min default)",
    )
    dead_liq_threshold_usd: float = Field(
        default=200.0,
        description="Liquidity below this marks token as potentially dead ($)",
    )
    dead_mcap_threshold_usd: float = Field(
        default=500.0,
        description="MCap below this marks token as potentially dead ($)",
    )
    dead_confirmation_hours: int = Field(
        default=24,
        ge=1,
        description="Hours a token must stay below thresholds before marked dead",
    )
    max_token_age_days: int = Field(
        default=30,
        ge=1,
        description="Stop tracking tokens older than this (days since alert)",
    )
    batch_size: int = Field(
        default=30,
        ge=1,
        le=30,
        description="Max tokens per DexScreener batch (API limit: 30)",
    )
    notify_ath: bool = Field(
        default=True,
        description="Send Telegram alert on new all-time-high MCap",
    )
    notify_multiplier: bool = Field(
        default=True,
        description="Send Telegram alert on new multiplier milestone (2x, 3x, ...)",
    )
    min_multiplier_notify: int = Field(
        default=2,
        ge=1,
        description="Minimum multiplier to start notifying (1=notify from 1x, 2=from 2x)",
    )
    ath_cooldown_seconds: int = Field(
        default=3600,
        ge=300,
        description="Minimum time between ATH notifications for the same token",
    )


class BinanceSkillsConfig(BaseModel):
    """Binance Skills Hub integration for Base chain intelligence.

    Uses multiple public Binance Web3 Skills APIs:
    1. Unified Token Rank — trending / top-search tokens on Base (chain 8453)
    2. Social Hype — social buzz leaderboard with AI sentiment summaries
    3. Token Security Audit — honeypot / scam / rug detection
    4. Token Dynamic Data — rich market data with KOL / smart money holder %
    5. Token Search — cross-chain token lookup by keyword / address
    6. Wallet Balance — on-chain wallet token positions

    All endpoints are public (no API key required).
    Docs: https://developers.binance.com/en/skills
    """

    enabled: bool = Field(
        default=True,
        description="Enable Binance Skills Hub integration",
    )

    # ── Trending scanner settings ──
    trending_poll_interval_seconds: int = Field(
        default=180,
        ge=60,
        description="How often to scan Binance for trending Base tokens (seconds)",
    )
    trending_min_mcap: float = Field(
        default=10_000.0,
        description="Min market cap for trending token discovery ($)",
    )
    trending_max_mcap: float = Field(
        default=50_000_000.0,
        description="Max market cap for trending tokens (skip blue chips)",
    )
    trending_min_liquidity: float = Field(
        default=5_000.0,
        description="Min liquidity for trending token discovery ($)",
    )
    trending_score_bonus: float = Field(
        default=0.05,
        ge=0.0,
        le=0.50,
        description="Flat score bonus for tokens discovered via Binance trending",
    )

    # ── Token security audit settings ──
    audit_enabled: bool = Field(
        default=True,
        description="Run Binance token security audit during scoring",
    )
    audit_max_risk_level: int = Field(
        default=3,
        ge=0,
        le=5,
        description="Max risk level to allow (0-1=LOW, 2-3=MEDIUM, 4=HIGH, 5=BLOCKED)",
    )
    audit_high_tax_pct: float = Field(
        default=10.0,
        description="Buy/sell tax above this % triggers a warning",
    )
    audit_score_bonus: float = Field(
        default=0.05,
        ge=0.0,
        le=0.20,
        description="Score bonus for tokens passing security audit (LOW risk)",
    )
    audit_penalty: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="Score penalty for tokens with MEDIUM risk audit",
    )

    # ── Enrichment settings (Token Dynamic Data) ──
    enrich_enabled: bool = Field(
        default=True,
        description="Fetch rich holder data (KOL/smart money/pro) from Binance during scoring",
    )
    kol_holder_bonus: float = Field(
        default=0.05,
        ge=0.0,
        le=0.20,
        description="Score bonus when token has KOL holders",
    )
    smart_money_holder_bonus: float = Field(
        default=0.08,
        ge=0.0,
        le=0.20,
        description="Score bonus when token has Binance-tracked smart money holders",
    )

    # ── Rate limiting ──
    request_delay_seconds: float = Field(
        default=0.5,
        ge=0.1,
        description="Minimum delay between Binance API calls (seconds)",
    )


class MinaraConfig(BaseModel):
    """Minara Agent API integration for pre-alert thesis and decisioning.

    Uses the Developer Chat endpoint:
    POST https://api-developer.minara.ai/v1/developer/chat
    """

    enabled: bool = Field(
        default=False,
        description="Enable Minara analysis before Telegram new-token gem alerts only",
    )
    auth_method: Literal["api_key", "x402"] = Field(
        default="api_key",
        description="Minara auth method: api_key subscription or x402 pay-as-you-go",
    )
    api_key: Optional[str] = Field(
        default=None,
        description="Minara API key. Prefer MINARA_API_KEY env var.",
    )
    base_url: str = Field(
        default="https://api-developer.minara.ai",
        description="Minara Developer API base URL",
    )
    x402_base_url: str = Field(
        default="https://x402.minara.ai",
        description="Minara x402 API base URL",
    )
    x402_chain: Literal["base", "polygon"] = Field(
        default="base",
        description="Payment chain for x402 chat endpoints",
    )
    x402_evm_private_key: Optional[str] = Field(
        default=None,
        description="EVM private key for Base/Polygon x402 payments. Prefer EVM_PRIVATE_KEY env var.",
    )
    mode: Literal["fast", "expert"] = Field(
        default="fast",
        description="Minara model mode: fast or expert",
    )
    timeout_seconds: float = Field(
        default=15.0,
        ge=1.0,
        description="HTTP timeout for Minara analysis calls",
    )
    include_thesis: bool = Field(
        default=True,
        description="Append Minara thesis to Telegram alerts",
    )
    gate_alerts: bool = Field(
        default=False,
        description="If true, block alerts when Minara decision is not allowed",
    )
    fail_open: bool = Field(
        default=True,
        description="If Minara fails, still send alerts unless this is false",
    )
    allowed_decisions: list[str] = Field(
        default_factory=lambda: ["BUY", "WATCH"],
        description="Decisions allowed through when gate_alerts is true",
    )
    min_confidence: int = Field(
        default=60,
        ge=0,
        le=100,
        description="Minimum Minara confidence required when gate_alerts is true",
    )
    max_thesis_chars: int = Field(
        default=700,
        ge=100,
        le=2000,
        description="Max Minara thesis characters appended to Telegram alerts",
    )


class ScrapingConfig(BaseModel):
    """Settings for the context resolver scraper."""
    clanker_page_url: str = "https://www.clanker.world/clanker/{address}"
    ddg_search_template: str = "${symbol} bankrbot site:x.com"
    request_timeout_seconds: int = 15
    max_retries: int = 2


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

class AppConfig(BaseModel):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    clanker: ClankerAPIConfig = Field(default_factory=ClankerAPIConfig)
    backfill: BackfillConfig = Field(default_factory=BackfillConfig)
    bankr: BankrConfig = Field(default_factory=BankrConfig)
    dexscreener: DexScreenerConfig = Field(default_factory=DexScreenerConfig)
    base_rpc: BaseRPCConfig = Field(default_factory=BaseRPCConfig)
    filtering: FilteringConfig = Field(default_factory=FilteringConfig)
    breakout: BreakoutConfig = Field(default_factory=BreakoutConfig)
    gainers: GainersConfig = Field(default_factory=GainersConfig)
    pid: PIDConfig = Field(default_factory=PIDConfig)
    arkham: ArkhamConfig = Field(default_factory=ArkhamConfig)
    wallet_monitor: WalletMonitorConfig = Field(default_factory=WalletMonitorConfig)
    nansen: NansenConfig = Field(default_factory=NansenConfig)
    champagne_eval: ChampagneEvalConfig = Field(default_factory=ChampagneEvalConfig)
    paper_trading: PaperTradingConfig = Field(default_factory=PaperTradingConfig)
    multi_conviction: MultiWalletConvictionConfig = Field(default_factory=MultiWalletConvictionConfig)
    binance_skills: BinanceSkillsConfig = Field(default_factory=BinanceSkillsConfig)
    minara: MinaraConfig = Field(default_factory=MinaraConfig)
    milestone_tracker: MilestoneTrackerConfig = Field(default_factory=MilestoneTrackerConfig)
    gate_pending: GatePendingConfig = Field(default_factory=GatePendingConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    scraping: ScrapingConfig = Field(default_factory=ScrapingConfig)
    log_level: str = "INFO"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _env_override(cfg: dict) -> dict:
    """Override selected fields with environment variables when present."""
    mapping = {
        "CLANKER_API_KEY": ("clanker", "api_key"),
        "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
        "TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
        "DATABASE_URL": ("database", "url"),
        "BASE_RPC_WS": ("base_rpc", "ws_url"),
        "DB_POOL_SIZE": ("database", "pool_size"),
        "ARKHAM_API_KEY": ("arkham", "api_key"),
        "BASESCAN_API_KEY": ("wallet_monitor", "basescan_api_key"),
        "NANSEN_API_ID": ("nansen", "api_id"),
        "NANSEN_API_HASH": ("nansen", "api_hash"),
        "MINARA_AUTH_METHOD": ("minara", "auth_method"),
        "MINARA_API_KEY": ("minara", "api_key"),
        "EVM_PRIVATE_KEY": ("minara", "x402_evm_private_key"),
    }
    for env_var, path in mapping.items():
        value = os.environ.get(env_var)
        if value is not None and value != "":
            section, key = path
            cfg.setdefault(section, {})[key] = value
    return cfg


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load configuration from a YAML file, with env-var overrides."""
    path = Path(path)
    raw: dict = {}
    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    raw = _env_override(raw)
    return AppConfig(**raw)
