"""검증 큐(review_queue) 영속화 계층 — 웹앱 워크벤치의 데이터 접근.

enqueue 규칙(PO 확정): **이메일 후보가 있는 리드는 전부** 큐에 넣어 사람이 발송 전
확인한다. 큐 행 PK 는 (company_id, field) 에서 결정적으로 파생해, 24/7 재크롤이 같은
회사를 다시 적재해도 **사람이 내린 확정/거부 상태가 초기화되지 않는다**(후보만 갱신).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..logging import get_logger
from ..schema import CompanyRow, ContactRow, EmailValidationRow, ReviewQueueRow

log = get_logger("review")

# 이메일 검증 신호 튜플: (status, mx, smtp). 신호 없으면 None.
_EmailSignal = tuple[str | None, bool | None, bool | None]

# 큐 상태 값.
PENDING = "pending"
CONFIRMED = "confirmed"
REJECTED = "rejected"
_VALID_STATUSES = frozenset({PENDING, CONFIRMED, REJECTED})


def review_id_for(company_id: str, field: str) -> str:
    """(회사·필드)로부터 안정적인 큐 행 PK 를 만든다(재크롤 간 불변)."""
    raw = f"{company_id}|{field}".encode()
    return "r_" + hashlib.sha1(raw).hexdigest()[:30]


def enqueue_email_review(session: Session, company_id: str, candidates: Sequence[str]) -> str:
    """회사의 이메일 검토 항목을 멱등 등록/갱신한다.

    신규면 ``pending`` 으로 만들고, 기존이면 후보 목록만 갱신하고 **상태·담당자는 보존**
    한다(사람의 확정/거부가 재크롤로 되돌아가지 않게). 큐 행 id 를 반환한다.
    """
    rid = review_id_for(company_id, "email")
    payload = json.dumps(list(candidates), ensure_ascii=False)
    row = session.get(ReviewQueueRow, rid)
    if row is None:
        row = ReviewQueueRow(
            id=rid, company_id=company_id, field="email", candidates=payload, status=PENDING
        )
        session.add(row)
    else:
        row.candidates = payload  # 후보만 갱신, status/assignee 보존
    return rid


def count_reviews(session: Session, *, status: str | None = None) -> int:
    """큐 항목 수(선택적 상태 필터)."""
    stmt = select(func.count()).select_from(ReviewQueueRow)
    if status is not None:
        stmt = stmt.where(ReviewQueueRow.status == status)
    return int(session.scalar(stmt) or 0)


def _email_signal_map(session: Session, company_ids: Sequence[str]) -> dict[str, _EmailSignal]:
    """회사별 **대표 이메일 1건**의 검증 신호를 맵으로 반환한다(조인 행폭증 방지).

    한 회사가 이메일 연락처를 여럿 가질 수 있으므로(IR + general 등), 큐 행과 1:1 을
    유지하려면 검증 신호를 별도 배치 조회로 평탄화한다. 대표는 ``contact.id`` 오름차순
    첫 행으로 **결정적** 선택한다(목록·단건 뷰가 항상 같은 신호를 보이도록).
    """
    if not company_ids:
        return {}
    rows = session.execute(
        select(
            ContactRow.company_id,
            EmailValidationRow.status,
            EmailValidationRow.mx,
            EmailValidationRow.smtp,
        )
        .join(EmailValidationRow, EmailValidationRow.contact_id == ContactRow.id)
        .where(ContactRow.company_id.in_(company_ids), ContactRow.type == "email")
        .order_by(ContactRow.company_id, ContactRow.id)
    ).all()
    out: dict[str, _EmailSignal] = {}
    for cid, status, mx, smtp in rows:
        out.setdefault(cid, (status, mx, smtp))  # 첫 행(id 최소)만 = 결정적 대표
    return out


def query_reviews(
    session: Session, *, status: str | None = None, limit: int = 50, offset: int = 0
) -> list[dict]:
    """큐 항목을 회사 정보와 함께 DTO dict 목록으로 반환한다(큐 행과 1:1).

    이메일 검증 신호(status/mx/smtp)는 :func:`_email_signal_map` 로 별도 평탄화해, 회사가
    이메일을 여럿 가져도 큐 행이 복제되지 않게 한다. 정렬에 ``id`` 최종 타이브레이커를
    더해 offset 페이지네이션이 안정적이다.
    """
    stmt = (
        select(ReviewQueueRow, CompanyRow)
        .join(CompanyRow, ReviewQueueRow.company_id == CompanyRow.id)
        .order_by(ReviewQueueRow.status, CompanyRow.name, ReviewQueueRow.id)
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        stmt = stmt.where(ReviewQueueRow.status == status)
    rows = session.execute(stmt).all()
    signals = _email_signal_map(session, [company.id for _, company in rows])
    return [_to_dict(rq, company, signals.get(company.id)) for rq, company in rows]


def get_review(session: Session, review_id: str) -> dict | None:
    """단건 큐 항목 DTO(없으면 None). 목록과 동일한 대표 이메일 신호를 쓴다."""
    rq = session.get(ReviewQueueRow, review_id)
    if rq is None:
        return None
    company = session.get(CompanyRow, rq.company_id)
    signal = _email_signal_map(session, [rq.company_id]).get(rq.company_id)
    return _to_dict(rq, company, signal)


def set_review_status(
    session: Session, review_id: str, status: str, *, assignee: str | None = None
) -> dict | None:
    """큐 항목 상태를 갱신한다(확정/거부/보류). 없으면 None, 잘못된 상태면 ValueError."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"허용되지 않은 상태: {status}")
    rq = session.get(ReviewQueueRow, review_id)
    if rq is None:
        return None
    rq.status = status
    if assignee is not None:
        rq.assignee = assignee
    session.flush()
    return get_review(session, review_id)


def _to_dict(
    rq: ReviewQueueRow, company: CompanyRow | None, signal: _EmailSignal | None
) -> dict:
    """ORM 행 + 이메일 신호를 API DTO dict 로 평탄화한다."""
    try:
        candidates = json.loads(rq.candidates)
    except (ValueError, TypeError):
        log.warning("review.candidates_corrupt", review_id=rq.id, raw=rq.candidates)
        candidates = []
    status, mx, smtp = signal if signal is not None else (None, None, None)
    return {
        "id": rq.id,
        "company_id": rq.company_id,
        "field": rq.field,
        "candidates": candidates,
        "status": rq.status,
        "assignee": rq.assignee,
        "name": company.name if company else "",
        "country": company.country if company else "",
        "industry": company.industry if company else "",
        "homepage": company.homepage if company else None,
        "site_alive": company.site_alive if company else False,
        "email_status": status,
        "email_mx": mx,
        "email_smtp": smtp,
    }
