"""Widen symbol column from String(32) to String(256).

Some Bankr bot tokens use the entire description as the symbol,
causing StringDataRightTruncationError on insert.

Revision ID: 002
Revises: 001
Create Date: 2026-02-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "tokens",
        "symbol",
        existing_type=sa.String(32),
        type_=sa.String(256),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "tokens",
        "symbol",
        existing_type=sa.String(256),
        type_=sa.String(32),
        existing_nullable=True,
    )
