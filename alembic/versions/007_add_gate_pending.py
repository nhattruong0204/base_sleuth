"""Add gate-pending re-scan columns to tokens.

Tokens that pass the scoring pipeline but fail hard MCap/Liq gates
are marked gate_pending=True for periodic re-checking.

Revision ID: 007
Revises: 006
Create Date: 2026-03-15
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tokens",
        sa.Column(
            "gate_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="Token passed scoring but failed MCap/Liq gate — pending re-check",
        ),
    )
    op.add_column(
        "tokens",
        sa.Column(
            "gate_check_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="Number of times this token has been re-checked for gate passage",
        ),
    )
    op.add_column(
        "tokens",
        sa.Column(
            "last_gate_check",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the token was last re-checked for gate passage",
        ),
    )
    op.create_index("ix_tokens_gate_pending", "tokens", ["gate_pending"])


def downgrade() -> None:
    op.drop_index("ix_tokens_gate_pending", table_name="tokens")
    op.drop_column("tokens", "last_gate_check")
    op.drop_column("tokens", "gate_check_count")
    op.drop_column("tokens", "gate_pending")
