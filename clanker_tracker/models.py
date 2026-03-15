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
    symbol: Mapped[Optional[str]] = mapped_column(String(256))
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

    # Breakout detection (delayed mover found via DexScreener trending)
    is_breakout: Mapped[bool] = mapped_column(
        Boolean, default=False,
        comment="Token discovered via breakout scanner (DexScreener trending/boosted)",
    )
    discovery_source: Mapped[Optional[str]] = mapped_column(
        String(30), default="firehose",
        comment="'firehose' | 'champagne' | 'breakout_boost' | 'breakout_profile' | 'breakout_trending'",
    )

    # Quality scoring
    quality_score: Mapped[Optional[float]] = mapped_column(Float)
    filter_stage_reached: Mapped[Optional[int]] = mapped_column(Integer)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)

    # Notification state
    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False)

    # Dead token tracking (milestone tracker cleanup)
    is_dead: Mapped[bool] = mapped_column(
        Boolean, default=False,
        comment="Token marked dead by milestone tracker (liq < $200 or mcap < $500 for 24h)",
    )
    dead_since: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        comment="When the token was first detected as dead",
    )

    # Gate-pending re-scan (token passed scoring but MCap/Liq too low)
    gate_pending: Mapped[bool] = mapped_column(
        Boolean, default=False,
        comment="Token passed scoring but failed MCap/Liq gate — pending re-check",
    )
    gate_check_count: Mapped[int] = mapped_column(
        Integer, default=0,
        comment="Number of times this token has been re-checked for gate passage",
    )
    last_gate_check: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        comment="When the token was last re-checked for gate passage",
    )

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
        Index("ix_tokens_gate_pending", "gate_pending"),
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
# AlertOutcome — PID feedback loop: track what happened AFTER we alerted
# ---------------------------------------------------------------------------

class AlertOutcome(Base):
    """Tracks the outcome of each alert for PID self-improvement.

    After alerting on a token, the bot re-checks it at 1h, 6h, 24h
    to classify the outcome as 'gem', 'survivor', or 'dead'.
    This feeds the PID controller that adjusts score_threshold.
    """
    __tablename__ = "alert_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="CASCADE"), unique=True,
    )

    # Metrics snapshot at the moment of alert
    alert_score: Mapped[Optional[float]] = mapped_column(Float)
    alert_mcap: Mapped[Optional[float]] = mapped_column(Float)
    alert_fdv: Mapped[Optional[float]] = mapped_column(Float)
    alert_liq: Mapped[Optional[float]] = mapped_column(Float)
    alert_vol_1h: Mapped[Optional[float]] = mapped_column(Float)
    alert_buys_1h: Mapped[Optional[int]] = mapped_column(Integer)
    alerted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    # 1-hour re-check
    check_1h_mcap: Mapped[Optional[float]] = mapped_column(Float)
    check_1h_liq: Mapped[Optional[float]] = mapped_column(Float)
    check_1h_vol: Mapped[Optional[float]] = mapped_column(Float)
    check_1h_buys: Mapped[Optional[int]] = mapped_column(Integer)
    checked_1h_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # 6-hour re-check
    check_6h_mcap: Mapped[Optional[float]] = mapped_column(Float)
    check_6h_liq: Mapped[Optional[float]] = mapped_column(Float)
    check_6h_vol: Mapped[Optional[float]] = mapped_column(Float)
    check_6h_buys: Mapped[Optional[int]] = mapped_column(Integer)
    checked_6h_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # 24-hour re-check
    check_24h_mcap: Mapped[Optional[float]] = mapped_column(Float)
    check_24h_liq: Mapped[Optional[float]] = mapped_column(Float)
    check_24h_vol: Mapped[Optional[float]] = mapped_column(Float)
    check_24h_buys: Mapped[Optional[int]] = mapped_column(Integer)
    checked_24h_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Final classification
    outcome: Mapped[Optional[str]] = mapped_column(
        String(20),
        comment="'gem' (mcap 2x+) | 'survivor' (still alive) | 'dead' (liq < $500)",
    )
    mcap_change_pct: Mapped[Optional[float]] = mapped_column(
        Float, comment="Best mcap vs alert mcap, as %",
    )

    # Milestone tracking — ATH + multiplier progress
    ath_mcap: Mapped[Optional[float]] = mapped_column(
        Float, comment="All-time high market cap observed since alert",
    )
    last_milestone_x: Mapped[Optional[int]] = mapped_column(
        Integer, default=0,
        comment="Highest whole-number multiplier notified (0=none, 1=1x, 2=2x, ...)",
    )
    milestone_notified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        comment="Timestamp of last milestone notification",
    )

    # Relationship
    token: Mapped["Token"] = relationship()

    __table_args__ = (
        Index("ix_outcomes_outcome", "outcome"),
        Index("ix_outcomes_alerted", "alerted_at"),
    )


