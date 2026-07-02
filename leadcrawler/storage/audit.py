"""관리자 조회 — 계정별 처리 통계와 검증 감사 로그(읽기 전용).

쓰기(감사 행 적재)는 :func:`leadcrawler.storage.review.set_review_status` 가 처리하고,
여기선 관리자 페이지가 쓰는 집계/목록 질의만 모은다.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..schema import CompanyRow, ReviewAuditRow, ReviewQueueRow, UserRow
from .review import CONFIRMED, PENDING, REJECTED


def user_stats(session: Session) -> list[dict]:
    """계정별 처리 통계 목록 — 확정/거부 건수 + 마지막 처리 시각 + 현재 점유 건수.

    감사 로그(actor_id)를 액션별로 집계해 각 계정 행에 병합한다(계정 삭제로 actor_id 가
    NULL 이 된 과거 이력은 어느 계정에도 귀속되지 않으므로 통계에서 제외 — 의도된 동작).
    ``claimed`` 는 영구 배정 모델의 회수 판단용 — 이 계정이 점유 중인 pending 건수.
    """
    claimed = dict(
        session.execute(
            select(ReviewQueueRow.claimed_by, func.count())
            .where(ReviewQueueRow.claimed_by.is_not(None))
            .where(ReviewQueueRow.status == PENDING)
            .group_by(ReviewQueueRow.claimed_by)
        ).all()
    )
    # 계정×액션 집계 + 마지막 처리 시각.
    agg = session.execute(
        select(
            ReviewAuditRow.actor_id,
            ReviewAuditRow.action,
            func.count().label("n"),
            func.max(ReviewAuditRow.at).label("last_at"),
        )
        .where(ReviewAuditRow.actor_id.is_not(None))
        .group_by(ReviewAuditRow.actor_id, ReviewAuditRow.action)
    ).all()
    confirmed: dict[str, int] = {}
    rejected: dict[str, int] = {}
    last_at: dict[str, object] = {}
    for actor_id, action, n, at in agg:
        if action == CONFIRMED:
            confirmed[actor_id] = n
        elif action == REJECTED:
            rejected[actor_id] = n
        if actor_id not in last_at or (at is not None and at > last_at[actor_id]):
            last_at[actor_id] = at

    users = session.scalars(select(UserRow).order_by(UserRow.created_at, UserRow.username)).all()
    out: list[dict] = []
    for u in users:
        la = last_at.get(u.id)
        out.append(
            {
                "id": u.id,
                "username": u.username,
                "role": u.role,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "confirmed": confirmed.get(u.id, 0),
                "rejected": rejected.get(u.id, 0),
                "claimed": claimed.get(u.id, 0),
                "last_action_at": la.isoformat() if la is not None else None,
            }
        )
    return out


def recent_audit(session: Session, *, limit: int = 100, offset: int = 0) -> list[dict]:
    """최근 검증 처리 이력(누가·언제·무엇) — 회사명 포함, 최신순."""
    rows = session.execute(
        select(ReviewAuditRow, CompanyRow.name)
        .join(ReviewQueueRow, ReviewAuditRow.review_id == ReviewQueueRow.id)
        .join(CompanyRow, ReviewQueueRow.company_id == CompanyRow.id, isouter=True)
        .order_by(ReviewAuditRow.at.desc(), ReviewAuditRow.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        {
            "id": a.id,
            "review_id": a.review_id,
            "actor_username": a.actor_username,
            "action": a.action,
            "selected": a.selected,
            "company_name": name or "",
            "at": a.at.isoformat() if a.at else None,
        }
        for a, name in rows
    ]
