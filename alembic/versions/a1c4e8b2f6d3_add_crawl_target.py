"""add crawl_target (웹앱 관리자 크롤 타깃 설정)

Revision ID: a1c4e8b2f6d3
Revises: f7a3d2e9c1b4
Create Date: 2026-06-23 11:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c4e8b2f6d3"
down_revision: str | None = "f7a3d2e9c1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crawl_target",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("countries", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("industries", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("listed", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("persist", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("crawl_target")
