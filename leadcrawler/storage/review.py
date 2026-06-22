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


def enqueue_email_review(
    session: Session,
    company_id: str,
    candidates: Sequence[str],
    *,
    selected_default: str | None = None,
) -> str:
    """회사의 이메일 검토 항목을 멱등 등록/갱신한다(다중 후보 + 선택 보존).

    신규면 ``pending`` 으로 만들고, 기존이면 후보 목록만 갱신하고 **상태·담당자·선택은
    보존**한다(사람의 확정/거부·선택이 재크롤로 되돌아가지 않게). 단, 기존 선택이 새 후보
    목록에 더 이상 없으면 기본값(``selected_default`` 또는 선두 후보)으로 재설정한다.
    큐 행 id 를 반환한다.
    """
    rid = review_id_for(company_id, "email")
    cand_list = list(candidates)
    payload = json.dumps(cand_list, ensure_ascii=False)
    # 기본 선택: selected_default 가 후보에 있으면 그것, 아니면 선두 후보(best-first).
    fallback = (
        selected_default
        if selected_default is not None and selected_default in cand_list
        else (cand_list[0] if cand_list else None)
    )
    row = session.get(ReviewQueueRow, rid)
    if row is None:
        row = ReviewQueueRow(
            id=rid, company_id=company_id, field="email", candidates=payload,
            status=PENDING, selected=fallback, selected_by_human=False,
        )
        session.add(row)
    else:
        row.candidates = payload  # 후보만 갱신, status/assignee 보존
        if row.selected_by_human:
            # 사람이 명시 선택한 경우 보존하되, 후보에서 사라졌으면 자동 기본값으로 강등.
            if row.selected not in cand_list:
                row.selected = fallback
                row.selected_by_human = False
        else:
            # 자동 기본값은 매 재크롤마다 best 로 갱신(더 나은 후보 반영).
            row.selected = fallback
    return rid


def clear_email_review(session: Session, company_id: str) -> None:
    """회사가 이메일 후보를 전부 잃었을 때 큐 행의 후보를 비운다(상태·담당자 보존).

    재크롤로 이메일이 0건이 되면 죽은 후보가 큐에 남지 않게 정리한다. 행 자체는 남겨
    사람의 확정/거부 이력을 보존한다(제약 ② 일관).
    """
    row = session.get(ReviewQueueRow, review_id_for(company_id, "email"))
    if row is not None and row.candidates != "[]":
        row.candidates = "[]"
        row.selected = None
        row.selected_by_human = False


def count_reviews(session: Session, *, status: str | None = None) -> int:
    """큐 항목 수(선택적 상태 필터)."""
    stmt = select(func.count()).select_from(ReviewQueueRow)
    if status is not None:
        stmt = stmt.where(ReviewQueueRow.status == status)
    return int(session.scalar(stmt) or 0)


def _email_signals_by_value(
    session: Session, company_ids: Sequence[str]
) -> dict[tuple[str, str], _EmailSignal]:
    """(회사, 이메일주소)별 검증 신호를 맵으로 반환한다(후보별 신호 표시용).

    다중 후보 선택 UI 가 각 후보의 검증 상태를 보여줄 수 있도록 회사·주소 단위로
    평탄화한다(조인 행폭증은 큐 행과 분리된 별도 배치 조회로 회피).
    """
    if not company_ids:
        return {}
    rows = session.execute(
        select(
            ContactRow.company_id,
            ContactRow.value,
            EmailValidationRow.status,
            EmailValidationRow.mx,
            EmailValidationRow.smtp,
        )
        .join(EmailValidationRow, EmailValidationRow.contact_id == ContactRow.id)
        .where(ContactRow.company_id.in_(company_ids), ContactRow.type == "email")
    ).all()
    return {(cid, value): (status, mx, smtp) for cid, value, status, mx, smtp in rows}


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
    signals = _email_signals_by_value(session, [company.id for _, company in rows])
    return [_to_dict(rq, company, signals) for rq, company in rows]


