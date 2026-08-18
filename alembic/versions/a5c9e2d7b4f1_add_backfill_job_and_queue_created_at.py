"""backfill_job 테이블 + review_queue.created_at (#352 PR②)

웹 대시보드 백필 제어의 상태 정본 테이블(backfill_job — 자식 프로세스 세대 감독,
active_track 유니크로 트랙별 활성 1건 강제)과, 전체큐 LIFO 의 정밀 정렬 키
(review_queue.created_at — 적재시각)를 추가한다. 기존 큐 행의 created_at 은 종전
근사키였던 발견 원장 first_seen 으로 백필해 현행 표시 순서를 보존한다(회사 미연결
행 등 조회 불가면 컬럼 기본값 = 마이그레이션 시각).

Revision ID: a5c9e2d7b4f1
Revises: d4b1c8e6f2a9
Create Date: 2026-08-18 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a5c9e2d7b4f1"
down_revision: str | None = "d4b1c8e6f2a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backfill_job",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("track", sa.String(8), nullable=False),
        sa.Column("active_track", sa.String(8), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default=sa.text("'running'")),
        sa.Column("countries", sa.String(256), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "exclude_industries", sa.String(1024), nullable=False, server_default=sa.text("''")
        ),
        sa.Column(
            "exclude_listed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("batch", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("workers", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_batches", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("min_queue", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("initial_target", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("remaining", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("processed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("resolved", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("promoted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("emails", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("batches_done", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("recycles", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("crash_restarts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column(
            "cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("stop_reason", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.String(64), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("active_track", name="uq_backfill_job_active_track"),
    )
    op.create_index("ix_backfill_job_track", "backfill_job", ["track"])
    op.create_index("ix_backfill_job_status", "backfill_job", ["status"])
    op.create_index("ix_backfill_job_started_at", "backfill_job", ["started_at"])

    created_at_col = sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False,
        server_default=sa.func.now(),
    )
    if op.get_bind().dialect.name == "sqlite":
        # SQLite 는 행이 있는 테이블에 비상수 기본값(now()) ADD COLUMN 을 거부한다
        # ("Cannot add a column with non-constant default", 2026-08-18 Codex HIGH-2 실증)
        # — 테이블 재생성(batch recreate)으로 우회한다. 운영(PG)은 아래 일반 경로.
        with op.batch_alter_table("review_queue", recreate="always") as batch:
            batch.add_column(created_at_col)
    else:
        # PG 주의: 비상수 default 라 메타데이터 최적화가 안 먹혀 짧은 테이블 rewrite 락
        # (수만 행 = 수 초)이 걸린다 — 배포는 한산 시간대에.
        op.add_column("review_queue", created_at_col)
    op.create_index("ix_review_queue_created_at", "review_queue", ["created_at"])
    _backfill_created_at(op.get_bind())


# 기존 행 백필 — 종전 LIFO 근사키(발견 first_seen)로 현행 표시 순서 보존. 표준 상관
# 서브쿼리(별칭 없는 UPDATE)라 PostgreSQL·SQLite 양쪽에서 동작한다. canonical_key 는
# company=UNIQUE·discovered=PK 라 서브쿼리는 항상 단일행-또는-NULL(다중행 위험 없음).
_BACKFILL_SQL = """
    update review_queue set created_at = coalesce(
        (select d.first_seen
           from company c
           join discovered_company d on d.canonical_key = c.canonical_key
          where c.id = review_queue.company_id),
        created_at)
    """


def _backfill_created_at(conn) -> None:  # noqa: ANN001 (Connection)
    """created_at 을 발견 first_seen 으로 백필한다 — 테스트가 직접 호출해 검증."""
    conn.execute(sa.text(_BACKFILL_SQL))


def downgrade() -> None:
    op.drop_index("ix_review_queue_created_at", table_name="review_queue")
    op.drop_column("review_queue", "created_at")
    op.drop_index("ix_backfill_job_started_at", table_name="backfill_job")
    op.drop_index("ix_backfill_job_status", table_name="backfill_job")
    op.drop_index("ix_backfill_job_track", table_name="backfill_job")
    op.drop_table("backfill_job")
