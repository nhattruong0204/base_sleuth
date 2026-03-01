"""Add paper_positions table for simulated trading.

Auto-enters paper positions on every alert to track SL/TP exits
and measure bot profitability without risking real capital.

Revision ID: 005
Revises: 004
Create Date: 2026-02-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "paper_positions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "token_id",
            sa.Integer(),
            sa.ForeignKey("tokens.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Entry
        sa.Column("entry_price_usd", sa.Float(), nullable=True),
        sa.Column("entry_mcap", sa.Float(), nullable=True),
        sa.Column("entry_fdv", sa.Float(), nullable=True),
        sa.Column("entry_liq", sa.Float(), nullable=True),
        sa.Column("position_size_usd", sa.Float(), nullable=False, server_default="1000.0"),
        sa.Column(
            "remaining_size_pct",
            sa.Float(),
            nullable=False,
            server_default="100.0",
            comment="% of original position still held (0-100)",
        ),
        # Exit tracking
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="open",
            comment="open | tp1 | tp2 | tp3 | sl | time_stop | closed",
        ),
        sa.Column("realized_pnl_usd", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("unrealized_pnl_usd", sa.Float(), nullable=True),
        # Price tracking
        sa.Column("highest_price_usd", sa.Float(), nullable=True),
        sa.Column("lowest_price_usd", sa.Float(), nullable=True),
        sa.Column("current_price_usd", sa.Float(), nullable=True),
        sa.Column("last_check_mcap", sa.Float(), nullable=True),
        # Timestamps
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index("ix_paper_positions_status", "paper_positions", ["status"])
    op.create_index("ix_paper_positions_opened", "paper_positions", ["opened_at"])


def downgrade() -> None:
    op.drop_index("ix_paper_positions_opened", table_name="paper_positions")
    op.drop_index("ix_paper_positions_status", table_name="paper_positions")
    op.drop_table("paper_positions")
