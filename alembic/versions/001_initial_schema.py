"""Initial schema — tokens, token_context, token_metrics

Revision ID: 001
Revises: 
Create Date: 2026-02-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── tokens table ──────────────────────────────────────────────
    op.create_table(
        "tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        # On-chain identity
        sa.Column("contract_address", sa.String(66), nullable=False, unique=True, index=True),
        sa.Column("chain", sa.String(20), server_default="base"),
        sa.Column("deployer_address", sa.String(42), nullable=True),
        sa.Column("pool_address", sa.String(66), nullable=True,
                   comment="Can be bytes32 pool ID in clanker_v4"),
        # Clanker metadata
        sa.Column("clanker_id", sa.Integer(), nullable=True, unique=True, index=True),
        sa.Column("name", sa.String(256), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("requestor_address", sa.String(42), nullable=True),
        sa.Column("social_media_urls", sa.Text(), nullable=True,
                   comment="JSON array of {name, link} objects"),
        # Tags & classification
        sa.Column("is_champagne", sa.Boolean(), server_default=sa.text("false"),
                   comment="Clanker champagne tag"),
        sa.Column("is_verified", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("launch_platform", sa.String(30), nullable=True,
                   comment="bankr|clawnch|farcaster|direct|unknown"),
        sa.Column("is_bankr_launch", sa.Boolean(), server_default=sa.text("false")),
        # Breakout detection
        sa.Column("is_breakout", sa.Boolean(), server_default=sa.text("false"),
                   comment="Token found via DexScreener trending/boosted"),
        sa.Column("discovery_source", sa.String(30), server_default="firehose",
                   comment="firehose|champagne|breakout_boost|breakout_profile|breakout_trending"),
        # Quality scoring
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("filter_stage_reached", sa.Integer(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        # Notification
        sa.Column("alert_sent", sa.Boolean(), server_default=sa.text("false")),
        # Timestamps
        sa.Column("launched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True),
                   server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                   server_default=sa.func.now()),
    )

    # Additional indexes for tokens
    op.create_index("ix_tokens_symbol", "tokens", ["symbol"])
    op.create_index("ix_tokens_requestor", "tokens", ["requestor_address"])
    op.create_index("ix_tokens_discovered", "tokens", ["discovered_at"])
    op.create_index("ix_tokens_champagne", "tokens", ["is_champagne"])
    op.create_index("ix_tokens_platform", "tokens", ["launch_platform"])
    op.create_index("ix_tokens_unscored", "tokens", ["quality_score", "discovered_at"],
                     postgresql_where=sa.text("quality_score IS NULL"))

    # ── token_context table ───────────────────────────────────────
    op.create_table(
        "token_context",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("token_id", sa.Integer(),
                   sa.ForeignKey("tokens.id", ondelete="CASCADE"),
                   unique=True, nullable=False),
        sa.Column("origin_url", sa.Text(), nullable=True),
        sa.Column("origin_platform", sa.String(20), nullable=True,
                   comment="x|farcaster|unknown"),
        sa.Column("resolution_strategy", sa.String(30), nullable=True,
                   comment="social_urls|page_scrape|ddg_search|unresolved"),
        sa.Column("origin_author", sa.String(256), nullable=True),
        sa.Column("origin_text", sa.Text(), nullable=True),
        sa.Column("project_idea", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                   server_default=sa.func.now()),
    )

    # ── token_metrics table ───────────────────────────────────────
    op.create_table(
        "token_metrics",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("token_id", sa.Integer(),
                   sa.ForeignKey("tokens.id", ondelete="CASCADE"),
                   nullable=False),
        # Price & market
        sa.Column("price_usd", sa.Float(), nullable=True),
        sa.Column("market_cap_usd", sa.Float(), nullable=True),
        sa.Column("fdv_usd", sa.Float(), nullable=True),
        sa.Column("liquidity_usd", sa.Float(), nullable=True),
        # Volume
        sa.Column("volume_5m_usd", sa.Float(), nullable=True),
        sa.Column("volume_1h_usd", sa.Float(), nullable=True),
        sa.Column("volume_24h_usd", sa.Float(), nullable=True),
        # Trading activity
        sa.Column("buys_5m", sa.Integer(), nullable=True),
        sa.Column("sells_5m", sa.Integer(), nullable=True),
        sa.Column("buys_1h", sa.Integer(), nullable=True),
        sa.Column("sells_1h", sa.Integer(), nullable=True),
        # Holders
        sa.Column("holder_count", sa.Integer(), nullable=True),
        sa.Column("smart_money_holders", sa.Integer(), server_default=sa.text("0"),
                   comment="Count of known smart-money wallets holding"),
        # Snapshot
        sa.Column("snapshot_at", sa.DateTime(timezone=True),
                   server_default=sa.func.now()),
    )

    op.create_index("ix_metrics_token_time", "token_metrics",
                     ["token_id", "snapshot_at"])


def downgrade() -> None:
    op.drop_table("token_metrics")
    op.drop_table("token_context")
    op.drop_table("tokens")
