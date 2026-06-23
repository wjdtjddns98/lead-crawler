"""add app_user.role + review_queue audit cols + review_audit table

Revision ID: f7a3d2e9c1b4
Revises: e6f2b8c1a3d9
Create Date: 2026-06-23 10:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a3d2e9c1b4"
down_revision: str | None = "e6f2b8c1a3d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # RBAC: 권한 역할(admin/worker). 기존 계정은 worker 기본(보수적 — 권한 최소화).
    op.add_column(
        "app_user",
        sa.Column("role", sa.String(length=16), nullable=False, server_default="worker"),
    )

    # 감사: 큐 행에 마지막 처리자 FK + 처리 시각.
    op.add_column("review_queue", sa.Column("assignee_id", sa.String(length=32), nullable=True))
    op.add_column(
        "review_queue", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_review_queue_assignee_id", "review_queue", ["assignee_id"]
    )
    op.create_foreign_key(
        "fk_review_queue_assignee_id_app_user",
        "review_queue",
        "app_user",
        ["assignee_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # 감사 이력(append-only) — confirm/reject 1건마다 적재.
    op.create_table(
        "review_audit",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("review_id", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=32), nullable=True),
        sa.Column(
            "actor_username",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("selected", sa.String(length=320), nullable=True),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["review_id"], ["review_queue.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["app_user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_audit_review_id", "review_audit", ["review_id"])
    op.create_index("ix_review_audit_actor_id", "review_audit", ["actor_id"])
    op.create_index("ix_review_audit_at", "review_audit", ["at"])


def downgrade() -> None:
    op.drop_index("ix_review_audit_at", table_name="review_audit")
    op.drop_index("ix_review_audit_actor_id", table_name="review_audit")
    op.drop_index("ix_review_audit_review_id", table_name="review_audit")
    op.drop_table("review_audit")

    op.drop_constraint(
        "fk_review_queue_assignee_id_app_user", "review_queue", type_="foreignkey"
    )
    op.drop_index("ix_review_queue_assignee_id", table_name="review_queue")
    op.drop_column("review_queue", "reviewed_at")
    op.drop_column("review_queue", "assignee_id")

    op.drop_column("app_user", "role")