# ---------------------------------------------------------------------------
# SmartWallet — tracked profitable wallets from Arkham Intel
# ---------------------------------------------------------------------------

class SmartWallet(Base):
    """A wallet tracked for smart money signals.

    Sourced from Arkham Intel 'fomo-user' tag, filtered by profitability.
    Used for:
    - Stage 4 smart money scoring (holder overlap)
    - Real-time conviction alerts (wallet buys known token)
    """
    __tablename__ = "smart_wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(
        String(42), unique=True, nullable=False, index=True,
    )
    chain: Mapped[str] = mapped_column(String(20), default="base")

    # Arkham metadata
    tag: Mapped[Optional[str]] = mapped_column(
        String(50), default="fomo-user",
        comment="Arkham tag that identified this wallet",
    )
    arkham_entity: Mapped[Optional[str]] = mapped_column(
        String(256), comment="Arkham entity name if labeled",
    )
    arkham_label: Mapped[Optional[str]] = mapped_column(
        String(256), comment="Arkham label for the address",
    )

    # Tier classification (based on profitability)
    tier: Mapped[Optional[int]] = mapped_column(
        Integer, default=3,
        comment="1=top performer, 2=consistent profit, 3=marginal",
    )

    # Performance metrics (USD flow-based PnL)
    pnl_1d_pct: Mapped[Optional[float]] = mapped_column(Float)
    pnl_7d_pct: Mapped[Optional[float]] = mapped_column(Float)
    pnl_30d_pct: Mapped[Optional[float]] = mapped_column(Float)
    balance_usd: Mapped[Optional[float]] = mapped_column(Float)
    volume_usd: Mapped[Optional[float]] = mapped_column(
        Float, comment="Total historical transaction volume in USD",
    )

    # Tracking state
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    profitable_periods: Mapped[Optional[int]] = mapped_column(
        Integer, default=0,
        comment="How many of 1d/7d/30d are profitable (0-3)",
    )

    # Timestamps
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )

    __table_args__ = (
        Index("ix_smart_wallets_tier", "tier"),
        Index("ix_smart_wallets_active", "is_active"),
        Index("ix_smart_wallets_tag", "tag"),
    )

    def __repr__(self) -> str:
        return f"<SmartWallet {self.address[:10]}… tier={self.tier}>"


# ---------------------------------------------------------------------------
# WalletSwap — recorded swaps from tracked wallets
# ---------------------------------------------------------------------------

