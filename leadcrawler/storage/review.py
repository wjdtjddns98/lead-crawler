"""검증 큐(review_queue) 영속화 계층 — 웹앱 워크벤치의 데이터 접근.

enqueue 규칙(PO 갱신 2026-06-24): **실존 리드는 이메일이 없어도 전부** 큐에 넣는다 —
사람이 워크벤치에서 직접 이메일을 찾아 입력/확정하거나, 문의폼만 있는 회사를 폼으로
처리한다(빈 후보 행도 등록). 큐 행 PK 는 (company_id, field) 에서 결정적으로 파생해,
24/7 재크롤이 같은 회사를 다시 적재해도 **사람이 내린 확정/거부 상태가 초기화되지
않는다**(후보만 갱신).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..logging import get_logger
from ..models import ContactType
from ..schema import (
    CompanyRow,
    ContactRow,
    DiscoveredCompanyRow,
    EmailValidationRow,
    ReviewAuditRow,
    ReviewQueueRow,
)
from ..sources.countries import country_match_set

log = get_logger("review")

# 큐 당겨가기/조회용 작업범위 필터 화이트리스트(상장 여부). 빈 문자열/None = 전체.
_VALID_LISTED = frozenset({"listed", "unlisted", "unknown"})


def _apply_queue_filters(
    stmt: object,
    *,
    countries: Sequence[str] | None,
    industries: Sequence[str] | None,
    listed: str | None,
) -> object:
    """CompanyRow 가 이미 조인된 select 에 국가/업종/상장 작업범위 필터를 적용한다.

    국가는 별칭·대소문자 무시 매칭(:func:`country_match_set`, 'KR'↔'대한민국'), 업종은
    대소문자 무시 매칭(엑셀 export·아웃리치 발송 선례와 동일 규약). 상장 여부는
    ``CompanyRow.canonical_key → DiscoveredCompanyRow`` 조인 후 ``listed`` 일치로 거른다
    (상장 컬럼은 CompanyRow 가 아니라 DiscoveredCompanyRow 에만 있음). 빈 값은 무시(전체).
    """
    if countries:
        cset = country_match_set(countries)
        if cset:  # 토큰이 전부 공백이면 빈 집합 — 그땐 필터 미적용(전체).
            stmt = stmt.where(func.lower(CompanyRow.country).in_(cset))
    if industries:
        wanted = {i.strip().lower() for i in industries if i and i.strip()}
        if wanted:
            stmt = stmt.where(func.lower(CompanyRow.industry).in_(wanted))
    if listed:
        # 스토리지 계층 방어선 — API 는 Literal 로 막지만(422), 비API 직접 호출의 오타·대문자
        # ('LISTED')가 조용히 0건이 되지 않게 fail-loud(set_review_status 의 상태검증과 동일 결).
        if listed not in _VALID_LISTED:
            raise ValueError(f"허용되지 않은 listed 값: {listed}")
        stmt = stmt.join(
            DiscoveredCompanyRow,
            CompanyRow.canonical_key == DiscoveredCompanyRow.canonical_key,
        ).where(DiscoveredCompanyRow.listed == listed)
    return stmt


def _has_queue_filters(
    countries: Sequence[str] | None,
    industries: Sequence[str] | None,
    listed: str | None,
) -> bool:
    """국가/업종/상장 중 실제로 거를 값이 하나라도 있으면 True(빈 값=전체)."""
    return bool(countries) or bool(industries) or bool(listed)


class ReviewConflict(Exception):
    """다른 직원이 활성 점유 중인 항목을 처리하려 할 때(동시성 충돌, API→409)."""

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


def count_reviews(
    session: Session,
    *,
    status: str | None = None,
    countries: Sequence[str] | None = None,
    industries: Sequence[str] | None = None,
    listed: str | None = None,
) -> int:
    """큐 항목 수(선택적 상태 + 국가/업종/상장 작업범위 필터).

    필터가 있으면 CompanyRow(상장은 DiscoveredCompanyRow)를 조인해 카운트한다 —
    조인은 모두 1:1(canonical_key/id 유일)이라 행 증식이 없어 count 가 정확하다.
    """
    if not _has_queue_filters(countries, industries, listed):
        stmt = select(func.count()).select_from(ReviewQueueRow)
        if status is not None:
            stmt = stmt.where(ReviewQueueRow.status == status)
        return int(session.scalar(stmt) or 0)
    stmt = (
        select(func.count())
        .select_from(ReviewQueueRow)
        .join(CompanyRow, ReviewQueueRow.company_id == CompanyRow.id)
    )
    if status is not None:
        stmt = stmt.where(ReviewQueueRow.status == status)
    stmt = _apply_queue_filters(
        stmt, countries=countries, industries=industries, listed=listed
    )
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


# 폼 신뢰도 리뷰 임계 — 이 값 미만이면 '저신뢰 폼(사람 확인 필요)'으로 표기한다. enricher
# 의 최후 폴백 문의페이지 폼만 0.3 이라 < 0.4 가 그 폴백만 정확히 잡는다(실폼은 0.45~0.7).
FORM_REVIEW_THRESHOLD = 0.4


def _forms_by_company(
    session: Session, company_ids: Sequence[str]
) -> dict[str, tuple[str, float]]:
    """회사별 (문의폼 URL, 신뢰도) 맵(회사당 1건). 저신뢰 폴백 폼을 리뷰레인서 가르는 데 쓴다."""
    if not company_ids:
        return {}
    rows = session.execute(
        select(ContactRow.company_id, ContactRow.value, ContactRow.confidence)
        .where(ContactRow.company_id.in_(list(company_ids)))
        .where(ContactRow.type == ContactType.FORM.value)
    ).all()
    forms: dict[str, tuple[str, float]] = {}
    for cid, value, confidence in rows:
        # 첫 폼만(여러 개여도 대표 1개). confidence 는 NOT NULL 이나 방어적으로 None→0.0.
        forms.setdefault(cid, (value, confidence if confidence is not None else 0.0))
    return forms


def query_reviews(
    session: Session,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    countries: Sequence[str] | None = None,
    industries: Sequence[str] | None = None,
    listed: str | None = None,
) -> list[dict]:
    """큐 항목을 회사 정보와 함께 DTO dict 목록으로 반환한다(큐 행과 1:1).

    이메일 검증 신호(status/mx/smtp)는 :func:`_email_signal_map` 로 별도 평탄화해, 회사가
    이메일을 여럿 가져도 큐 행이 복제되지 않게 한다. 정렬에 ``id`` 최종 타이브레이커를
    더해 offset 페이지네이션이 안정적이다. 국가/업종/상장 작업범위 필터는 선택적으로
    적용한다(:func:`_apply_queue_filters`).
    """
    stmt = select(ReviewQueueRow, CompanyRow).join(
        CompanyRow, ReviewQueueRow.company_id == CompanyRow.id
    )
    if status is not None:
        stmt = stmt.where(ReviewQueueRow.status == status)
    stmt = _apply_queue_filters(
        stmt, countries=countries, industries=industries, listed=listed
    )
    stmt = (
        stmt.order_by(ReviewQueueRow.status, CompanyRow.name, ReviewQueueRow.id)
        .limit(limit)
        .offset(offset)
    )
    rows = session.execute(stmt).all()
    ids = [company.id for _, company in rows]
    signals = _email_signals_by_value(session, ids)
    forms = _forms_by_company(session, ids)
    return [_to_dict(rq, company, signals, forms) for rq, company in rows]


def get_review(session: Session, review_id: str) -> dict | None:
    """단건 큐 항목 DTO(없으면 None). 목록과 동일한 후보별 신호를 쓴다."""
    rq = session.get(ReviewQueueRow, review_id)
    if rq is None:
        return None
    company = session.get(CompanyRow, rq.company_id)
    signals = _email_signals_by_value(session, [rq.company_id])
    forms = _forms_by_company(session, [rq.company_id])
    return _to_dict(rq, company, signals, forms)


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
    assignee_id: str | None = None,
    selected: str | None = None,
    now: datetime | None = None,
    claim_ttl_minutes: int | None = None,
) -> dict | None:
    """큐 항목 상태(확정/거부/보류)와 선택 후보를 갱신하고 감사 이력을 적재한다.

    없으면 None, 잘못된 상태면 ValueError. ``selected`` 가 주어지면 후보 목록에 있어야
    하며(아니면 ValueError), 확정/거부 시 사람이 고른 최종 이메일을 기록한다. 처리자
    (assignee/assignee_id)와 시각(reviewed_at)을 큐 행에 남기고, 변경 1건마다
    :class:`ReviewAuditRow` 를 append 해 책임추적 이력을 보존한다. ``claim_ttl_minutes``
    가 주어지면 동시성 백스톱 — 타인이 활성 점유 중인 항목이면 :class:`ReviewConflict`.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"허용되지 않은 상태: {status}")
    rq = session.get(ReviewQueueRow, review_id)
    if rq is None:
        return None
    when = now or datetime.now(timezone.utc)
    # 클레임 백스톱(동시성) — 활성 점유자가 타인이면 충돌(전체보기 등 우연한 동시처리 차단).
    if (
        claim_ttl_minutes is not None
        and rq.claimed_by is not None
        and rq.claimed_by != assignee_id
        and rq.claimed_at is not None
        and _aware(rq.claimed_at) >= _expiry(when, claim_ttl_minutes)
    ):
        raise ReviewConflict("다른 직원이 처리 중인 항목입니다. 새로고침 후 다시 시도하세요.")
    if selected is not None:
        if selected not in _parse_candidates(rq):
            raise ValueError(f"후보에 없는 선택: {selected}")
        rq.selected = selected
        rq.selected_by_human = True  # 사람 명시 선택 — 이후 재크롤에서 보존.
    rq.status = status
    if status in (CONFIRMED, REJECTED):
        # 종료 상태로 가면 점유는 무의미 — 정리(귀속은 assignee/reviewed_at 가 보존).
        rq.claimed_by = None
        rq.claimed_at = None
    # 처리자(사람) 정보는 username·id·시각·감사행을 한 묶음으로 기록한다 — 부분 갱신으로
    # username↔id 가 서로 다른 처리자를 가리키는 불일치를 막는다. 처리자 없이(내부 호출)
    # 부르면 상태만 바꾸고 귀속/감사행은 남기지 않는다(빈 actor 감사행 방지).
    if assignee is not None or assignee_id is not None:
        rq.assignee = assignee
        rq.assignee_id = assignee_id
        rq.reviewed_at = when
        # 감사 이력 — 처리자 계정이 삭제돼도 username 스냅샷으로 '누가' 가 남는다.
        session.add(
            ReviewAuditRow(
                id=uuid4().hex[:12],
                review_id=review_id,
                actor_id=assignee_id,
                actor_username=assignee or "",
                action=status,
                selected=rq.selected,
                at=when,
            )
        )
    session.flush()
    return get_review(session, review_id)