def get_review(session: Session, review_id: str) -> dict | None:
    """단건 큐 항목 DTO(없으면 None). 목록과 동일한 후보별 신호를 쓴다."""
    rq = session.get(ReviewQueueRow, review_id)
    if rq is None:
        return None
    company = session.get(CompanyRow, rq.company_id)
    signals = _email_signals_by_value(session, [rq.company_id])
    return _to_dict(rq, company, signals)


def _parse_candidates(rq: ReviewQueueRow) -> list[str]:
    """candidates JSON 을 안전 파싱한다(깨졌으면 빈 목록 + 경고)."""
    try:
        value = json.loads(rq.candidates)
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        log.warning("review.candidates_corrupt", review_id=rq.id, raw=rq.candidates)
        return []


def effective_selected(selected: str | None, candidate_values: list[str]) -> str | None:
    """'유효 선택' 단일 진실원천 — selected 가 후보에 있으면 그것, 아니면 선두 후보.

    DTO 표시(:func:`_to_dict`)와 엑셀 export(load_leads)가 **동일 규칙**으로 같은 후보를
    고르도록 한 곳에 정의한다(워크벤치 표시 ≠ export 불일치 방지). 둘 다 review_queue
    candidates JSON 순서(best-first)를 진실원천으로 삼는다.
    """
    if selected is not None and selected in candidate_values:
        return selected
    return candidate_values[0] if candidate_values else None


def candidate_values_of(rq: ReviewQueueRow) -> list[str]:
    """큐 행의 후보 주소 목록(load_leads 등 외부에서 effective_selected 와 함께 사용)."""
    return _parse_candidates(rq)


def set_review_status(
    session: Session,
    review_id: str,
    status: str,
    *,
    assignee: str | None = None,
    selected: str | None = None,
) -> dict | None:
    """큐 항목 상태(확정/거부/보류)와 선택 후보를 갱신한다.

    없으면 None, 잘못된 상태면 ValueError. ``selected`` 가 주어지면 후보 목록에 있어야
    하며(아니면 ValueError), 확정/거부 시 사람이 고른 최종 이메일을 기록한다.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"허용되지 않은 상태: {status}")
    rq = session.get(ReviewQueueRow, review_id)
    if rq is None:
        return None
    if selected is not None:
        if selected not in _parse_candidates(rq):
            raise ValueError(f"후보에 없는 선택: {selected}")
        rq.selected = selected
        rq.selected_by_human = True  # 사람 명시 선택 — 이후 재크롤에서 보존.
    rq.status = status
    if assignee is not None:
        rq.assignee = assignee
    session.flush()
    return get_review(session, review_id)


def _to_dict(
    rq: ReviewQueueRow,
    company: CompanyRow | None,
    signals: dict[tuple[str, str], _EmailSignal],
) -> dict:
    """ORM 행 + 후보별 이메일 신호를 API DTO dict 로 평탄화한다."""
    candidates = _parse_candidates(rq)
    # 선택: load_leads(export) 와 동일 규칙(effective_selected)으로 단일화.
    selected = effective_selected(rq.selected, candidates)
    cand_dtos = []
    for value in candidates:
        st, mx, smtp = signals.get((rq.company_id, value), (None, None, None))
        cand_dtos.append({"value": value, "email_status": st, "email_mx": mx, "email_smtp": smtp})
    # 대표 신호 = 선택된 후보의 신호(이메일 컬럼 표시·export 정합).
    rep_status, rep_mx, rep_smtp = (
        signals.get((rq.company_id, selected), (None, None, None))
        if selected is not None
        else (None, None, None)
    )
    return {
        "id": rq.id,
        "company_id": rq.company_id,
        "field": rq.field,
        "candidates": cand_dtos,
        "selected": selected,
        "status": rq.status,
        "assignee": rq.assignee,
        "name": company.name if company else "",
        "country": company.country if company else "",
        "industry": company.industry if company else "",
        "homepage": company.homepage if company else None,
        "site_alive": company.site_alive if company else False,
        "email_status": rep_status,
        "email_mx": rep_mx,
        "email_smtp": rep_smtp,
    }
