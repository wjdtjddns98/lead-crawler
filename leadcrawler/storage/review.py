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
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from ..logging import get_logger
from ..models import ContactType, ExtractMethod
from ..schema import (
    CompanyRow,
    ContactRow,
    DiscoveredCompanyRow,
    EmailValidationRow,
    ReviewAuditRow,
    ReviewQueueRow,
)
from ..sources.countries import country_match_set, resolve_country
from ..sources.taxonomy import UNCLASSIFIED

log = get_logger("review")

# 큐 당겨가기/조회용 작업범위 필터 화이트리스트(상장 여부). 빈 문자열/None = 전체.
_VALID_LISTED = frozenset({"listed", "unlisted", "unknown"})

# 큐 목록 서버 정렬(#238) 허용 컬럼 — FE 테이블의 정렬 가능 컬럼과 1:1(계약 어휘).
QUEUE_SORT_KEYS = ("name", "country", "industry", "listed", "form", "status")


def _sort_expression(sort_by: str, *, pg: bool = False):  # noqa: ANN202 — SQLAlchemy 표현식
    """정렬 키 → SQLAlchemy 정렬 표현식(#238). 허용 밖 키는 ValueError(fail-loud).

    status/listed 는 알파벳순이 아니라 **업무 순위**(pending 먼저 / 상장·비상장·미상)로,
    form 은 '폼 있음 먼저'(asc 기준)로 FE 클라이언트 정렬과 동일 의미를 낸다. listed 는
    스칼라 서브쿼리로 읽어 필터의 조건부 DiscoveredCompanyRow 조인과 간섭하지 않는다.
    문자열 컬럼은 PostgreSQL(``pg=True``)에서 ``COLLATE "C"``(코드포인트순)를 강제한다 —
    운영 DB 의 en_US.utf8 로케일 콜레이션은 한글을 가나다순이 아닌 순서로 돌려줘(실서버
    실측: '하나…'가 '동우…'보다 앞) 종전 FE 클라이언트 정렬(JS 코드포인트=가나다순)과
    어긋난다. SQLite 는 기본 BINARY 가 이미 코드포인트순이라 그대로 둔다("C" 미지원).
    """
    if sort_by == "name":
        return CompanyRow.name.collate("C") if pg else CompanyRow.name
    if sort_by == "country":
        return CompanyRow.country.collate("C") if pg else CompanyRow.country
    if sort_by == "industry":
        return CompanyRow.industry.collate("C") if pg else CompanyRow.industry
    if sort_by == "status":
        return case(
            (ReviewQueueRow.status == PENDING, 0),
            (ReviewQueueRow.status == CONFIRMED, 1),
            else_=2,
        )
    if sort_by == "listed":
        # 별칭+명시 correlate — listed/region/market 필터가 같은 테이블을 외부 조인하면
        # raw 참조는 자동 상관으로 FROM 을 잃고 500 이 난다(#258 리뷰 재현 — 기본 정렬의
        # first_seen 서브쿼리와 동일 패턴·동일 처방).
        dc = aliased(DiscoveredCompanyRow)
        listed_sq = (
            select(dc.listed)
            .where(dc.canonical_key == CompanyRow.canonical_key)
            .correlate(CompanyRow)
            .scalar_subquery()
        )
        return case((listed_sq == "listed", 0), (listed_sq == "unlisted", 1), else_=2)
    if sort_by == "form":
        form_exists = (
            select(ContactRow.id)
            .where(ContactRow.company_id == ReviewQueueRow.company_id)
            .where(ContactRow.type == ContactType.FORM.value)
            .exists()
        )
        return case((form_exists, 0), else_=1)
    raise ValueError(f"허용되지 않은 정렬 키: {sort_by}")


