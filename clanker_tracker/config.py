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
        default="sqlite+aiosqlite:///data/clanker_tracker.db",
        description="Async SQLAlchemy database URL",
    )
    echo: bool = False


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
    bankr: BankrConfig = Field(default_factory=BankrConfig)
    dexscreener: DexScreenerConfig = Field(default_factory=DexScreenerConfig)
    base_rpc: BaseRPCConfig = Field(default_factory=BaseRPCConfig)
    filtering: FilteringConfig = Field(default_factory=FilteringConfig)
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
