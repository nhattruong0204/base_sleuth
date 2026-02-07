"""Configuration models and loader."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    backend: Literal["sqlite", "postgresql"] = "sqlite"
    sqlite_path: str = "data/tracker.db"
    postgres_dsn: str = ""

    @property
    def dsn(self) -> str:
        if self.backend == "sqlite":
            return f"sqlite+aiosqlite:///{self.sqlite_path}"
        return self.postgres_dsn


class ClankerConfig(BaseModel):
    base_url: str = "https://www.clanker.world/api"
    poll_interval_seconds: int = 30
    page_size: int = 50


class BankrConfig(BaseModel):
    deployer_addresses: list[str] = Field(default_factory=list)

    def is_bankr_deployer(self, address: str) -> bool:
        return address.lower() in {a.lower() for a in self.deployer_addresses}


class ContextConfig(BaseModel):
    enabled: bool = True
    max_concurrency: int = 3
    request_delay_seconds: float = 2.0
    web_search_fallback: bool = True


class FilteringConfig(BaseModel):
    min_starting_mcap_eth: float = 1.0
    min_unique_buyers_5min: int = 5
    min_volume_eth_15min: float = 0.5
    max_top_holder_pct: float = 40.0
    min_buy_sell_ratio: float = 1.5
    smart_money_wallets_file: str = "data/smart_money_wallets.txt"
    smart_money_threshold: int = 2
    context_weight: float = 0.3


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    min_score_to_notify: int = 60
    cooldown_seconds: int = 30


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/tracker.log"
    json_format: bool = True


class AppConfig(BaseModel):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    clanker: ClankerConfig = Field(default_factory=ClankerConfig)
    bankr: BankrConfig = Field(default_factory=BankrConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    filtering: FilteringConfig = Field(default_factory=FilteringConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(path: str = "config.yaml") -> AppConfig:
    """Load config from YAML file, fall back to defaults if missing."""
    config_path = Path(path)
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        return AppConfig(**raw)
    return AppConfig()