def _apply_queue_filters(
    stmt: object,
    *,
    countries: Sequence[str] | None,
    industries: Sequence[str] | None,
    listed: str | None,
    regions: Sequence[str] | None = None,
    markets: Sequence[str] | None = None,
) -> object:
    """CompanyRow 가 이미 조인된 select 에 국가/업종/상장/지역/시장 작업범위 필터를 적용한다.

    국가는 별칭·대소문자 무시 매칭(:func:`country_match_set`, 'KR'↔'대한민국'), 업종은
    대소문자 무시 매칭(엑셀 export·아웃리치 발송 선례와 동일 규약). 상장 여부·지역·시장은
    ``CompanyRow.canonical_key → DiscoveredCompanyRow`` 조인 후 일치로 거른다
    (세 컬럼 다 DiscoveredCompanyRow 에만 있음 — 조인은 한 번만). 빈 값은 무시(전체).
    지역·시장 필터는 미상(NULL) 행을 자연히 제외한다. 시장(KOSPI/KOSDAQ…)은 저장값이
    전부 대문자라 upper() 정규화 매칭한다.
    """
    if countries:
        cset = country_match_set(countries)
        if cset:  # 토큰이 전부 공백이면 빈 집합 — 그땐 필터 미적용(전체).
            stmt = stmt.where(func.lower(CompanyRow.country).in_(cset))
    if industries:
        wanted = {i.strip().lower() for i in industries if i and i.strip()}
        if wanted:
            cond = func.lower(CompanyRow.industry).in_(wanted)
            if UNCLASSIFIED.lower() in wanted:
                # '미분류' 는 저장값이 리터럴이 아니라 **빈 문자열**인 행들이다 —
                # /queue/stock 집계가 빈값을 '미분류' 로 접어 뱃지를 다는데, 여기서
                # 원시 일치만 하면 그 뱃지 조합이 0건이 되는 왕복 불가(라이브 5,393건,
                # 이중 리뷰 HIGH 합치). 목록·카운트·claim 이 전부 이 헬퍼를 지나므로
                # 세 경로가 같은 수를 본다.
                cond = or_(cond, func.coalesce(CompanyRow.industry, "") == "")
            stmt = stmt.where(cond)
    rset = {r.strip().lower() for r in (regions or []) if r and r.strip()}
    mset = {m.strip().upper() for m in (markets or []) if m and m.strip()}
    if listed or rset or mset:
        if listed:
            # 스토리지 계층 방어선 — API 는 Literal 로 막지만(422), 비API 직접 호출의 오타·
            # 대문자('LISTED')가 조용히 0건이 되지 않게 fail-loud.
            if listed not in _VALID_LISTED:
                raise ValueError(f"허용되지 않은 listed 값: {listed}")
        stmt = stmt.join(
            DiscoveredCompanyRow,
            CompanyRow.canonical_key == DiscoveredCompanyRow.canonical_key,
        )
        if listed:
            stmt = stmt.where(DiscoveredCompanyRow.listed == listed)
        if rset:
            stmt = stmt.where(func.lower(DiscoveredCompanyRow.region).in_(rset))
        if mset:
            stmt = stmt.where(func.upper(DiscoveredCompanyRow.market).in_(mset))
    return stmt


def _has_queue_filters(
    countries: Sequence[str] | None,
    industries: Sequence[str] | None,
    listed: str | None,
    regions: Sequence[str] | None = None,
    markets: Sequence[str] | None = None,
) -> bool:
    """국가/업종/상장/지역/시장 중 실제로 거를 값이 하나라도 있으면 True(빈 값=전체)."""
    return (
        bool(countries) or bool(industries) or bool(listed) or bool(regions) or bool(markets)
    )


def list_regions(session: Session) -> list[str]:
    """수집된 KR 지역 라벨 distinct 목록(정렬) — 큐 지역필터 옵션의 단일 출처.

    고정 목록 대신 실제 저장값을 쓴다(데이터 없는 지역 옵션 방지). 지역 미상(NULL)은
    옵션이 아니라 '필터 안 걸었을 때 전체'로만 접근 가능하다.
    KR 로 한정한다 — 해외 소스(CH·EDGAR·GLEIF…)는 region 에 도시/주 원문을 그대로
    넣어(GB 만 1,600+ 도시) 옵션 목록을 오염시키고, PO 결정(2026-08-25)으로 해외 지역
    필터는 불필요하다.
    """
    rows = session.scalars(
        select(DiscoveredCompanyRow.region)
        .where(DiscoveredCompanyRow.region.is_not(None), DiscoveredCompanyRow.country == "KR")
        .distinct()
        .order_by(DiscoveredCompanyRow.region)
    ).all()
    return [r for r in rows if r]


def list_markets(session: Session) -> list[str]:
    """수집된 시장 보드(KOSPI/KOSDAQ…) distinct 목록(정렬) — 큐 시장필터 옵션의 단일 출처.

    :func:`list_regions` 와 같은 이유로 고정 목록이 아니라 실제 저장값을 쓴다(데이터 없는
    보드 옵션 방지 — FE 는 이 목록이 비면 자체 폴백 어휘를 쓴다). 미상(NULL)은 옵션이
    아니라 '필터 안 걸었을 때 전체'로만 접근 가능하다.
    """
    rows = session.scalars(
        select(DiscoveredCompanyRow.market)
        .where(DiscoveredCompanyRow.market.is_not(None))
        .distinct()
        .order_by(DiscoveredCompanyRow.market)
    ).all()
    return [m for m in rows if m]


