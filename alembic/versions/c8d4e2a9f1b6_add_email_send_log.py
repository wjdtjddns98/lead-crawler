"""add email_send_log (아웃리치 발송 로그 — 재발송 방지·이력)

Revision ID: c8d4e2a9f1b6
Revises: b3d9f1a7c2e8
Create Date: 2026-06-24 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d4e2a9f1b6"
down_revision: str | None = "b3d9f1a7c2e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_send_log",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.String(length=40), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("sent_by", sa.String(length=64), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_send_log_email", "email_send_log", ["email"])
    op.create_index("ix_email_send_log_company_id", "email_send_log", ["company_id"])
    op.create_index("ix_email_send_log_status", "email_send_log", ["status"])
    op.create_index("ix_email_send_log_sent_at", "email_send_log", ["sent_at"])


def downgrade() -> None:
    op.drop_index("ix_email_send_log_sent_at", table_name="email_send_log")
    op.drop_index("ix_email_send_log_status", table_name="email_send_log")
    op.drop_index("ix_email_send_log_company_id", table_name="email_send_log")
    op.drop_index("ix_email_send_log_email", table_name="email_send_log")
    op.drop_table("email_send_log")
