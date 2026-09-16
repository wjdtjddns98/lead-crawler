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
from typing import Protocol
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
    titles_fetched: int = 0  # --fetch-titles 로 실제 title 을 얻은 host 수(0 이면 title 근거 없음)
    llm_judged: int = 0  # --llm 관계 판정 호출 수


_CJK_RE = re.compile(r"[가-힣぀-ヿ一-鿿]")


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
    """회사별로 사람이 워크벤치에서 **가장 최근에** 고친 홈페이지 root(review_audit.homepage_after).

    append-only 이력이라 과거 값(A→B 로 재수정한 A)이 근거로 남지 않게 최신 1건만 쓴다.
    """
    ids = list(company_ids)
    out: dict[str, set[str]] = defaultdict(set)
    if not ids:
        return out
    stmt = (
        select(ReviewQueueRow.company_id, ReviewAuditRow.homepage_after)
        .join(ReviewAuditRow, ReviewAuditRow.review_id == ReviewQueueRow.id)
        .where(ReviewQueueRow.company_id.in_(ids), ReviewAuditRow.homepage_after.is_not(None))
        .order_by(ReviewAuditRow.at.desc())
    )
    seen: set[str] = set()
    for cid, after in session.execute(stmt):
        if cid in seen:
            continue
        seen.add(cid)
        root = normalize_domain(after)
        if root:
            out[cid].add(root)
    return out


def fetch_titles(hosts: Iterable[str], get_text: Callable[[str], str]) -> dict[str, str]:
    """host 당 1회 홈페이지 ``<title>`` 을 가져온다(실패는 조용히 건너뜀 — 근거 없음으로 처리)."""
    titles: dict[str, str] = {}
    failed = 0
    for host in hosts:
        for url in (f"https://{host}", f"http://{host}"):
            try:
                m = _TITLE_RE.search(get_text(url))
            except Exception:  # noqa: BLE001 — 네트워크 실패는 근거 부재일 뿐 판정 오류가 아님
                continue
            if m:
                titles[host] = re.sub(r"\s+", " ", m.group(1)).strip()
                break
        else:
            failed += 1
    log.info("domain_conflict.titles", fetched=len(titles), failed=failed)
    return titles


def _title_supports(name: str, title: str) -> bool:
    """회사명이 title 에 **토큰 경계로** 들어 있는지 — 단일 토큰 완전일치 또는 다중 토큰 연속 부분열."""
    toks = compare_tokens(name)
    core = "".join(toks)
    # 단일 토큰 **완전일치**는 2글자 상호(기아·세방·일진·한올)도 인정 — 검색 후보 여러 개를 가르는
    # 해석기(_KOREAN_TOKEN_MIN=3)와 달리 여기선 이미 정해진 한 페이지의 title 이라 오탐 여지가 작다.
    if len(core) < 2:
        return False
    ttoks = tokenize_name(title)
    if core in ttoks:
        return True
    n = len(toks)
    return len(core) >= _TITLE_MIN and n >= 2 and any(
        ttoks[i:i + n] == toks for i in range(len(ttoks) - n + 1)
    )


# 소유 근거 강도 — 사람 수정 이력(audited_homepage)만으로는 소유주가 되지 못한다: 2026-09-16 라이브에서
# '레이'(의료기기, dom:raymedical)의 홈페이지를 kia.com 으로 잘못 고친 이력 하나가 기아를 auto_wrong 으로
# 밀어냈다. 강한 근거(소스 도메인·라틴 정합·title)가 하나는 있어야 한다.
_STRONG = frozenset({"dom_key", "latin_name", "title"})

_REL_PROMPT = (
    "같은 웹사이트 도메인을 공유하는 두 회사의 관계를 판정하라.\n"
    "- same: 같은 회사(한글/영문/약어/법인격/지점·공장·사업장 표기 차이일 뿐)\n"
    "- affiliate: 같은 그룹의 계열사·자회사·모회사·지주사\n"
    "- different: 무관한 별개 회사(해당 도메인은 한쪽 회사 것이 아님)\n"
    "- unknown: 근거 부족\n"
    '오직 아래 JSON 만 출력하라: {{"relation": "same|affiliate|different|unknown", "reason": "한 문장"}}\n\n'
    "회사 A: {name_a!r}\n회사 B: {name_b!r}\n공유 도메인: {host!r}\n국가: {country!r}"
)
_RELATIONS = ("same", "affiliate", "different", "unknown")