def queue_stock(session: Session) -> list[dict]:
    """세그먼트별 대기 재고 — (국가, 업종, 상장) 그룹의 pending·미점유 카운트.

    작업자가 필터 조합을 걸기 **전에** 잔량을 보여주기 위한 집계(빈 조합을 골라 헛걸음하는
    상황 방지 — 2026-08-19 P0). 값 축과 왕복 계약:
    - 국가: 등록국은 :func:`resolve_country` 로 ISO2 에 접고('KR'/'대한민국' 합산 —
      필터 쪽 :func:`country_match_set` 별칭 확장과 대칭), **미등록 표기는 원문 유지**
      (필터의 lower 원문 일치로 왕복 가능 — 단 FE 옵션 목록엔 없을 수 있음),
      빈값은 '' 그대로(국가 필터로 도달 불가 — FE 는 '미상' 으로 구분 렌더).
    - 업종: 빈값 → ``UNCLASSIFIED``('미분류'). 필터 쪽도 '미분류' 요청 시 빈값 행을
      포함하도록 대칭 처리돼 있다(:func:`_apply_queue_filters`).
    - 상장: 빈값/NULL → 'unknown'(스키마상 NOT NULL default 라 방어적).
    지역·시장 축은 미포함 — 5축 전체 곱은 조합 폭발이라, 필요해지면 요청 파라미터로
    슬라이스 확장(ponytail).

    한 회사 = 큐 행 1개(``review_id_for(company_id, 'email')`` 결정적 PK)라 count(*) 가
    곧 회사 수다. 조인은 전부 1:1(id/canonical_key 유일) — 행 증식 없음. 발견 원장이
    없는 고아 company 도 누락되지 않게 outer join(+unknown 폴백 — ``_listed_by_company``
    와 동일 의미). 호출당 원장 group by 1회(실측 ~140ms) — 폴링용이 아니다(API docstring
    의 FE 계약 참고).
    """
    rows = session.execute(
        select(
            CompanyRow.country,
            CompanyRow.industry,
            func.coalesce(DiscoveredCompanyRow.listed, "unknown"),
            func.count(),
        )
        .select_from(ReviewQueueRow)
        .join(CompanyRow, CompanyRow.id == ReviewQueueRow.company_id)
        .outerjoin(
            DiscoveredCompanyRow,
            DiscoveredCompanyRow.canonical_key == CompanyRow.canonical_key,
        )
        .where(ReviewQueueRow.status == PENDING, ReviewQueueRow.claimed_by.is_(None))
        .group_by(
            CompanyRow.country, CompanyRow.industry, DiscoveredCompanyRow.listed
        )
    ).all()
    agg: dict[tuple[str, str, str], int] = {}
    for country, industry, listed, n in rows:
        resolved = resolve_country(country or "")
        country_label = resolved.iso2 if resolved else (country or "").strip()
        key = (
            country_label,
            (industry or "").strip() or UNCLASSIFIED,
            (listed or "").strip() or "unknown",
        )
        agg[key] = agg.get(key, 0) + int(n)
    return [
        {"country": c, "industry": i, "listed": li, "n": n}
        for (c, i, li), n in sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


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
    regions: Sequence[str] | None = None,
    markets: Sequence[str] | None = None,
) -> int:
    """큐 항목 수(선택적 상태 + 국가/업종/상장/지역/시장 작업범위 필터) — 점유 중 행 제외.

    누군가 점유(claim)한 행은 전체큐에서 숨긴다(전체큐 = 아직 아무도 안 받아간 작업).
    확정/거부 행은 점유가 해제되므로 상태별 카운트엔 영향 없다. 필터가 있으면
    CompanyRow(상장·지역·시장은 DiscoveredCompanyRow)를 조인해 카운트한다 — 조인은 모두
    1:1(canonical_key/id 유일)이라 행 증식이 없어 count 가 정확하다.
    """
    if not _has_queue_filters(countries, industries, listed, regions, markets):
        stmt = (
            select(func.count())
            .select_from(ReviewQueueRow)
            .where(ReviewQueueRow.claimed_by.is_(None))
        )
        if status is not None:
            stmt = stmt.where(ReviewQueueRow.status == status)
        return int(session.scalar(stmt) or 0)
    stmt = (
        select(func.count())
        .select_from(ReviewQueueRow)
        .join(CompanyRow, ReviewQueueRow.company_id == CompanyRow.id)
        .where(ReviewQueueRow.claimed_by.is_(None))
    )
    if status is not None:
        stmt = stmt.where(ReviewQueueRow.status == status)
    stmt = _apply_queue_filters(
        stmt, countries=countries, industries=industries, listed=listed, regions=regions,
        markets=markets,
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
    regions: Sequence[str] | None = None,
    markets: Sequence[str] | None = None,
    sort_by: str | None = None,
    sort_dir: str = "asc",
) -> list[dict]:
    """큐 항목을 회사 정보와 함께 DTO dict 목록으로 반환한다(큐 행과 1:1).

    이메일 검증 신호(status/mx/smtp)는 :func:`_email_signal_map` 로 별도 평탄화해, 회사가
    이메일을 여럿 가져도 큐 행이 복제되지 않게 한다. 정렬에 ``id`` 최종 타이브레이커를
    더해 offset 페이지네이션이 안정적이다. 국가/업종/상장 작업범위 필터는 선택적으로
    적용한다(:func:`_apply_queue_filters`). 점유(claim) 중 행은 :func:`count_reviews` 와
    동일하게 제외한다(전체큐 = 미점유 작업만 — 단건 조회 :func:`get_review` 는 무관).
    ``sort_by``(#238, :data:`QUEUE_SORT_KEYS` 중 1개)가 주어지면 그 키+방향으로 전체
    결과를 서버 정렬한다(페이지 경계에서도 순서 일관). 기본(sort_by 없음)은 LIFO —
    status 업무순위(pending 먼저) 안에서 **최신 크롤분(발견 first_seen 역순) 최상단**.
    """
    stmt = (
        select(ReviewQueueRow, CompanyRow)
        .join(CompanyRow, ReviewQueueRow.company_id == CompanyRow.id)
        .where(ReviewQueueRow.claimed_by.is_(None))
    )
    if status is not None:
        stmt = stmt.where(ReviewQueueRow.status == status)
    stmt = _apply_queue_filters(
        stmt, countries=countries, industries=industries, listed=listed, regions=regions,
        markets=markets,
    )
    if sort_by:
        pg = session.bind is not None and session.bind.dialect.name == "postgresql"
        expr = _sort_expression(sort_by, pg=pg)
        stmt = stmt.order_by(
            expr.desc() if sort_dir == "desc" else expr.asc(), ReviewQueueRow.id
        )
    else:
        # 기본 = LIFO(최신 적재분 최상단, 2026-07-13 PO 요청). 정렬 키 = **큐 적재시각**
        # ``review_queue.created_at``(#352) — 종전 근사키(발견 first_seen 별칭 JOIN)는
        # "발견은 옛날, 적재는 지금"인 백필 승격 행을 바닥에 묻었다. 기존 행은
        # 마이그레이션이 first_seen 으로 백필해 순서를 보존했고, 자체 컬럼 정렬이라
        # JOIN 도 사라진다(성능 동등 이상 — created_at 인덱스).
        # status 는 원시 문자열(알파벳순 confirmed<pending — 확정건이 위로 오는 버그,
        # Codex 리뷰 HIGH-1)이 아니라 업무순위 CASE(#238 정렬키와 동일)로. 확정/거부는
        # 점유 해제로 미점유 풀에 재노출되므로 pending 먼저가 실동작에 중요하다.
        # 범위 주의: LIFO 는 이 목록 조회 전용 — 클레임 배정(_claim_more)·내 점유 목록
        # 순서는 별개(PO 확인 후 후속).
        stmt = stmt.order_by(
            _sort_expression("status"), ReviewQueueRow.created_at.desc(), ReviewQueueRow.id
        )
    stmt = stmt.limit(limit).offset(offset)
    rows = session.execute(stmt).all()
    ids = [company.id for _, company in rows]
    signals = _email_signals_by_value(session, ids)
    forms = _forms_by_company(session, ids)
    listed_map = _listed_by_company(session, [company for _, company in rows])
    return [_to_dict(rq, company, signals, forms, listed_map) for rq, company in rows]