def _aware(dt: datetime) -> datetime:
    """naive datetime(SQLite)을 UTC aware 로 보정(비교 안전)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _expiry(now: datetime, ttl_minutes: int) -> datetime:
    """점유 만료 기준 시각 — 이보다 이전에 점유된 미처리 항목은 풀로 복귀 가능."""
    return now - timedelta(minutes=max(1, ttl_minutes))


def _claim_more(
    session: Session,
    user_id: str,
    *,
    want: int,
    ttl_minutes: int,
    now: datetime,
    countries: Sequence[str] | None = None,
    industries: Sequence[str] | None = None,
    listed: str | None = None,
) -> int:
    """미점유(또는 TTL 만료) pending 항목을 최대 ``want`` 개 이 직원에게 배타 배정한다.

    PostgreSQL 은 ``FOR UPDATE SKIP LOCKED`` 로 6명 동시 요청에도 서로 다른 행을 받게
    한다(잠긴 행 건너뜀 — 동시 작업 큐의 표준). SQLite(테스트)는 미지원이라 평이한
    select(단일 라이터라 무해). 국가/업종/상장 작업범위 필터가 주어지면 CompanyRow
    (상장은 DiscoveredCompanyRow)를 조인해 그 조건의 행만 배정한다. 배정한 행 수를 반환한다.
    """
    if want <= 0:
        return 0
    expiry = _expiry(now, ttl_minutes)
    stmt = (
        select(ReviewQueueRow.id)
        .where(ReviewQueueRow.status == PENDING)
        .where(or_(ReviewQueueRow.claimed_by.is_(None), ReviewQueueRow.claimed_at < expiry))
    )
    if _has_queue_filters(countries, industries, listed):
        stmt = stmt.join(CompanyRow, ReviewQueueRow.company_id == CompanyRow.id)
        stmt = _apply_queue_filters(
            stmt, countries=countries, industries=industries, listed=listed
        )
    stmt = stmt.order_by(ReviewQueueRow.id).limit(want)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        # 필터로 CompanyRow/DiscoveredCompanyRow 조인이 들어가도 잠금은 review_queue 행에만
        # 건다 — of=ReviewQueueRow 로 한정해 조인 테이블(회사 행)까지 잠그거나 건너뛰지
        # 않게 한다(서로 다른 필터의 동시 직원이 같은 회사를 참조해도 회귀 없음).
        stmt = stmt.with_for_update(of=ReviewQueueRow, skip_locked=True)
    # 배타성은 이 행잠금이 보장한다(시계 일치가 아님) — select 와 아래 update 가 같은
    # 요청 트랜잭션(get_db) 안이라 잠금이 update 까지 유지된다. 리팩터로 트랜잭션을
    # 쪼개면 안 된다.
    ids = list(session.scalars(stmt).all())
    if not ids:
        return 0
    session.execute(
        update(ReviewQueueRow)
        .where(ReviewQueueRow.id.in_(ids))
        .values(claimed_by=user_id, claimed_at=now)
    )
    return len(ids)


def _my_active_rows(
    session: Session,
    user_id: str,
    *,
    ttl_minutes: int,
    now: datetime,
    countries: Sequence[str] | None = None,
    industries: Sequence[str] | None = None,
    listed: str | None = None,
) -> list[tuple[ReviewQueueRow, CompanyRow]]:
    """이 직원이 점유 중인(아직 pending·미만료) 항목 (rq, company) 목록(선택적 필터)."""
    expiry = _expiry(now, ttl_minutes)
    stmt = (
        select(ReviewQueueRow, CompanyRow)
        .join(CompanyRow, ReviewQueueRow.company_id == CompanyRow.id)
        .where(ReviewQueueRow.claimed_by == user_id)
        .where(ReviewQueueRow.status == PENDING)
        .where(ReviewQueueRow.claimed_at >= expiry)
    )
    stmt = _apply_queue_filters(
        stmt, countries=countries, industries=industries, listed=listed
    )
    stmt = stmt.order_by(CompanyRow.name, ReviewQueueRow.id)
    rows = session.execute(stmt).all()
    return [(rq, company) for rq, company in rows]


def release_non_matching(
    session: Session,
    user_id: str,
    *,
    countries: Sequence[str] | None = None,
    industries: Sequence[str] | None = None,
    listed: str | None = None,
) -> int:
    """내가 점유한 pending 항목 중 현재 작업범위 필터에 **안 맞는** 것을 풀로 반납한다.

    직원이 작업범위(국가/업종/상장)를 바꾸면 이전 범위로 점유 중이던 비매칭 항목이
    화면에 남지 않도록 즉시 반납한다(다른 직원이 가져갈 수 있게). 필터가 전부 비면
    (전체) 반납하지 않는다. 반납한 행 수를 반환한다.
    """
    if not _has_queue_filters(countries, industries, listed):
        return 0
    matching_ids = set(
        session.scalars(
            _apply_queue_filters(
                select(ReviewQueueRow.id).join(
                    CompanyRow, ReviewQueueRow.company_id == CompanyRow.id
                )
                .where(ReviewQueueRow.claimed_by == user_id)
                .where(ReviewQueueRow.status == PENDING),
                countries=countries,
                industries=industries,
                listed=listed,
            )
        ).all()
    )
    release_stmt = update(ReviewQueueRow).where(
        ReviewQueueRow.claimed_by == user_id, ReviewQueueRow.status == PENDING
    )
    if matching_ids:  # 매칭 행은 보존, 나머지만 반납(빈 집합이면 전부 반납).
        release_stmt = release_stmt.where(ReviewQueueRow.id.notin_(matching_ids))
    result = session.execute(release_stmt.values(claimed_by=None, claimed_at=None))
    return int(result.rowcount or 0)


def claim_work(
    session: Session,
    user_id: str,
    *,
    target: int,
    ttl_minutes: int,
    now: datetime | None = None,
    countries: Sequence[str] | None = None,
    industries: Sequence[str] | None = None,
    listed: str | None = None,
) -> list[dict]:
    """내 활성 점유를 ``target`` 개까지 채우고(부족분 배타 배정) 내 작업분 DTO 를 반환한다.

    매 호출이 target 으로 top-up 하므로 확정/거부로 줄어든 만큼 자동 리필된다(반복 호출
    멱등 — 항상 ~target 개 유지). 6명이 동시에 불러도 SKIP LOCKED 로 작업분이 겹치지 않는다.
    국가/업종/상장 작업범위 필터가 주어지면 그 조건의 행만 점유하고, 필터를 바꾸면 이전
    범위의 비매칭 점유를 먼저 반납한다(화면엔 항상 현재 범위만 남는다).
    """
    now = now or datetime.now(timezone.utc)
    # 필터 전환 시 비매칭 점유 반납(전체 필터면 no-op).
    release_non_matching(
        session, user_id, countries=countries, industries=industries, listed=listed
    )
    session.flush()
    current = _my_active_rows(
        session, user_id, ttl_minutes=ttl_minutes, now=now,
        countries=countries, industries=industries, listed=listed,
    )
    if len(current) < target:
        _claim_more(
            session, user_id, want=target - len(current), ttl_minutes=ttl_minutes, now=now,
            countries=countries, industries=industries, listed=listed,
        )
        session.flush()
        current = _my_active_rows(
            session, user_id, ttl_minutes=ttl_minutes, now=now,
            countries=countries, industries=industries, listed=listed,
        )
    ids = [c.id for _, c in current]
    signals = _email_signals_by_value(session, ids)
    forms = _forms_by_company(session, ids)
    return [_to_dict(rq, company, signals, forms) for rq, company in current]


def release_my_claims(session: Session, user_id: str) -> int:
    """내가 점유한 미처리(pending) 항목을 모두 풀로 반납한다(작업 종료). 반납 수 반환."""
    result = session.execute(
        update(ReviewQueueRow)
        .where(ReviewQueueRow.claimed_by == user_id, ReviewQueueRow.status == PENDING)
        .values(claimed_by=None, claimed_at=None)
    )
    return int(result.rowcount or 0)


def _to_dict(
    rq: ReviewQueueRow,
    company: CompanyRow | None,
    signals: dict[tuple[str, str], _EmailSignal],
    forms: dict[str, tuple[str, float]] | None = None,
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
    form_entry = (forms or {}).get(rq.company_id)
    form_url, form_conf = form_entry if form_entry is not None else (None, None)
    return {
        "id": rq.id,
        "company_id": rq.company_id,
        "field": rq.field,
        "candidates": cand_dtos,
        "selected": selected,
        "status": rq.status,
        "assignee": rq.assignee,
        "reviewed_at": rq.reviewed_at.isoformat() if rq.reviewed_at else None,
        "name": company.name if company else "",
        "country": company.country if company else "",
        "industry": company.industry if company else "",
        "homepage": company.homepage if company else None,
        "site_alive": company.site_alive if company else False,
        # 문의폼 URL + 신뢰도(없으면 None) — 저신뢰(폴백 0.3)면 리뷰레인서 '사람 확인' 표기.
        "form": form_url,
        "form_confidence": form_conf,
        "form_low_confidence": form_url is not None and form_conf is not None
        and form_conf < FORM_REVIEW_THRESHOLD,
        "email_status": rep_status,
        "email_mx": rep_mx,
        "email_smtp": rep_smtp,
    }
