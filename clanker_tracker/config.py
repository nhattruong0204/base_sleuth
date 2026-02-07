"""Pydantic configuration models and YAML loader."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

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
    min_mcap_usd: float = 1_000.0

    # ── Stage 2 — DEX metrics (DexScreener batch lookup) ──
    min_pool_liquidity_usd: float = 1_000.0
    min_volume_1h_usd: float = 50.0
    min_buy_sell_ratio: float = 0.2
    min_holders: int = 3
    recheck_delay_seconds: int = Field(
        default=300,
        description="Wait N seconds after discovery before checking DEX metrics "
                    "(gives pools time to get indexed by DexScreener)",
    )

    # ── Stage 3 — momentum detection ──
    min_volume_5m_usd: float = 100.0
    min_buys_1h: int = 5
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
    weight_metrics: float = 1.0
    weight_momentum: float = 1.5
    weight_smart_money: float = 2.0
    weight_context: float = 1.0
    weight_champagne_bonus: float = Field(
        default=0.15,
        description="Flat score bonus for champagne-tagged tokens",
    )

    # ── Final threshold ──
    score_threshold: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        description="Minimum weighted score to trigger an alert",
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
        default=0.10,
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


class TelegramConfig(BaseModel):
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    disable_notification: bool = False
    parse_mode: str = "HTML"


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
    }
    for env_var, path in mapping.items():
        value = os.environ.get(env_var)
        if value is not None:
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