def get_review(session: Session, review_id: str) -> dict | None:
    """단건 큐 항목 DTO(없으면 None). 목록과 동일한 후보별 신호를 쓴다."""
    rq = session.get(ReviewQueueRow, review_id)
    if rq is None:
        return None
    company = session.get(CompanyRow, rq.company_id)
    signals = _email_signals_by_value(session, [rq.company_id])
    forms = _forms_by_company(session, [rq.company_id])
    return _to_dict(rq, company, signals, forms, _listed_by_company(session, [company]))


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
    homepage: str | None = None,
    has_form: bool | None = None,
    note: str | None = None,
    has_attachment: bool | None = None,
    manager: str | None = None,
    remove_emails: Sequence[str] | None = None,
    now: datetime | None = None,
) -> dict | None:
    """큐 항목 상태(확정/거부/보류)와 선택 후보를 갱신하고 감사 이력을 적재한다.

    없으면 None, 잘못된 상태면 ValueError. ``selected`` 가 주어지면 후보 목록에 있어야
    하며(아니면 ValueError), 확정/거부 시 사람이 고른 최종 이메일을 기록한다. ``homepage``
    가 주어지면(#185) 회사(CompanyRow)의 홈페이지를 갱신한다 — URL 형식 검증은 상위
    (API 스키마) 책임이라 여기선 값을 그대로 반영한다. ``has_form``(#241, ``None``=변경
    없음)이 주어지면 문의폼 유무를 교정한다: ``False`` 는 저장된 폼 연락처를 삭제하고,
    ``True`` 인데 폼이 없으면 실제 폼 URL 을 모르므로(FE 는 유무만 교정) **홈페이지를
    진입 링크로 저장**한다(엑셀 E 컬럼=클릭 이동 목적과 정합 — 홈페이지도 없으면
    ValueError). ``remove_emails`` 는 **실제로 존재하지 않는 이메일** 삭제 목록으로,
    후보와 연락처 행을 함께 지운다(:func:`_apply_email_removals`) — 삭제 후 남은 이메일이
    없고 문의폼이 있으면 엑셀 J 가 자연히 "사이트 내 문의폼"이 된다. ``note`` 는 검수자
    기타 메모(문의폼 미발송 사유 등, ``None``=변경 없음·
    빈 문자열=지움) — 엑셀 L(기타) 컬럼으로 export 된다. 처리자(assignee/assignee_id)와
    시각(reviewed_at)을 큐 행에 남기고, 변경 1건마다 :class:`ReviewAuditRow` 를 append 해
    책임추적 이력을 보존한다(홈페이지·폼 변경 전/후 값도 같은 행에 기록). 점유는 영구
    귀속이라 **타인이 점유한 항목이면 시간 경과와 무관하게** :class:`ReviewConflict`.
    미점유 동시수정은 FOR UPDATE 행잠금 + 종료상태 재진입 가드로 차단한다(타인이 이미
    확정/거부한 항목의 재확정/재거부도 :class:`ReviewConflict` — 같은 처리자는 허용).
    처리자 미상(assignee_id NULL)으로 확정된 과거 행도 로그인 사용자의 재처리를 막는다
    (교차리뷰 MED — 익명 확정분을 아무나 덮지 못하게; 내부 호출끼리(None↔None)는 허용).
    """
    if status not in _VALID_STATUSES:
        raise ValueError(f"허용되지 않은 상태: {status}")
    # FOR UPDATE 행잠금 — 미점유 항목을 두 검수자가 동시에 확정하는 last-write-wins 차단
    # (전수리뷰): 두 번째 트랜잭션은 첫 커밋을 기다렸다가 아래 종료상태 가드에 걸린다.
    # populate_existing 필수(교차리뷰 HIGH 실측): confirm 라우트가 register_edited_email 로
    # 같은 세션에 행을 먼저 로드하므로, 없으면 잠금 SELECT 가 나가도 stale 객체가 반환돼
    # 가드가 무력화된다. SQLite(단위테스트)는 FOR UPDATE 를 무시한다 — 동시성 보장은
    # PG(운영) 전용이고, 단위테스트는 순차 실행이라 통과할 뿐이다(주석 오해 방지).
    rq = session.get(
        ReviewQueueRow, review_id, with_for_update=True, populate_existing=True
    )
    if rq is None:
        return None
    when = now or datetime.now(timezone.utc)
    # 클레임 백스톱(동시성) — 점유자가 타인이면 충돌. 처리자 없는 내부 호출(assignee_id
    # None)도 점유 항목엔 동일 적용된다(점유 = 그 계정의 배타 작업분).
    if rq.claimed_by is not None and rq.claimed_by != assignee_id:
        raise ReviewConflict("다른 직원이 처리 중인 항목입니다. 새로고침 후 다시 시도하세요.")
    # 종료상태 재진입 가드 — 이미 타인이 확정/거부한 항목의 재확정/재거부는 충돌로 거절해
    # 마지막 저장이 앞선 검수를 덮는 것을 막는다. 같은 처리자의 재처리(이메일 정정 등)와
    # 내부 호출(assignee_id=None)의 상태 복원은 기존대로 허용.
    if (
        status in (CONFIRMED, REJECTED)
        and rq.status in (CONFIRMED, REJECTED)
        and rq.assignee_id != assignee_id
    ):
        raise ReviewConflict("이미 다른 직원이 처리한 항목입니다. 새로고침 후 확인하세요.")
    # 삭제 먼저 — 같은 요청에서 '죽은 이메일 삭제 + 남은 후보 선택'이 한 번에 되도록
    # selected 검증보다 앞에 둔다(삭제된 주소를 selected 로 보내면 아래에서 400).
    removed = _apply_email_removals(session, rq, remove_emails)
    if selected is not None:
        if selected not in _parse_candidates(rq):
            raise ValueError(f"후보에 없는 선택: {selected}")
        rq.selected = selected
        rq.selected_by_human = True  # 사람 명시 선택 — 이후 재크롤에서 보존.
    company = session.get(CompanyRow, rq.company_id)
    homepage_before = homepage_after = None
    if homepage is not None and company is not None:
        homepage_before = company.homepage
        company.homepage = homepage
        homepage_after = homepage
    form_before, form_after = _apply_form_correction(
        session, rq.company_id, has_form, homepage=homepage, company=company
    )
    if note is not None:
        # ponytail: note 는 감사이력(before/after) 미기록 — 최종값만 의미 있는 자유
        # 메모라 의도적 생략. 책임추적이 필요해지면 ReviewAuditRow 에 컬럼 추가.
        rq.note = note.strip() or None  # 빈 문자열=메모 지움.
    if has_attachment is not None:
        # ponytail: note 와 동일하게 감사이력 미기록 — 최종값만 의미 있는 검수 체크값.
        rq.has_attachment = has_attachment
    if manager is not None:
        rq.manager = manager.strip() or None  # 빈 문자열=담당자 지움.
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
                homepage_before=homepage_before,
                homepage_after=homepage_after,
                form_before=form_before,
                form_after=form_after,
                emails_removed=json.dumps(removed, ensure_ascii=False) if removed else None,
                at=when,
            )
        )
    session.flush()
    return get_review(session, review_id)


