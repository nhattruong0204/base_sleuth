"""Add alert_outcomes table for PID feedback loop.

Tracks what happens to each alerted token at 1h, 6h, 24h
so the PID controller can auto-tune score_threshold.

Revision ID: 003
Revises: 002
Create Date: 2026-02-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alert_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "token_id",
            sa.Integer(),
            sa.ForeignKey("tokens.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),

        # Alert-time snapshot
        sa.Column("alert_score", sa.Float(), nullable=True),
        sa.Column("alert_mcap", sa.Float(), nullable=True),
        sa.Column("alert_fdv", sa.Float(), nullable=True),
        sa.Column("alert_liq", sa.Float(), nullable=True),
        sa.Column("alert_vol_1h", sa.Float(), nullable=True),
        sa.Column("alert_buys_1h", sa.Integer(), nullable=True),
        sa.Column(
            "alerted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),

        # 1h re-check
        sa.Column("check_1h_mcap", sa.Float(), nullable=True),
        sa.Column("check_1h_liq", sa.Float(), nullable=True),
        sa.Column("check_1h_vol", sa.Float(), nullable=True),
        sa.Column("check_1h_buys", sa.Integer(), nullable=True),
        sa.Column("checked_1h_at", sa.DateTime(timezone=True), nullable=True),

        # 6h re-check
        sa.Column("check_6h_mcap", sa.Float(), nullable=True),
        sa.Column("check_6h_liq", sa.Float(), nullable=True),
        sa.Column("check_6h_vol", sa.Float(), nullable=True),
        sa.Column("check_6h_buys", sa.Integer(), nullable=True),
        sa.Column("checked_6h_at", sa.DateTime(timezone=True), nullable=True),

        # 24h re-check
        sa.Column("check_24h_mcap", sa.Float(), nullable=True),
        sa.Column("check_24h_liq", sa.Float(), nullable=True),
        sa.Column("check_24h_vol", sa.Float(), nullable=True),
        sa.Column("check_24h_buys", sa.Integer(), nullable=True),
        sa.Column("checked_24h_at", sa.DateTime(timezone=True), nullable=True),

        # Classification
        sa.Column("outcome", sa.String(20), nullable=True),
        sa.Column("mcap_change_pct", sa.Float(), nullable=True),
    )

    op.create_index("ix_outcomes_outcome", "alert_outcomes", ["outcome"])
    op.create_index("ix_outcomes_alerted", "alert_outcomes", ["alerted_at"])


def downgrade() -> None:
    op.drop_index("ix_outcomes_alerted", table_name="alert_outcomes")
    op.drop_index("ix_outcomes_outcome", table_name="alert_outcomes")
    op.drop_table("alert_outcomes")