class WalletSwap(Base):
    """Records a swap (buy/sell) made by a tracked smart wallet.

    Used for:
    - Conviction alerts (wallet buys token in our DB → boost signal)
    - Activity monitoring (wallet buying new tokens → new token alert)
    """
    __tablename__ = "wallet_swaps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(
        String(42), nullable=False, index=True,
    )

    # What was traded
    token_address: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    token_symbol: Mapped[Optional[str]] = mapped_column(String(256))
    token_name: Mapped[Optional[str]] = mapped_column(String(256))

    # Trade details
    action: Mapped[str] = mapped_column(
        String(10), comment="'buy' | 'sell'",
    )
    usd_value: Mapped[Optional[float]] = mapped_column(Float)
    unit_value: Mapped[Optional[float]] = mapped_column(Float)

    # Arkham reference
    tx_hash: Mapped[Optional[str]] = mapped_column(String(66), unique=True)
    block_timestamp: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
    )

    # Whether this triggered a conviction alert
    conviction_sent: Mapped[bool] = mapped_column(Boolean, default=False)

    # Timestamps
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_wallet_swaps_token", "token_address"),
        Index("ix_wallet_swaps_wallet_token", "wallet_address", "token_address"),
        Index("ix_wallet_swaps_recorded", "recorded_at"),
    )

    def __repr__(self) -> str:
        return f"<WalletSwap {self.wallet_address[:10]}… {self.action} {self.token_symbol}>"


# ---------------------------------------------------------------------------
# PaperPosition — simulated trading positions
# ---------------------------------------------------------------------------

class PaperPosition(Base):
    """Simulated paper trading position entered on every alert.

    Tracks entry, SL/TP exits, and final PnL for measuring bot
    profitability without risking real capital.
    """
    __tablename__ = "paper_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tokens.id", ondelete="CASCADE"),
    )

    # Entry
    entry_price_usd: Mapped[Optional[float]] = mapped_column(Float)
    entry_mcap: Mapped[Optional[float]] = mapped_column(Float)
    entry_fdv: Mapped[Optional[float]] = mapped_column(Float)
    entry_liq: Mapped[Optional[float]] = mapped_column(Float)
    position_size_usd: Mapped[float] = mapped_column(Float, default=1000.0)
    remaining_size_pct: Mapped[float] = mapped_column(
        Float, default=100.0,
        comment="% of original position still held (0-100)",
    )

    # Exit tracking
    status: Mapped[str] = mapped_column(
        String(20), default="open",
        comment="'open' | 'tp1' | 'tp2' | 'tp3' | 'sl' | 'time_stop' | 'closed'",
    )
    realized_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl_usd: Mapped[Optional[float]] = mapped_column(Float)

    # Price tracking
    highest_price_usd: Mapped[Optional[float]] = mapped_column(Float)
    lowest_price_usd: Mapped[Optional[float]] = mapped_column(Float)
    current_price_usd: Mapped[Optional[float]] = mapped_column(Float)
    last_check_mcap: Mapped[Optional[float]] = mapped_column(Float)

    # Timestamps
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Relationship
    token: Mapped["Token"] = relationship()

    __table_args__ = (
        Index("ix_paper_positions_status", "status"),
        Index("ix_paper_positions_opened", "opened_at"),
    )

    def __repr__(self) -> str:
        return f"<PaperPosition token_id={self.token_id} status={self.status} pnl={self.realized_pnl_usd:.2f}>"


# ---------------------------------------------------------------------------
# Engine / session helpers
# ---------------------------------------------------------------------------

def create_engine(
    database_url: str,
    echo: bool = False,
    pool_size: int = 10,
    max_overflow: int = 20,
    pool_recycle: int = 3600,
):
    """Create an async engine with connection pooling.

    For PostgreSQL (asyncpg), uses QueuePool with configurable size.
    For SQLite (aiosqlite), pooling kwargs are ignored by the driver.
    """
    kwargs: dict = {"echo": echo}
    # Only set pool params for non-SQLite (SQLite uses StaticPool)
    if "sqlite" not in database_url:
        kwargs.update(
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_recycle=pool_recycle,
            pool_pre_ping=True,  # Verify connections before use
        )
    return create_async_engine(database_url, **kwargs)


def create_session_factory(engine) -> async_sessionmaker:
    """Create an async session factory bound to *engine*."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine) -> None:
    """Create all tables (safe to call repeatedly).

    In production with Alembic, this is a no-op fallback.
    Use `alembic upgrade head` for schema migrations.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