def _apply_email_removals(
    session: Session, rq: ReviewQueueRow, emails: Sequence[str] | None
) -> list[str]:
    """실존하지 않는 이메일을 큐 후보 + 연락처에서 삭제하고 실제 삭제된 주소를 돌려준다.

    검수자가 발송 테스트 등으로 '이 주소는 없다'를 확인했을 때 쓰는 교정이다. 후보에서만
    빼면 export(load_leads)가 남은 연락처로 폴백해 같은 주소가 다시 나가므로 **연락처
    행까지** 지운다(비교: 이메일이 하나도 안 남고 문의폼이 있으면 엑셀 J 가 "사이트 내
    문의폼"으로 떨어진다 — 피드백의 목적). 후보에 없던 이메일 연락처(role 배제분 등)도
    값이 일치하면 지운다 — export 폴백이 그것까지 집어들기 때문이다.
    비교는 대소문자 무시. 삭제된 주소가 현재 선택이면 선택을 해제한다.
    자식 ``EmailValidationRow`` 를 먼저 명시 삭제한다 — 두 테이블 사이에 ORM
    relationship 이 없어 flush 삭제 순서가 보장되지 않기 때문이다(save_lead 의 stale
    연락처 정리와 동일 패턴).
    감사행 적재는 호출부(set_review_status)의 처리자(assignee) 게이트를 따른다 —
    처리자 없는 내부 호출에 remove_emails 를 넘기지 말 것(삭제 기록이 안 남는다).
    """
    targets = {e.strip().lower() for e in emails or () if e and e.strip()}
    if not targets:
        return []
    dead = [
        row
        for row in session.scalars(
            select(ContactRow)
            .where(ContactRow.company_id == rq.company_id)
            .where(ContactRow.type == ContactType.EMAIL.value)
        )
        if row.value.lower() in targets
    ]
    removed: list[str] = [row.value for row in dead]
    if dead:
        ids = [row.id for row in dead]
        session.execute(
            delete(EmailValidationRow).where(EmailValidationRow.contact_id.in_(ids))
        )
        session.execute(delete(ContactRow).where(ContactRow.id.in_(ids)))
    cands = _parse_candidates(rq)
    kept = [c for c in cands if c.lower() not in targets]
    if len(kept) != len(cands):
        rq.candidates = json.dumps(kept, ensure_ascii=False)
        removed.extend(c for c in cands if c.lower() in targets and c not in removed)
    if rq.selected is not None and rq.selected.lower() in targets:
        rq.selected = None  # 대표 선택은 남은 후보의 선두로 자연 회귀(effective_selected).
        rq.selected_by_human = False
    if removed:
        log.info("review.emails_removed", company_id=rq.company_id, emails=removed)
    return removed


