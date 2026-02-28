"""Add smart_wallets and wallet_swaps tables for Arkham Intel tracking.

Stores profitable wallets from Arkham Intel's fomo-user tag,
and records their swap activity for conviction detection.

Revision ID: 004
Revises: 003
Create Date: 2026-02-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── smart_wallets table ──
    op.create_table(
        "smart_wallets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("address", sa.String(42), unique=True, nullable=False, index=True),
        sa.Column("chain", sa.String(20), nullable=False, server_default="base"),
        sa.Column("tag", sa.String(50), nullable=False, server_default="fomo-user"),
        sa.Column("arkham_entity", sa.String(200), nullable=True),
        sa.Column("arkham_label", sa.String(200), nullable=True),
        sa.Column("tier", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("pnl_1d_pct", sa.Float(), nullable=True),
        sa.Column("pnl_7d_pct", sa.Float(), nullable=True),
        sa.Column("pnl_30d_pct", sa.Float(), nullable=True),
        sa.Column("balance_usd", sa.Float(), nullable=True),
        sa.Column("volume_usd", sa.Float(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("profitable_periods", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_smart_wallets_tier", "smart_wallets", ["tier"])
    op.create_index("ix_smart_wallets_is_active", "smart_wallets", ["is_active"])
    op.create_index("ix_smart_wallets_tag", "smart_wallets", ["tag"])

    # ── wallet_swaps table ──
    op.create_table(
        "wallet_swaps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("wallet_address", sa.String(42), nullable=False, index=True),
        sa.Column("token_address", sa.String(66), nullable=False, index=True),
        sa.Column("token_symbol", sa.String(50), nullable=True),
        sa.Column("token_name", sa.String(200), nullable=True),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("usd_value", sa.Float(), nullable=True),
        sa.Column("unit_value", sa.Float(), nullable=True),
        sa.Column("tx_hash", sa.String(66), unique=True, nullable=True),
        sa.Column("block_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("conviction_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_wallet_swaps_wallet_token",
        "wallet_swaps",
        ["wallet_address", "token_address"],
    )
    op.create_index("ix_wallet_swaps_recorded_at", "wallet_swaps", ["recorded_at"])


def downgrade() -> None:
    op.drop_table("wallet_swaps")
    op.drop_table("smart_wallets")
