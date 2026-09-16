"""도메인 오배정 해소 — 같은 홈페이지 host 를 이름이 다른 회사가 공유하는 그룹의 판정·계획·적용·되돌리기.

설계 ``docs/domain-conflict-design-2026-09-16.md`` §6(Claude·Codex 독립안 종합). 2026-09-16 실측:
company 51,612건 중 같은 country+host 를 다른 이름이 공유하는 그룹 715개/1,621행 — NPS name: 행이
검색 해석으로 남의 도메인을 받은 것이 주 원인(향우종합관리→yesform.com).

판정(그룹 단위·결정적·무료):
- **소유 근거** = ``dom:`` 키(소스가 도메인을 직접 줌) · 사람이 워크벤치에서 고친 홈페이지
  (``review_audit.homepage_after``) · 라틴 회사명↔도메인 경계 정합(``_name_matches``) · 홈페이지
  ``<title>`` 토큰 완전일치(opt-in fetch). **등록번호는 법인 동일성 근거이지 도메인 소유 근거가
  아니다**(``reg:dart`` 행도 도메인은 해석으로 받는다 — 삼지토건↔samji.com).
- 근거 있는 행 = ``owner``(2곳 이상이면 ``allow_shared`` — 계열사 정당 공유, 관계 데이터가 없어
  자동 판정 안 함). 근거 없는 행은 소유주와 이름 强일치면 ``same_entity``(dedup 몫), 소유주
  이름을 **접두**로 품으면 ``related``(지점·자회사 — 알테오젠바이오로직스/신성미네랄성남지점,
  손대지 않음), 등록처 키(``reg:``)·등록번호 보유면 ``review``(법인 확정 행은 사람 검토 —
  라이브 미리보기에서 케이제이인더스트리↔경주수출포장(kjep) 처럼 진짜 소유주가 뒤집힌 사례),
  그 외(``name:`` 키·근거 없음)만 ``auto_wrong``. 소유주가 없으면 전부 ``review``(워크벤치).

적용(``auto_wrong`` 만): 원장 domain·company.homepage 를 비우고(host 동일 시) site_alive=False,
그 host 의 이메일·문의폼 연락처만 삭제(전화 보존), 검증큐는 **pending 으로 되돌린다**(confirmed
포함 — PO 결정 2026-09-16: 틀린 도메인의 확정은 확정이 아님). company 행은 삭제하지 않는다.
계획 JSON 에 before 값을 담아 적용 시 재검증(동시 변경 덮어쓰기 방지)하고 ``rollback`` 으로
역적용한다. 비운 행은 ``fill.resolve_batch`` 가 재해석한다(승격됐지만 홈페이지 공백 행 포함).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..dedup import normalize_domain, tokenize_name
from ..logging import get_logger
from ..schema import (
    CompanyRow,
    ContactRow,
    DiscoveredCompanyRow,
    EmailValidationRow,
    ReviewAuditRow,
    ReviewQueueRow,
)
from ..sources.domain_resolver import _name_matches, _name_slug
from .near_dup import NAME_STRONG, _name_score, compare_tokens

log = get_logger("dedup.domain_conflict")

OWNER = "owner"
ALLOW_SHARED = "allow_shared"
SAME_ENTITY = "same_entity"
RELATED = "related"
AUTO_WRONG = "auto_wrong"
REVIEW = "review"

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TITLE_MIN = 3  # domain_resolver._KOREAN_TOKEN_MIN 과 같은 하한(짧은 상호는 title 근거 불인정).


class ConflictRow(BaseModel):
    """그룹 구성 회사 1건 + 판정 + (auto_wrong 이면) 적용 전 스냅샷."""

    company_id: str
    canonical_key: str
    name: str
    country: str
    host: str  # 정규화 홈페이지 root(그룹 키)
    reg_no: str | None = None
    evidence: list[str] = Field(default_factory=list)
    action: str = REVIEW
    before: dict = Field(default_factory=dict)  # ledger_domain·homepage·site_alive·queue·contacts


class ConflictPlan(BaseModel):
    generated_at: datetime
    groups: int
    by_action: dict[str, int]
    rows: list[ConflictRow]


def _compare_key(name: str) -> str:
    return "".join(compare_tokens(name))


def load_conflict_groups(session: Session) -> dict[tuple[str, str], list[dict]]:
    """승격 회사를 (country, 홈페이지 root)로 묶어 **이름이 다른 회사가 2곳 이상**인 그룹만 돌려준다."""
    stmt = (
        select(
            CompanyRow.id, CompanyRow.canonical_key, CompanyRow.name, CompanyRow.country,
            CompanyRow.homepage, CompanyRow.site_alive, DiscoveredCompanyRow.domain,
            DiscoveredCompanyRow.reg_no, DiscoveredCompanyRow.name_eng,
        )
        .join(DiscoveredCompanyRow, DiscoveredCompanyRow.canonical_key == CompanyRow.canonical_key)
        .where(DiscoveredCompanyRow.duplicate_of.is_(None), CompanyRow.homepage.is_not(None))
    )
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for cid, key, name, country, homepage, alive, dom, reg_no, name_eng in session.execute(stmt):
        host = normalize_domain(homepage)
        if not host:
            continue
        groups[(country or "", host)].append({
            "company_id": cid, "canonical_key": key, "name": name, "country": country or "",
            "host": host, "homepage": homepage, "site_alive": bool(alive), "ledger_domain": dom,
            "reg_no": reg_no, "name_eng": name_eng,
        })
    return {
        k: rows for k, rows in groups.items()
        if len(rows) >= 2 and len({_compare_key(r["name"]) for r in rows}) >= 2
    }


def audited_hosts(session: Session, company_ids: Iterable[str]) -> dict[str, set[str]]:
    """회사별로 사람이 워크벤치에서 확정·수정한 홈페이지 root 집합(review_audit.homepage_after)."""
    ids = list(company_ids)
    out: dict[str, set[str]] = defaultdict(set)
    if not ids:
        return out
    stmt = (
        select(ReviewQueueRow.company_id, ReviewAuditRow.homepage_after)
        .join(ReviewAuditRow, ReviewAuditRow.review_id == ReviewQueueRow.id)
        .where(ReviewQueueRow.company_id.in_(ids), ReviewAuditRow.homepage_after.is_not(None))
    )
    for cid, after in session.execute(stmt):
        root = normalize_domain(after)
        if root:
            out[cid].add(root)
    return out


def fetch_titles(hosts: Iterable[str], get_text: Callable[[str], str]) -> dict[str, str]:
    """host 당 1회 홈페이지 ``<title>`` 을 가져온다(실패는 조용히 건너뜀 — 근거 없음으로 처리)."""
    titles: dict[str, str] = {}
    for host in hosts:
        for url in (f"https://{host}", f"http://{host}"):
            try:
                m = _TITLE_RE.search(get_text(url))
            except Exception:  # noqa: BLE001 — 네트워크 실패는 근거 부재일 뿐 판정 오류가 아님
                continue
            if m:
                titles[host] = re.sub(r"\s+", " ", m.group(1)).strip()
                break
    return titles


def _evidence(row: dict, audited: dict[str, set[str]], titles: dict[str, str]) -> list[str]:
    host = row["host"]
    ev: list[str] = []
    key = row["canonical_key"]
    if key.startswith("dom:") and normalize_domain(key[4:]) == host:
        ev.append("dom_key")
    if host in audited.get(row["company_id"], ()):
        ev.append("audited_homepage")
    # name 의 영문 병기('(주)에이럭스(ALUX Co)')도 슬러그가 되게 name_eng 뒤에 name 도 본다 —
    # _name_matches 의 경계·길이 하한이 우연한 라틴 조각 오탐을 막는다.
    for nm in (row.get("name_eng"), row["name"]):
        slug = _name_slug(nm or "")
        if slug and _name_matches(slug, host):
            ev.append("latin_name")
            break
    title = titles.get(host)
    if title:
        core = _compare_key(row["name"])
        if len(core) >= _TITLE_MIN and core in tokenize_name(title):
            ev.append("title")
    return ev


def classify_group(
    rows: list[dict], audited: dict[str, set[str]], titles: dict[str, str] | None = None
) -> list[ConflictRow]:
    titles = titles or {}
    out = [
        ConflictRow(
            company_id=r["company_id"], canonical_key=r["canonical_key"], name=r["name"],
            country=r["country"], host=r["host"], reg_no=r.get("reg_no"),
            evidence=_evidence(r, audited, titles),
        )
        for r in rows
    ]
    owners = [c for c in out if c.evidence]
    if not owners:
        for c in out:
            c.action = REVIEW
        return out
    owner_tokens = [compare_tokens(o.name) for o in owners]
    owner_keys = ["".join(t) for t in owner_tokens]
    for c in out:
        if c.evidence:
            c.action = ALLOW_SHARED if len(owners) >= 2 else OWNER
            continue
        mine = compare_tokens(c.name)
        mine_key = "".join(mine)
        if any(_name_score(mine, ot) >= NAME_STRONG for ot in owner_tokens):
            c.action = SAME_ENTITY  # 같은 회사 — dedup-report/merge 몫
        elif any(len(k) >= _TITLE_MIN and mine_key.startswith(k) for k in owner_keys):
            c.action = RELATED  # 지점·자회사 표기 — 모기업 도메인 공유는 손대지 않음
        elif c.canonical_key.startswith("reg:") or c.reg_no:
            c.action = REVIEW  # 법인 확정 행 — 소유주가 뒤집힌 경우가 있어 사람 판정
        else:
            c.action = AUTO_WRONG
    return out


def _contact_host(c: ContactRow) -> str | None:
    if c.type == "email":
        return normalize_domain(c.value.rsplit("@", 1)[-1]) if "@" in c.value else None
    if c.type == "form":
        return normalize_domain(c.value)
    return None


def _snapshot(session: Session, row: ConflictRow, raw: dict) -> dict:
    """auto_wrong 행의 적용 전 값 — 재검증·되돌리기 근거."""
    from ..storage.review import review_id_for

    contacts = []
    for c in session.scalars(select(ContactRow).where(ContactRow.company_id == row.company_id)):
        if _contact_host(c) != row.host:
            continue
        ev = session.get(EmailValidationRow, c.id)
        contacts.append({
            "id": c.id, "type": c.type, "value": c.value, "role": c.role,
            "extract_method": c.extract_method, "confidence": c.confidence,
            "validation": None if ev is None else {
                "status": ev.status, "mx": ev.mx, "domain_match": ev.domain_match, "smtp": ev.smtp,
                "provider": ev.provider,
                "checked_at": ev.checked_at.isoformat() if ev.checked_at else None,
            },
        })
    rq = session.get(ReviewQueueRow, review_id_for(row.company_id, "email"))
    queue = None if rq is None else {
        "id": rq.id, "status": rq.status, "assignee": rq.assignee, "assignee_id": rq.assignee_id,
        "reviewed_at": rq.reviewed_at.isoformat() if rq.reviewed_at else None,
        "selected": rq.selected, "selected_by_human": rq.selected_by_human,
        "candidates": rq.candidates,
    }
    return {
        "ledger_domain": raw["ledger_domain"], "homepage": raw["homepage"],
        "site_alive": raw["site_alive"], "contacts": contacts, "queue": queue,
    }


def build_plan(
    session: Session, *, get_text: Callable[[str], str] | None = None,
    now: Callable[[], datetime] | None = None,
) -> ConflictPlan:
    """그룹 조회 → 판정 → auto_wrong 스냅샷까지 담은 계획(읽기 전용)."""
    groups = load_conflict_groups(session)
    all_ids = [r["company_id"] for rows in groups.values() for r in rows]
    audited = audited_hosts(session, all_ids)
    titles = fetch_titles({h for _, h in groups}, get_text) if get_text is not None else {}
    rows_out: list[ConflictRow] = []
    for rows in groups.values():
        raw_by_id = {r["company_id"]: r for r in rows}
        for c in classify_group(rows, audited, titles):
            if c.action == AUTO_WRONG:
                c.before = _snapshot(session, c, raw_by_id[c.company_id])
            rows_out.append(c)
    by_action: dict[str, int] = defaultdict(int)
    for c in rows_out:
        by_action[c.action] += 1
    return ConflictPlan(
        generated_at=(now or (lambda: datetime.now(timezone.utc)))(),
        groups=len(groups), by_action=dict(by_action), rows=rows_out,
    )


def _parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def apply_row(session: Session, row: ConflictRow, *, now: datetime) -> str:
    """auto_wrong 1행 적용. 반환 'applied' | 'stale'(before 와 현재값 불일치 — 건너뜀) | 'skipped'."""
    from ..storage.review import candidate_values_of, review_id_for

    if row.action != AUTO_WRONG:
        return "skipped"
    co = session.get(CompanyRow, row.company_id)
    if co is None or normalize_domain(co.homepage) != row.host:
        return "stale"  # 이미 바뀜(사람 수정·재해석) — 덮어쓰지 않는다.
    dc = session.get(DiscoveredCompanyRow, row.canonical_key)
    if dc is not None and normalize_domain(dc.domain) == row.host:
        dc.domain = None
    old_home = co.homepage
    co.homepage = None
    co.site_alive = False
    # 삭제는 스냅샷 id 가 아니라 **현재** 연락처를 host 로 다시 걸러서 — 계획 이후 추가된 그 host
    # 연락처가 남지 않게(리뷰 MED). 스냅샷은 되돌리기 복원용.
    for ct in session.scalars(select(ContactRow).where(ContactRow.company_id == row.company_id)):
        if _contact_host(ct) == row.host:
            session.delete(ct)  # email_validation 은 CASCADE(PG) — SQLite 도 PRAGMA FK ON.
    rq = session.get(ReviewQueueRow, review_id_for(row.company_id, "email"))
    if rq is not None:
        cands = [
            v for v in candidate_values_of(rq)
            if normalize_domain(v.rsplit("@", 1)[-1]) != row.host
        ]
        rq.candidates = json.dumps(cands, ensure_ascii=False)
        rq.status = "pending"
        rq.assignee = rq.assignee_id = rq.reviewed_at = None
        rq.selected, rq.selected_by_human = (cands[0] if cands else None), False
        session.add(ReviewAuditRow(
            id=uuid4().hex[:12], review_id=rq.id, actor_id=None,
            actor_username="system:domain-conflicts", action="domain_reset",
            selected=None, homepage_before=old_home, homepage_after=None, at=now,
        ))
    session.flush()
    log.info("domain_conflict.applied", company=row.company_id, host=row.host)
    return "applied"


def rollback_row(session: Session, row: ConflictRow) -> str:
    """apply_row 의 역적용(계획의 before 값으로 복원). 반환 'restored' | 'skipped'."""
    if row.action != AUTO_WRONG or not row.before:
        return "skipped"
    b = row.before
    co = session.get(CompanyRow, row.company_id)
    if co is None:
        return "skipped"
    q = b.get("queue")
    rq = session.get(ReviewQueueRow, q["id"]) if q else None
    if rq is not None and (rq.status != "pending" or rq.assignee is not None):
        # 적용 뒤 사람이 다시 판정한 큐 — 되돌리기가 그 판단을 덮지 않는다(apply 의 stale 과 대칭).
        return "stale"
    dc = session.get(DiscoveredCompanyRow, row.canonical_key)
    if dc is not None and not dc.domain and b.get("ledger_domain"):
        dc.domain = b["ledger_domain"]
    if not co.homepage:  # 재해석으로 새 홈페이지가 이미 들어왔으면 홈페이지·생존 판정 모두 보존.
        co.homepage = b.get("homepage")
        co.site_alive = bool(b.get("site_alive"))
    for c in b.get("contacts", []):
        if session.get(ContactRow, c["id"]) is None:
            session.add(ContactRow(
                id=c["id"], company_id=row.company_id, type=c["type"], value=c["value"],
                role=c["role"], extract_method=c["extract_method"], confidence=c["confidence"],
            ))
            session.flush()
            v = c.get("validation")
            if v:
                session.add(EmailValidationRow(
                    contact_id=c["id"], status=v["status"], mx=v["mx"],
                    domain_match=v["domain_match"], smtp=v["smtp"], provider=v["provider"],
                    checked_at=_parse_dt(v["checked_at"]),
                ))
    if rq is not None:
        rq.status, rq.assignee, rq.assignee_id = q["status"], q["assignee"], q["assignee_id"]
        rq.reviewed_at = _parse_dt(q["reviewed_at"])
        rq.selected, rq.selected_by_human = q["selected"], q["selected_by_human"]
        rq.candidates = q["candidates"]
    session.flush()
    return "restored"
