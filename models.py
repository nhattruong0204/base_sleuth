"""Database models and async session factory."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import AppConfig


class Base(DeclarativeBase):
    pass


class Token(Base):
    """A Clanker-deployed token on Base."""

    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- Onchain identity ---
    contract_address: Mapped[str] = mapped_column(String(42), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    deployer_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    chain_id: Mapped[int] = mapped_column(Integer, default=8453)  # Base = 8453

    # --- Clanker metadata ---
    clanker_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    social_urls: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON list
    pool_address: Mapped[Optional[str]] = mapped_column(String(42), nullable=True)
    paired_token: Mapped[Optional[str]] = mapped_column(String(42), nullable=True)  # WETH usually
    starting_mcap_eth: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    warnings: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON list of warning flags

    # --- Source tracking ---
    is_bankr_origin: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    requestor_address: Mapped[Optional[str]] = mapped_column(String(42), nullable=True)

    # --- Timestamps ---
    deployed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # --- Quality scoring ---
    quality_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_tokens_deployed_at", "deployed_at"),
        Index("ix_tokens_bankr_deployed", "is_bankr_origin", "deployed_at"),
    )


class TokenContext(Base):
    """Traceback context linking a token to its X/Farcaster origin."""

    __tablename__ = "token_context"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)

    # --- X (Twitter) context ---
    x_tweet_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    x_author_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    x_author_display_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    x_tweet_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- Farcaster context ---
    farcaster_cast_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    farcaster_author: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    farcaster_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- Derived project info ---
    project_idea: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # LLM-summarized or extracted
    source_platform: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # "x", "farcaster", "web"

    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_method: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class TokenMetrics(Base):
    """Time-series metrics snapshots for a token."""

    __tablename__ = "token_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # --- Price & volume ---
    price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_eth: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mcap_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume_24h_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # --- Holder stats ---
    unique_holders: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    unique_buyers: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    unique_sellers: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    buy_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sell_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    top_holder_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # --- Smart money ---
    smart_money_buys: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_metrics_addr_time", "contract_address", "snapshot_at"),
    )


# ---- Engine + session factory ----

_engine = None
_session_factory = None


async def init_db(config: AppConfig) -> async_sessionmaker[AsyncSession]:
    """Create engine, ensure tables exist, return session factory."""
    global _engine, _session_factory

    dsn = config.database.dsn
    _engine = create_async_engine(dsn, echo=False, pool_pre_ping=True)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _session_factory


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory
