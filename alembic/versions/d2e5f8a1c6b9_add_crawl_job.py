"""add crawl_job (직접 크롤 작업 상태·진행 카운터)

Revision ID: d2e5f8a1c6b9
Revises: c8d4e2a9f1b6
Create Date: 2026-06-24 14:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e5f8a1c6b9"
down_revision: str | None = "c8d4e2a9f1b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crawl_job",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("countries", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("industries", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("listed", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("persist", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("segments_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("segments_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enriched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("saved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("triggered_by", sa.String(length=64), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_crawl_job_status", "crawl_job", ["status"])
    op.create_index("ix_crawl_job_started_at", "crawl_job", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_crawl_job_started_at", table_name="crawl_job")
    op.drop_index("ix_crawl_job_status", table_name="crawl_job")
    op.drop_table("crawl_job")
