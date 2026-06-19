"""add email_validation.smtp

Revision ID: b2f4a1c9d3e7
Revises: 5b7ef028de56
Create Date: 2026-06-19 14:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2f4a1c9d3e7"
down_revision: str | None = "5b7ef028de56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SMTP RCPT 프로브 결과(nullable): True=수신확정, False=없음, NULL=미시도/판정불가.
    op.add_column("email_validation", sa.Column("smtp", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("email_validation", "smtp")