def _apply_form_correction(
    session: Session,
    company_id: str,
    has_form: bool | None,
    *,
    homepage: str | None,
    company: CompanyRow | None,
) -> tuple[str | None, str | None]:
    """문의폼 유무 교정(#241)을 연락처에 반영하고 감사용 (변경 전, 후) 폼 URL 을 돌려준다.

    ``None`` 은 변경 없음((None, None) 반환 — 감사행에 폼 변경 없음으로 남는다).
    ``False`` 는 회사의 폼 연락처 전부 삭제. ``True`` 는 폼이 이미 있으면 그 폼을 사람
    확인으로 **승격**(confidence=1.0·method=manual — 저신뢰 폴백 폼이 '사람 확인 필요'로
    재노출되지 않게), 없으면 사람이 '폼 있음'을 확인한 것이므로 홈페이지(이번 요청
    교정값 우선)를 진입 링크로 저장한다. 홈페이지조차 없으면 저장할 URL 이 없어
    ValueError(API 에서 400).
    재크롤이 폼을 다시 감지/미감지하면 이 교정은 덮일 수 있다 — 홈페이지 교정(#185)과
    동일한 내구성 한계로, 수용된 선례를 따른다. 감사행 적재는 호출부(set_review_status)
    의 처리자(assignee) 게이트를 따르므로 처리자 없는 내부 호출은 has_form 을 넘기지
    말 것(폼은 바뀌는데 감사기록이 안 남는다 — API 경로는 항상 처리자 세팅).
    """
    if has_form is None:
        return None, None
    form_rows = session.scalars(
        select(ContactRow)
        .where(ContactRow.company_id == company_id)
        .where(ContactRow.type == ContactType.FORM.value)
    ).all()
    before = form_rows[0].value if form_rows else None
    if not has_form:
        for row in form_rows:
            session.delete(row)
        return before, None
    if form_rows:
        # 이미 폼 있음 — URL 은 유지하고 사람 확인으로 승격(교차리뷰 반영: 저신뢰 0.3
        # 폴백 폼이 form_low_confidence 로 계속 재노출되던 것을 여기서 끝낸다).
        for row in form_rows:
            row.extract_method = ExtractMethod.MANUAL.value
            row.confidence = 1.0
        return before, before
    url = homepage or (company.homepage if company is not None else None)
    if not url:
        raise ValueError("문의폼 있음으로 교정하려면 회사 홈페이지가 필요합니다")
    from .repository import contact_id_for  # 순환 import 회피(repository→review 역방향).

    # ponytail: check-then-insert — 미점유 행을 두 직원이 동시에 has_form=true 확정하면
    # 같은 결정적 PK 로 충돌(한쪽 IntegrityError→500)할 수 있다. register_edited_email 의
    # 기존 수용 패턴과 동일한 한계 — 실측 발생 시 dialect upsert(on_conflict_do_nothing)로.
    session.add(
        ContactRow(
            id=contact_id_for(company_id, ContactType.FORM.value, url),
            company_id=company_id,
            type=ContactType.FORM.value,
            value=url,
            extract_method=ExtractMethod.MANUAL.value,
            confidence=1.0,
        )
    )
    return before, url


