"""SQLAlchemy 2.0 async models for the Clanker Token Tracker.

Tables
------
- Token        : on-chain identity + Clanker metadata + quality score
- TokenContext  : X / Farcaster traceback (origin tweet / cast)
- TokenMetrics  : time-series metrics snapshots (price, volume, holders)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(AsyncAttrs, DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Token — core token record
# ---------------------------------------------------------------------------

class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # On-chain identity
    contract_address: Mapped[str] = mapped_column(
        String(66), unique=True, nullable=False, index=True,
    )
    chain: Mapped[str] = mapped_column(String(20), default="base")
    deployer_address: Mapped[Optional[str]] = mapped_column(String(42))
    pool_address: Mapped[Optional[str]] = mapped_column(
        String(66), comment="Can be bytes32 pool ID in clanker_v4",
    )

    # Clanker metadata
    clanker_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(256))
    symbol: Mapped[Optional[str]] = mapped_column(String(32))
    image_url: Mapped[Optional[str]] = mapped_column(Text)
    description: Mapped[Optional[str]] = mapped_column(Text)
    requestor_address: Mapped[Optional[str]] = mapped_column(String(42))

    # Social URLs from Clanker API (JSON — socialLinks array)
    social_media_urls: Mapped[Optional[str]] = mapped_column(
        Text, comment="JSON array of {name, link} objects from socialLinks",
    )

    # Clanker tags & classification
    is_champagne: Mapped[bool] = mapped_column(
        Boolean, default=False,
        comment="Clanker 'champagne' tag — curated quality token",
    )
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    launch_platform: Mapped[Optional[str]] = mapped_column(
        String(30),
        comment="'bankr' | 'clawnch' | 'farcaster' | 'direct' | 'unknown'",
    )

    # Bankr attribution (detected from description + deployer address)
    is_bankr_launch: Mapped[bool] = mapped_column(Boolean, default=False)

    # Quality scoring
    quality_score: Mapped[Optional[float]] = mapped_column(Float)
    filter_stage_reached: Mapped[Optional[int]] = mapped_column(Integer)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)

    # Notification state
    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False)

    # Timestamps
    launched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    context: Mapped[Optional["TokenContext"]] = relationship(
        back_populates="token", uselist=False, cascade="all, delete-orphan",
    )
    metrics: Mapped[list["TokenMetrics"]] = relationship(
        back_populates="token", cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_tokens_symbol", "symbol"),
        Index("ix_tokens_requestor", "requestor_address"),
        Index("ix_tokens_discovered", "discovered_at"),
        Index("ix_tokens_champagne", "is_champagne"),
        Index("ix_tokens_platform", "launch_platform"),
    )

    def __repr__(self) -> str:
        return f"<Token ${self.symbol} {self.contract_address[:10]}…>"


# ---------------------------------------------------------------------------
# TokenContext — origin traceback
# ---------------------------------------------------------------------------

class TokenContext(Base):
    __tablename__ = "token_context"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="CASCADE"), unique=True,
    )

    # Origin URL (X tweet or Farcaster cast)
    origin_url: Mapped[Optional[str]] = mapped_column(Text)
    origin_platform: Mapped[Optional[str]] = mapped_column(
        String(20), comment="'x' | 'farcaster' | 'unknown'",
    )

    # Resolution metadata
    resolution_strategy: Mapped[Optional[str]] = mapped_column(
        String(30),
        comment="'social_urls' | 'page_scrape' | 'ddg_search' | 'unresolved'",
    )
    origin_author: Mapped[Optional[str]] = mapped_column(String(256))
    origin_text: Mapped[Optional[str]] = mapped_column(Text)
    project_idea: Mapped[Optional[str]] = mapped_column(
        Text, comment="Extracted project description from origin post",
    )

    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Relationship
    token: Mapped["Token"] = relationship(back_populates="context")

    def __repr__(self) -> str:
        return f"<TokenContext token_id={self.token_id} strategy={self.resolution_strategy}>"


# ---------------------------------------------------------------------------
# TokenMetrics — time-series snapshots
# ---------------------------------------------------------------------------

class TokenMetrics(Base):
    __tablename__ = "token_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="CASCADE"),
    )

    # Price & market
    price_usd: Mapped[Optional[float]] = mapped_column(Float)
    market_cap_usd: Mapped[Optional[float]] = mapped_column(Float)
    fdv_usd: Mapped[Optional[float]] = mapped_column(Float)
    liquidity_usd: Mapped[Optional[float]] = mapped_column(Float)

    # Volume
    volume_5m_usd: Mapped[Optional[float]] = mapped_column(Float)
    volume_1h_usd: Mapped[Optional[float]] = mapped_column(Float)
    volume_24h_usd: Mapped[Optional[float]] = mapped_column(Float)

    # Trading activity
    buys_5m: Mapped[Optional[int]] = mapped_column(Integer)
    sells_5m: Mapped[Optional[int]] = mapped_column(Integer)
    buys_1h: Mapped[Optional[int]] = mapped_column(Integer)
    sells_1h: Mapped[Optional[int]] = mapped_column(Integer)

    # Holders
    holder_count: Mapped[Optional[int]] = mapped_column(Integer)

    # Smart money
    smart_money_holders: Mapped[Optional[int]] = mapped_column(
        Integer, default=0, comment="Count of known smart-money wallets holding",
    )

    # Snapshot timestamp
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Relationship
    token: Mapped["Token"] = relationship(back_populates="metrics")

    __table_args__ = (
        Index("ix_metrics_token_time", "token_id", "snapshot_at"),
    )

    def __repr__(self) -> str:
        return f"<TokenMetrics token_id={self.token_id} @{self.snapshot_at}>"


# ---------------------------------------------------------------------------
# Engine / session helpers
# ---------------------------------------------------------------------------

def create_engine(database_url: str, echo: bool = False):
    """Create an async engine."""
    return create_async_engine(database_url, echo=echo)


def create_session_factory(engine) -> async_sessionmaker:
    """Create an async session factory bound to *engine*."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine) -> None:
    """Create all tables (safe to call repeatedly)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
