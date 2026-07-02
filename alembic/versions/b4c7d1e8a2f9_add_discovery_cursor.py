"""add discovery_cursor (등록처 발견 커서 — 런 간 스캔 위치 영속)

Revision ID: b4c7d1e8a2f9
Revises: f1a9c3d7b2e4
Create Date: 2026-07-02 09:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4c7d1e8a2f9"
down_revision: str | None = "f1a9c3d7b2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discovery_cursor",
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("cursor_key", sa.String(length=256), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("source", "cursor_key"),
    )


def downgrade() -> None:
    op.drop_table("discovery_cursor")