def _claim_more(
    session: Session,
    user_id: str,
    *,
    want: int,
    now: datetime,
    countries: Sequence[str] | None = None,
    industries: Sequence[str] | None = None,
    listed: str | None = None,
    regions: Sequence[str] | None = None,
) -> int:
    """미점유 pending 항목을 최대 ``want`` 개 이 직원에게 배타 배정한다.

    점유는 처리 전까지 영구 귀속(TTL 만료 재배정 없음 — 회수는 :func:`admin_reclaim` 만).
    PostgreSQL 은 ``FOR UPDATE SKIP LOCKED`` 로 6명 동시 요청에도 서로 다른 행을 받게
    한다(잠긴 행 건너뜀 — 동시 작업 큐의 표준). SQLite(테스트)는 미지원이라 평이한
    select(단일 라이터라 무해). 국가/업종/상장 작업범위 필터가 주어지면 CompanyRow
    (상장은 DiscoveredCompanyRow)를 조인해 그 조건의 행만 배정한다. 배정한 행 수를 반환한다.
    """
    if want <= 0:
        return 0
    stmt = (
        select(ReviewQueueRow.id)
        .where(ReviewQueueRow.status == PENDING)
        .where(ReviewQueueRow.claimed_by.is_(None))
    )
    if _has_queue_filters(countries, industries, listed, regions):
        stmt = stmt.join(CompanyRow, ReviewQueueRow.company_id == CompanyRow.id)
        stmt = _apply_queue_filters(
            stmt, countries=countries, industries=industries, listed=listed, regions=regions
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


def _my_claimed_rows(
    session: Session, user_id: str
) -> list[tuple[ReviewQueueRow, CompanyRow]]:
    """이 직원이 점유 중인(pending) 항목 (rq, company) 전체 목록 — 필터 무관.

    점유는 영구 귀속이라 작업범위 필터를 바꿔도 기존 점유는 반납되지 않고 화면에
    남는다(필터는 신규 배정에만 적용 — :func:`claim_work`).
    """
    stmt = (
        select(ReviewQueueRow, CompanyRow)
        .join(CompanyRow, ReviewQueueRow.company_id == CompanyRow.id)
        .where(ReviewQueueRow.claimed_by == user_id)
        .where(ReviewQueueRow.status == PENDING)
        .order_by(CompanyRow.name, ReviewQueueRow.id)
    )
    rows = session.execute(stmt).all()
    return [(rq, company) for rq, company in rows]


def _rows_to_dtos(
    session: Session, rows: Sequence[tuple[ReviewQueueRow, CompanyRow]]
) -> list[dict]:
    """(rq, company) 행 목록 → API DTO dict 목록(신호/폼/상장여부 배치 조회 공통 경로)."""
    ids = [c.id for _, c in rows]
    signals = _email_signals_by_value(session, ids)
    forms = _forms_by_company(session, ids)
    listed_map = _listed_by_company(session, [company for _, company in rows])
    return [_to_dict(rq, company, signals, forms, listed_map) for rq, company in rows]


def my_work(session: Session, user_id: str) -> list[dict]:
    """내 점유 작업분 DTO 목록 — 부작용 없는 조회(새로고침·재로그인 복원용)."""
    return _rows_to_dtos(session, _my_claimed_rows(session, user_id))


def my_history(
    session: Session, user_id: str, *, status: str, limit: int = 200
) -> list[dict]:
    """내가 처리한(확정/거부) 이력 DTO 목록 — ``reviewed_at`` 최신순 최근 ``limit``건.

    처리 완료 항목은 :func:`set_review_status` 가 ``claimed_by`` 를 정리하므로(점유는
    종료 상태에서 무의미) 이력 매칭은 점유가 아니라 ``assignee_id``(마지막 처리자)로
    한다. #191: ``GET /queue/mine?status=confirmed|rejected``.
    """
    stmt = (
        select(ReviewQueueRow, CompanyRow)
        .join(CompanyRow, ReviewQueueRow.company_id == CompanyRow.id)
        .where(ReviewQueueRow.assignee_id == user_id)
        .where(ReviewQueueRow.status == status)
        .order_by(ReviewQueueRow.reviewed_at.desc())
        .limit(limit)
    )
    rows = session.execute(stmt).all()
    return _rows_to_dtos(session, [(rq, company) for rq, company in rows])


def claim_work(
    session: Session,
    user_id: str,
    *,
    batch: int,
    cap: int,
    now: datetime | None = None,
    countries: Sequence[str] | None = None,
    industries: Sequence[str] | None = None,
    listed: str | None = None,
    regions: Sequence[str] | None = None,
) -> list[dict]:
    """새 작업을 최대 ``batch`` 개 추가 점유하고(총량 ``cap`` 상한) 내 작업분 전체를 반환한다.

    "작업 받기" 1회 = +batch 추가(additive). 남은 작업이 있어도 다른 세그먼트 지시를
    받아 미리 받아둘 수 있고(선취), 한 계정의 동시 점유 총량은 cap 을 넘지 못한다
    (cap 도달 시 신규 배정 0 — 기존 점유만 반환). 6명이 동시에 불러도 SKIP LOCKED 로
    작업분이 겹치지 않는다. 점유는 처리 전까지 영구 귀속: 반납·TTL 복귀가 없고,
    국가/업종/상장 작업범위 필터는 **신규 배정에만** 적용된다(기존 점유는 필터 무관 유지).
    부작용 없는 조회는 :func:`my_work`.
    """
    now = now or datetime.now(timezone.utc)
    current = _my_claimed_rows(session, user_id)
    want = min(batch, cap - len(current))
    if want > 0:
        _claim_more(
            session, user_id, want=want, now=now,
            countries=countries, industries=industries, listed=listed, regions=regions,
        )
        session.flush()
    return my_work(session, user_id)


def admin_reclaim(
    session: Session,
    user_id: str,
    *,
    actor_id: str | None = None,
    actor_username: str = "",
    now: datetime | None = None,
) -> int:
    """관리자 회수 — 해당 계정이 점유한 pending 항목을 전부 풀로 되돌린다(회수 수 반환).

    영구 배정 모델에서 유일한 점유 해제 수단(퇴사·장기부재 대응). 회수 1건마다 감사행
    (action="reclaim", actor=관리자)을 남겨 '누가 언제 풀었는지'를 보존한다.
    """
    when = now or datetime.now(timezone.utc)
    ids = list(
        session.scalars(
            select(ReviewQueueRow.id)
            .where(ReviewQueueRow.claimed_by == user_id)
            .where(ReviewQueueRow.status == PENDING)
        ).all()
    )
    if not ids:
        return 0
    session.execute(
        update(ReviewQueueRow)
        .where(ReviewQueueRow.id.in_(ids))
        .values(claimed_by=None, claimed_at=None)
    )
    for rid in ids:
        session.add(
            ReviewAuditRow(
                id=uuid4().hex[:12],
                review_id=rid,
                actor_id=actor_id,
                actor_username=actor_username,
                action="reclaim",
                selected=None,
                at=when,
            )
        )
    session.flush()
    return len(ids)


def _listed_by_company(
    session: Session, companies: Sequence[CompanyRow | None]
) -> dict[str, tuple[str, str | None]]:
    """회사 목록의 상장여부·시장 보드를 원장에서 배치 조회해
    ``company_id → (listed, market)`` 로 돌려준다.

    listed/market 은 발견 원장(DiscoveredCompanyRow)에만 있어 canonical_key 로 IN 1회
    조회한다(페이지당 1쿼리 — signals/forms 배치와 동일 패턴, N+1 방지). 원장 미존재는
    (unknown, None).
    """
    present = [c for c in companies if c is not None]
    if not present:
        return {}
    keys = {c.canonical_key for c in present}
    rows = session.execute(
        select(
            DiscoveredCompanyRow.canonical_key,
            DiscoveredCompanyRow.listed,
            DiscoveredCompanyRow.market,
        ).where(DiscoveredCompanyRow.canonical_key.in_(keys))
    ).all()
    by_key = {key: (listed, market) for key, listed, market in rows}
    return {c.id: by_key.get(c.canonical_key, ("unknown", None)) for c in present}


def _to_dict(
    rq: ReviewQueueRow,
    company: CompanyRow | None,
    signals: dict[tuple[str, str], _EmailSignal],
    forms: dict[str, tuple[str, float]] | None = None,
    listed_map: dict[str, tuple[str, str | None]] | None = None,
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
    listed, market = (listed_map or {}).get(rq.company_id, ("unknown", None))
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
        "listed": listed,
        "market": market,
        "homepage": company.homepage if company else None,
        "site_alive": company.site_alive if company else False,
        # 검수자 기타 메모(문의폼 미발송 사유 등) — 엑셀 L(기타) 컬럼.
        "note": rq.note,
        # 첨부파일 유무(검수자 체크, None=미확인)·상대 회사 담당자명 — 엑셀 H 컬럼.
        "has_attachment": rq.has_attachment,
        "manager": rq.manager,
        # 문의폼 URL + 신뢰도(없으면 None) — 저신뢰(폴백 0.3)면 리뷰레인서 '사람 확인' 표기.
        "form": form_url,
        "form_confidence": form_conf,
        "form_low_confidence": form_url is not None and form_conf is not None
        and form_conf < FORM_REVIEW_THRESHOLD,
        "email_status": rep_status,
        "email_mx": rep_mx,
        "email_smtp": rep_smtp,
    }
