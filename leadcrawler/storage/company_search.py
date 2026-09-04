"""관리자용 회사 DB 검색 — 큐 상태와 무관하게 ``company`` 전체를 텍스트로 찾는다.

회사명·홈페이지·연락처 값(이메일·폼 URL만)·발견 원장 영문명(JP 등) 부분일치(대소문자 무시).
결과엔 연락처와 큐 상태를 붙여 "이 회사가 어디까지 왔나"를 한 줄로 보여준다(중복 확인·
수동 조회용). ``%``/``_`` 는 이스케이프해 사용자 입력이 와일드카드로 해석되지 않게 한다.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import ContactType
from ..schema import (
    CompanyRow,
    ContactRow,
    DiscoveredCompanyRow,
    EmailValidationRow,
    ReviewQueueRow,
)
from .review import _listed_by_company

_ESCAPE = "\\"
_SEARCHED_CONTACT_TYPES = (ContactType.EMAIL.value, ContactType.FORM.value)


def _like_pattern(q: str) -> str:
    text = q.strip()
    for ch in (_ESCAPE, "%", "_"):
        text = text.replace(ch, _ESCAPE + ch)
    return f"%{text}%"


def search_companies(
    session: Session,
    q: str,
    *,
    limit: int = 50,
    offset: int = 0,
    review_status: str | None = None,
) -> tuple[list[dict], int]:
    """``q`` 부분일치 회사 목록(dict)과 전체 건수를 반환한다. 빈 ``q`` 는 (빈 목록, 0).

    ``review_status`` 가 주어지면 이메일 검증 큐가 그 상태인 회사만 남긴다
    (예: ``"confirmed"`` = 확정만 — 큐 미적재 회사는 어떤 상태에도 안 걸린다).
    """
    if not q.strip():
        return [], 0
    pattern = _like_pattern(q)
    contact_hit = select(ContactRow.company_id).where(
        ContactRow.type.in_(_SEARCHED_CONTACT_TYPES),
        ContactRow.value.ilike(pattern, escape=_ESCAPE),
    )
    name_eng_hit = select(DiscoveredCompanyRow.canonical_key).where(
        DiscoveredCompanyRow.name_eng.ilike(pattern, escape=_ESCAPE)
    )
    match = or_(
        CompanyRow.name.ilike(pattern, escape=_ESCAPE),
        CompanyRow.homepage.ilike(pattern, escape=_ESCAPE),
        CompanyRow.id.in_(contact_hit),
        CompanyRow.canonical_key.in_(name_eng_hit),
    )
    if review_status is not None:
        status_hit = select(ReviewQueueRow.company_id).where(
            ReviewQueueRow.field == "email", ReviewQueueRow.status == review_status
        )
        match = match & CompanyRow.id.in_(status_hit)
    # ponytail: 선두 와일드카드 ILIKE 라 인덱스 미사용(4만 행·라이브 실측 ~180ms). 느려지면
    # pg_trgm GIN 인덱스(name·homepage·contact.value) 추가.
    total = session.execute(select(func.count()).select_from(CompanyRow).where(match)).scalar_one()
    # PG 의 en_US 콜레이션은 한글을 사전순으로 못 세운다(review._sort_expression 과 동일 우회).
    pg = session.bind is not None and session.bind.dialect.name == "postgresql"
    name_key = CompanyRow.name.collate("C") if pg else CompanyRow.name
    companies = list(
        session.scalars(
            select(CompanyRow).where(match).order_by(name_key, CompanyRow.id)
            .limit(limit).offset(offset)
        )
    )
    if not companies:
        return [], total
    ids = [c.id for c in companies]

    contacts: dict[str, list[dict]] = {cid: [] for cid in ids}
    forms: dict[str, str] = {}
    rows = session.execute(
        select(
            ContactRow.company_id, ContactRow.type, ContactRow.value, ContactRow.role,
            EmailValidationRow.status,
        )
        .outerjoin(EmailValidationRow, EmailValidationRow.contact_id == ContactRow.id)
        .where(ContactRow.company_id.in_(ids), ContactRow.type.in_(_SEARCHED_CONTACT_TYPES))
        .order_by(ContactRow.value)
    ).all()
    for cid, ctype, value, role, status in rows:
        if ctype == ContactType.FORM.value:
            forms.setdefault(cid, value)  # 회사당 대표 폼 1개(_forms_by_company 와 동일).
        else:
            contacts[cid].append({"value": value, "role": role, "status": status})

    reviews = {
        rq.company_id: rq
        for rq in session.scalars(
            select(ReviewQueueRow).where(
                ReviewQueueRow.company_id.in_(ids), ReviewQueueRow.field == "email"
            )
        )
    }
    listed_map = _listed_by_company(session, companies)
    items = []
    for c in companies:
        rq = reviews.get(c.id)
        listed, market = listed_map.get(c.id, ("unknown", None))
        items.append({
            "id": c.id,
            "canonical_key": c.canonical_key,
            "name": c.name,
            "country": c.country,
            "industry": c.industry,
            "homepage": c.homepage,
            "is_active": c.is_active,
            "site_alive": c.site_alive,
            "listed": listed,
            "market": market,
            "emails": contacts[c.id],
            "form": forms.get(c.id),
            "review_id": rq.id if rq else None,
            "review_status": rq.status if rq else None,
            "review_assignee": rq.assignee if rq else None,
        })
    return items, total