class RelationJudge(Protocol):
    """두 회사의 관계 판정기(테스트 더블·스텁·Claude). 반환 (relation, billed)."""

    model: str

    def judge(self, name_a: str, name_b: str, host: str, country: str) -> tuple[str, bool]: ...


class StubRelationJudge:
    """dry_run·키 없음 — 네트워크 없이 항상 unknown(→ review). 절대 different 로 단정하지 않는다."""

    model = "stub"

    def judge(self, name_a: str, name_b: str, host: str, country: str) -> tuple[str, bool]:
        return "unknown", False


class ClaudeRelationJudge:
    """Claude(Haiku) 관계 판정 — 실패·파싱불가는 unknown(review). llm_judge.ClaudeJudge 와 같은 규율."""

    def __init__(self, api_key: str, *, model: str, max_tokens: int = 150) -> None:
        self._api_key = api_key
        self.model = model
        self._max_tokens = max_tokens
        self._client = None

    def judge(self, name_a: str, name_b: str, host: str, country: str) -> tuple[str, bool]:
        from ..llm import anthropic_client

        prompt = _REL_PROMPT.format(name_a=name_a, name_b=name_b, host=host, country=country)
        try:
            if self._client is None:
                self._client = anthropic_client(api_key=self._api_key)
            msg = self._client.messages.create(
                model=self.model, max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 — 미설치·키오류·API 오류 → unknown·미과금
            log.info("domain_conflict.llm_error", err=str(exc))
            return "unknown", False
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        start, end = text.find("{"), text.rfind("}")
        try:
            rel = str(json.loads(text[start:end + 1]).get("relation", "unknown")).strip().lower()
        except (ValueError, TypeError):
            rel = "unknown"
        return (rel if rel in _RELATIONS else "unknown"), True


def _evidence(row: dict, audited: dict[str, set[str]], titles: dict[str, str]) -> list[str]:
    host = row["host"]
    ev: list[str] = []
    key = row["canonical_key"]
    if key.startswith("dom:") and normalize_domain(key[4:]) == host:
        ev.append("dom_key")
    if host in audited.get(row["company_id"], ()):
        ev.append("audited_homepage")
    # 라틴 근거는 권위 있는 name_eng 또는 **전부 라틴인 주명칭**만 — 한글명 안의 괄호 병기
    # ('동일상사 (APPLE)')는 근거로 쓰지 않는다(Codex HIGH: 실제 소유주 역전 재현).
    name = row["name"]
    for nm in (row.get("name_eng"), name if not _CJK_RE.search(name) else None):
        slug = _name_slug(nm or "")
        if slug and _name_matches(slug, host):
            ev.append("latin_name")
            break
    title = titles.get(host)
    if title and _title_supports(name, title):
        ev.append("title")
    return ev


_REL_TO_ACTION = {"same": SAME_ENTITY, "affiliate": RELATED, "different": AUTO_WRONG, "unknown": REVIEW}


def classify_group(
    rows: list[dict], audited: dict[str, set[str]], titles: dict[str, str] | None = None,
    *, judge: RelationJudge | None = None, judge_budget: list[int] | None = None,
    ledger: object | None = None,
) -> list[ConflictRow]:
    """그룹 1개 판정. ``judge`` 가 있으면 규칙상 auto_wrong 인 행을 소유주와 LLM 관계 판정으로
    한 번 더 거른다(same→same_entity·affiliate→related·different→auto_wrong·unknown→review) —
    한글↔영문·음역 변형(에코프로비엠↔EcoPro BM)을 규칙이 못 가르기 때문. ``judge_budget``
    [남은 호출 수] 가 0 이면 판정 없이 review. ``ledger`` 가 있으면 과금 왕복만 record."""
    titles = titles or {}
    out = [
        ConflictRow(
            company_id=r["company_id"], canonical_key=r["canonical_key"], name=r["name"],
            country=r["country"], host=r["host"], reg_no=r.get("reg_no"),
            evidence=_evidence(r, audited, titles),
        )
        for r in rows
    ]
    owners = [c for c in out if set(c.evidence) & _STRONG]
    if not owners:
        for c in out:
            c.action = REVIEW
        return out
    owner_tokens = [compare_tokens(o.name) for o in owners]
    owner_keys = ["".join(t) for t in owner_tokens]
    # LLM 상대는 근거가 가장 강한 소유주 1곳(dom_key > latin_name > title).
    best_owner = max(owners, key=lambda o: (
        "dom_key" in o.evidence, "latin_name" in o.evidence, "title" in o.evidence,
    ))
    for c in out:
        if c in owners:
            c.action = ALLOW_SHARED if len(owners) >= 2 else OWNER
            continue
        mine = compare_tokens(c.name)
        mine_key = "".join(mine)
        if any(_name_score(mine, ot) >= NAME_STRONG for ot in owner_tokens):
            c.action = SAME_ENTITY  # 같은 회사 — dedup-report/merge 몫
        elif any(len(k) >= _TITLE_MIN and k in mine_key for k in owner_keys):
            # 소유주 이름을 품음(접두·중간 모두: 알테오젠바이오로직스·성남한국전력지점) — 지점·자회사
            # 표기는 손대지 않는다. 독립 법인을 과보호할 수 있으나 삭제 안 하는 방향의 오차(안전).
            c.action = RELATED
        elif c.canonical_key.startswith("reg:") or c.reg_no:
            c.action = REVIEW  # 법인 확정 행 — 소유주가 뒤집힌 경우가 있어 사람 판정
        elif judge is None:
            c.action = AUTO_WRONG
        elif judge_budget is not None and judge_budget[0] <= 0:
            c.action = REVIEW  # 캡·예산 소진 — 판정 없이 사람 위임(다른 회사로 단정하지 않음)
        else:
            if ledger is not None and getattr(judge, "model", "") != "stub" and ledger.is_over_budget():
                c.action = REVIEW
                continue
            rel, billed = judge.judge(c.name, best_owner.name, c.host, c.country)
            if judge_budget is not None:
                judge_budget[0] -= 1
            if ledger is not None and billed:
                ledger.record("dedup_llm")
            c.evidence.append(f"llm:{rel}")
            c.action = _REL_TO_ACTION.get(rel, REVIEW)
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
    judge: RelationJudge | None = None, judge_max: int = 200, ledger: object | None = None,
    now: Callable[[], datetime] | None = None,
) -> ConflictPlan:
    """그룹 조회 → 판정(+옵션 LLM 관계 게이트) → auto_wrong 스냅샷까지 담은 계획(읽기 전용)."""
    groups = load_conflict_groups(session)
    all_ids = [r["company_id"] for rows in groups.values() for r in rows]
    audited = audited_hosts(session, all_ids)
    titles = fetch_titles(sorted({h for _, h in groups}), get_text) if get_text is not None else {}
    budget = [judge_max]
    rows_out: list[ConflictRow] = []
    for rows in groups.values():
        raw_by_id = {r["company_id"]: r for r in rows}
        for c in classify_group(rows, audited, titles, judge=judge, judge_budget=budget, ledger=ledger):
            if c.action == AUTO_WRONG:
                c.before = _snapshot(session, c, raw_by_id[c.company_id])
            rows_out.append(c)
    by_action: dict[str, int] = defaultdict(int)
    for c in rows_out:
        by_action[c.action] += 1
    return ConflictPlan(
        generated_at=(now or (lambda: datetime.now(timezone.utc)))(),
        groups=len(groups), by_action=dict(by_action), rows=rows_out, titles_fetched=len(titles),
        llm_judged=judge_max - budget[0],
    )


def _parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def apply_row(session: Session, row: ConflictRow, *, now: datetime) -> str:
    """auto_wrong 1행 적용. 반환 'applied' | 'stale'(before 와 현재값 불일치 — 건너뜀) | 'skipped'."""
    from ..storage.review import candidate_values_of, review_id_for

    if row.action != AUTO_WRONG:
        return "skipped"
    co = session.get(CompanyRow, row.company_id, with_for_update=True)  # PG 행 잠금(SQLite 무시)
    if co is None or normalize_domain(co.homepage) != row.host:
        return "stale"  # 이미 바뀜(사람 수정·재해석) — 덮어쓰지 않는다.
    q = row.before.get("queue")
    rq0 = session.get(ReviewQueueRow, q["id"], with_for_update=True) if q else None
    if rq0 is not None and (
        rq0.status != q["status"] or rq0.assignee != q["assignee"]
        or (rq0.reviewed_at.isoformat() if rq0.reviewed_at else None) != q["reviewed_at"]
    ):
        return "stale"  # 계획 이후 사람이 큐를 다시 판정 — 그 판단을 덮지 않는다.
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
