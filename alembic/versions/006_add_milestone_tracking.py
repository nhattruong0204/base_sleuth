"""Add milestone tracking columns.

- alert_outcomes: ath_mcap, last_milestone_x, milestone_notified_at
- tokens: is_dead, dead_since

Used by the milestone tracker loop to notify ATH / multiplier
milestones and clean up dead tokens.

Revision ID: 006
Revises: 005
Create Date: 2026-03-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # AlertOutcome milestone columns
    op.add_column(
        "alert_outcomes",
        sa.Column(
            "ath_mcap", sa.Float(), nullable=True,
            comment="All-time high market cap observed since alert",
        ),
    )
    op.add_column(
        "alert_outcomes",
        sa.Column(
            "last_milestone_x", sa.Integer(), nullable=True,
            server_default="0",
            comment="Highest whole-number multiplier notified (0=none, 1=1x, 2=2x, ...)",
        ),
    )
    op.add_column(
        "alert_outcomes",
        sa.Column(
            "milestone_notified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Timestamp of last milestone notification",
        ),
    )

    # Token dead tracking columns
    op.add_column(
        "tokens",
        sa.Column(
            "is_dead", sa.Boolean(), nullable=True,
            server_default=sa.text("false"),
            comment="Token marked dead by milestone tracker",
        ),
    )
    op.add_column(
        "tokens",
        sa.Column(
            "dead_since",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the token was first detected as dead",
        ),
    )


def downgrade() -> None:
    op.drop_column("tokens", "dead_since")
    op.drop_column("tokens", "is_dead")
    op.drop_column("alert_outcomes", "milestone_notified_at")
    op.drop_column("alert_outcomes", "last_milestone_x")
    op.drop_column("alert_outcomes", "ath_mcap")
