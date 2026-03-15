"""Add alert_message_id to alert_outcomes for Telegram reply threading.

Stores the Telegram message_id of the original alert so milestone
and dead-token notifications can be sent as replies to the alert.

Revision ID: 008
Revises: 007
Create Date: 2026-03-15
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert_outcomes",
        sa.Column(
            "alert_message_id",
            sa.Integer(),
            nullable=True,
            comment="Telegram message_id of the original alert — for reply threading",
        ),
    )


def downgrade() -> None:
    op.drop_column("alert_outcomes", "alert_message_id")
