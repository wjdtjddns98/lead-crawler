"""C3 골든레코드 survivorship — 생존자/캐노니컬 선정·클러스터링·가역 머지(전부 오프라인)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from leadcrawler.config import Settings
from leadcrawler.dedup_resolve.golden import (
    ClusterMember,
    GoldenRecord,
    apply_golden,
    build_clusters,
    load_cluster_members,
    resolve_all,
    resolve_golden,
)
from leadcrawler.schema import DiscoveredCompanyRow
from leadcrawler.storage.db import init_db, session_scope


def _m(key, name, *, domain=None, registry=None, registry_id=None, country="KR") -> ClusterMember:
    return ClusterMember(
        key=key, name=name, country=country, domain=domain,
        registry=registry, registry_id=registry_id,
    )


# ── build_clusters: 이행적 병합 + 결정적 정렬 ──────────────────────────────────
def test_clusters_merge_transitively() -> None:
    clusters = build_clusters([("a", "b"), ("b", "c"), ("x", "y")])
    assert {frozenset(c) for c in clusters} == {frozenset({"a", "b", "c"}), frozenset({"x", "y"})}
    # 결정적: 최소 key 기준 정렬 → 첫 클러스터는 a 포함.
    assert min(clusters[0]) == "a"


def test_empty_pairs_yield_no_clusters() -> None:
    assert build_clusters([]) == []


# ── 생존자 선정: 등록처 > 도메인 > 토큰수 > key ────────────────────────────────
def test_registry_member_survives() -> None:
    g = resolve_golden([
        _m("dom:acme.com", "Acme", domain="acme.com"),
        _m("reg:dart:123", "에이스산업 주식회사", registry="dart", registry_id="123"),
    ])
    assert g.survivor_key == "reg:dart:123"  # 등록처 보유가 생존
    assert g.absorbed_keys == ["dom:acme.com"]


def test_domain_beats_nameonly() -> None:
    g = resolve_golden([
        _m("name:kr:acme", "Acme Inc"),
        _m("dom:acme.com", "Acme", domain="acme.com"),
    ])
    assert g.survivor_key == "dom:acme.com"


def test_survivor_tiebreak_is_deterministic_by_key() -> None:
    # 동급(둘 다 도메인만, 토큰수 동일) → key 사전순 작은 쪽 생존.
    g = resolve_golden([
        _m("dom:z.com", "Acme", domain="z.com"),
        _m("dom:a.com", "Acme", domain="a.com"),
    ])
    assert g.survivor_key == "dom:a.com"


# ── 캐노니컬명: 등록처(법인명) 우선 ────────────────────────────────────────────
def test_canonical_name_prefers_registry_legal_name() -> None:
    g = resolve_golden([
        _m("dom:acme.com", "Acme", domain="acme.com"),
        _m("reg:dart:1", "에이스산업 주식회사", registry="dart", registry_id="1"),
    ])
    assert g.canonical_name == "에이스산업 주식회사"


def test_canonical_domain_prefers_authoritative_source() -> None:
    g = resolve_golden([
        _m("dom:old.com", "Acme", domain="old.com"),
        _m("reg:dart:1", "Acme Corp", registry="dart", registry_id="1", domain="acme.co.kr"),
    ])
    assert g.canonical_domain == "acme.co.kr"  # 등록처 멤버 도메인 우선


# ── 단일/빈 클러스터 방어 ──────────────────────────────────────────────────────
def test_single_member_has_no_absorbed() -> None:
    g = resolve_golden([_m("dom:a.com", "A", domain="a.com")])
    assert g.absorbed_keys == [] and "단일" in g.reason


def test_empty_cluster_raises() -> None:
    with pytest.raises(ValueError):
        resolve_golden([])


# ── DB 적용(SQLite) — 가역 머지 + idempotent ──────────────────────────────────
@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/golden.db", dry_run=True)
    init_db(settings)
    with session_scope(settings) as s:
        yield s


def test_apply_golden_writes_merge_audit(session: Session) -> None:
    session.add_all([
        DiscoveredCompanyRow(canonical_key="reg:dart:1", name="에이스 주식회사",
                             country="KR", registry="dart", registry_id="1"),
        DiscoveredCompanyRow(canonical_key="dom:acme.com", name="Acme", country="KR", domain="acme.com"),
    ])
    session.flush()
    members = load_cluster_members(session, ["reg:dart:1", "dom:acme.com"])
    goldens = resolve_all(members, [("reg:dart:1", "dom:acme.com")])
    assert len(goldens) == 1

    fixed = datetime(2026, 6, 26, tzinfo=timezone.utc)
    absorbed = apply_golden(session, goldens[0], merged_by="auto", now=lambda: fixed)
    session.flush()
    assert absorbed == 1

    survivor = session.get(DiscoveredCompanyRow, "reg:dart:1")
    dup = session.get(DiscoveredCompanyRow, "dom:acme.com")
    assert survivor.canonical_name == "에이스 주식회사"
    assert survivor.domain == "acme.com"  # 생존자 도메인 비어 있어 권위 도메인 채움
    assert dup.duplicate_of == "reg:dart:1"
    # SQLite 는 tzinfo 를 떼고 저장(PG 는 유지) — naive 로 보정해 동일 시각인지만 본다.
    assert dup.merged_by == "auto"
    assert dup.merged_at.replace(tzinfo=timezone.utc) == fixed

    # idempotent — 재적용해도 이미 머지된 행은 다시 흡수하지 않음.
    assert apply_golden(session, goldens[0], merged_by="auto", now=lambda: fixed) == 0


def test_resolve_all_skips_broken_cluster(session: Session) -> None:
    # pair 에 등장하지만 원장에 없는 key → 멤버 2 미만이면 머지 안 함(제약② 보수).
    session.add(DiscoveredCompanyRow(canonical_key="dom:a.com", name="A", country="KR", domain="a.com"))
    session.flush()
    members = load_cluster_members(session, ["dom:a.com", "dom:gone.com"])
    goldens = resolve_all(members, [("dom:a.com", "dom:gone.com")])
    assert goldens == []  # 한쪽이 사라져 클러스터 깨짐 → 머지 없음


def test_bridge_member_loss_does_not_merge_unrelated(session: Session) -> None:
    # 허브 C 로만 연결된 A·B (직접 (A,B) 쌍 없음). C 가 원장에 없으면 A·B 는
    # 직접 중복근거가 없으므로 합쳐지면 안 된다(제약② — 리뷰어 재현 MEDIUM 회귀).
    session.add_all([
        DiscoveredCompanyRow(canonical_key="dom:alpha.com", name="Alpha", country="KR", domain="alpha.com"),
        DiscoveredCompanyRow(canonical_key="dom:beta.com", name="Beta", country="KR", domain="beta.com"),
    ])
    session.flush()
    members = load_cluster_members(session, ["dom:alpha.com", "dom:beta.com", "dom:hub.com"])
    goldens = resolve_all(members, [("dom:alpha.com", "dom:hub.com"), ("dom:beta.com", "dom:hub.com")])
    assert goldens == []  # 허브 소실 → 컴포넌트 분리 → 머지 없음


def test_cross_run_chain_is_flattened(session: Session) -> None:
    # 리뷰어 재현 HIGH: 2회 실행에 걸친 체인. b→a 머지 후, 더 권위 높은 reg 가 a 를 흡수하면
    # b 가 a(비-root)를 가리켜 고아가 되면 안 됨 → 자식 재지정으로 평탄화돼야 한다.
    session.add_all([
        DiscoveredCompanyRow(canonical_key="dom:a.com", name="Acme", country="KR", domain="a.com"),
        DiscoveredCompanyRow(canonical_key="dom:b.com", name="Acme", country="KR", domain="a.com"),
        DiscoveredCompanyRow(canonical_key="reg:dart:9", name="에이스 주식회사",
                             country="KR", registry="dart", registry_id="9", domain="a.com"),
    ])
    session.flush()
    fixed = datetime(2026, 6, 26, tzinfo=timezone.utc)

    # Run 1: (a,b) → 둘 다 도메인 a.com → survivor=a(key 사전순), b 흡수.
    m1 = load_cluster_members(session, ["dom:a.com", "dom:b.com"])
    g1 = resolve_all(m1, [("dom:a.com", "dom:b.com")])
    for g in g1:
        apply_golden(session, g, now=lambda: fixed)
    session.flush()
    assert session.get(DiscoveredCompanyRow, "dom:b.com").duplicate_of == "dom:a.com"

    # Run 2: (a, reg) → reg 가 등록처 보유로 생존, a 흡수. a 의 자식 b 는 reg 로 재지정돼야.
    m2 = load_cluster_members(session, ["dom:a.com", "reg:dart:9"])
    g2 = resolve_all(m2, [("dom:a.com", "reg:dart:9")])
    for g in g2:
        apply_golden(session, g, now=lambda: fixed)
    session.commit()

    a = session.get(DiscoveredCompanyRow, "dom:a.com")
    b = session.get(DiscoveredCompanyRow, "dom:b.com")
    assert a.duplicate_of == "reg:dart:9"
    assert b.duplicate_of == "reg:dart:9"  # 평탄화 — 고아(b→a) 아님
    # root 로 단일 조인하면 두 자식 모두 잡힘(불변 회복).
    children = session.scalars(
        select(DiscoveredCompanyRow.canonical_key).where(
            DiscoveredCompanyRow.duplicate_of == "reg:dart:9"
        )
    ).all()
    assert set(children) == {"dom:a.com", "dom:b.com"}


def test_survivor_already_absorbed_is_refused(session: Session) -> None:
    # 생존 후보가 이미 흡수된 비-root 면 머지 거부(체인 방지·제약② 보수).
    session.add_all([
        DiscoveredCompanyRow(canonical_key="root", name="R", country="KR", domain="r.com"),
        DiscoveredCompanyRow(canonical_key="dom:a.com", name="Acme", country="KR",
                             domain="a.com", duplicate_of="root"),  # 이미 흡수됨
        DiscoveredCompanyRow(canonical_key="name:kr:acme", name="Acme Inc", country="KR"),
    ])
    session.flush()
    # a(도메인 보유) 가 생존 후보가 되지만 이미 흡수됨 → 거부.
    g = GoldenRecord(
        survivor_key="dom:a.com", canonical_name="Acme", canonical_domain="a.com",
        country="KR", absorbed_keys=["name:kr:acme"], reason="t",
    )
    assert apply_golden(session, g) == 0
    assert session.get(DiscoveredCompanyRow, "name:kr:acme").duplicate_of is None


# ── company 본체 병합(큐·엑셀 중복 해소) ────────────────────────────────────────
def _promote(session: Session, key: str, cid: str, *, emails: list[str], homepage: str | None = None) -> None:
    from leadcrawler.schema import CompanyRow, ContactRow, EmailValidationRow
    from leadcrawler.storage.repository import contact_id_for
    from leadcrawler.storage.review import enqueue_email_review

    session.add(CompanyRow(id=cid, canonical_key=key, name=key, country="KR", homepage=homepage))
    session.flush()
    for e in emails:
        k = contact_id_for(cid, "email", e)
        session.add(ContactRow(id=k, company_id=cid, type="email", value=e, role="ir"))
        session.flush()
        session.add(EmailValidationRow(contact_id=k, status="valid", mx=True))
    enqueue_email_review(session, cid, emails)
    session.flush()


def _merge_pair(session: Session, fixed: datetime) -> None:
    members = load_cluster_members(session, ["reg:dart:1", "dom:acme.com"])
    goldens = resolve_all(members, [("reg:dart:1", "dom:acme.com")])
    assert apply_golden(session, goldens[0], merged_by="auto", now=lambda: fixed) == 1
    session.flush()


def test_apply_golden_repoints_company_when_survivor_unpromoted(session: Session) -> None:
    """생존자 쪽 company 가 없으면 흡수행 company 의 키만 생존자로 이관(본체·큐 보존)."""
    from leadcrawler.schema import CompanyRow
    from leadcrawler.storage.review import ReviewQueueRow

    session.add_all([
        DiscoveredCompanyRow(canonical_key="reg:dart:1", name="에이스 주식회사", country="KR",
                             registry="dart", registry_id="1"),
        DiscoveredCompanyRow(canonical_key="dom:acme.com", name="에이스", country="KR", domain="acme.com"),
    ])
    session.flush()
    _promote(session, "dom:acme.com", "c_dom", emails=["ir@acme.com"])
    _merge_pair(session, datetime(2026, 9, 16, tzinfo=timezone.utc))

    co = session.get(CompanyRow, "c_dom")
    assert co is not None and co.canonical_key == "reg:dart:1"
    assert session.query(CompanyRow).count() == 1
    assert session.query(ReviewQueueRow).filter_by(company_id="c_dom").count() == 1


def test_apply_golden_merges_promoted_companies_and_inherits_confirmed(session: Session) -> None:
    """둘 다 승격됐으면 연락처 합집합·확정 판단 승계·감사 이력 재지정 후 흡수 company 삭제."""
    from leadcrawler.schema import CompanyRow, ContactRow, EmailValidationRow, ReviewAuditRow
    from leadcrawler.storage.review import ReviewQueueRow, candidate_values_of, review_id_for

    session.add_all([
        DiscoveredCompanyRow(canonical_key="reg:dart:1", name="에이스 주식회사", country="KR",
                             registry="dart", registry_id="1", domain="acme.com"),
        DiscoveredCompanyRow(canonical_key="dom:acme.com", name="에이스", country="KR", domain="acme.com"),
    ])
    session.flush()
    _promote(session, "reg:dart:1", "c_reg", emails=["ir@acme.com"], homepage=None)
    _promote(session, "dom:acme.com", "c_dom", emails=["ir@acme.com", "contact@acme.com"],
             homepage="https://acme.com")
    # 흡수될 쪽만 사람이 확정 + 감사 이력 1건.
    arq = session.get(ReviewQueueRow, review_id_for("c_dom", "email"))
    arq.status, arq.assignee, arq.selected, arq.selected_by_human = "confirmed", "kim", "contact@acme.com", True
    session.add(ReviewAuditRow(id="a1", review_id=arq.id, actor_username="kim", action="confirmed"))
    session.flush()

    _merge_pair(session, datetime(2026, 9, 16, tzinfo=timezone.utc))

    assert session.get(CompanyRow, "c_dom") is None
    surv = session.get(CompanyRow, "c_reg")
    assert surv.homepage == "https://acme.com"  # 공란만 채움
    values = sorted(c.value for c in session.query(ContactRow).filter_by(company_id="c_reg"))
    assert values == ["contact@acme.com", "ir@acme.com"]  # 중복 없이 합집합
    assert session.query(ContactRow).filter_by(company_id="c_dom").count() == 0
    assert session.query(EmailValidationRow).count() == 2  # 복제 1 + 흡수분 삭제
    srq = session.get(ReviewQueueRow, review_id_for("c_reg", "email"))
    assert srq.status == "confirmed" and srq.assignee == "kim" and srq.selected == "contact@acme.com"
    assert sorted(candidate_values_of(srq)) == ["contact@acme.com", "ir@acme.com"]
    assert session.query(ReviewQueueRow).count() == 1
    assert session.get(ReviewAuditRow, "a1").review_id == srq.id  # 이력 보존


def test_apply_golden_keeps_survivor_confirmation_when_both_confirmed(session: Session) -> None:
    from leadcrawler.storage.review import ReviewQueueRow, review_id_for

    session.add_all([
        DiscoveredCompanyRow(canonical_key="reg:dart:1", name="에이스", country="KR",
                             registry="dart", registry_id="1", domain="acme.com"),
        DiscoveredCompanyRow(canonical_key="dom:acme.com", name="에이스", country="KR", domain="acme.com"),
    ])
    session.flush()
    _promote(session, "reg:dart:1", "c_reg", emails=["ir@acme.com"])
    _promote(session, "dom:acme.com", "c_dom", emails=["ir@acme.com"])
    for cid, who in (("c_reg", "lee"), ("c_dom", "kim")):
        rq = session.get(ReviewQueueRow, review_id_for(cid, "email"))
        rq.status, rq.assignee = "confirmed", who
    session.flush()
    _merge_pair(session, datetime(2026, 9, 16, tzinfo=timezone.utc))
    srq = session.get(ReviewQueueRow, review_id_for("c_reg", "email"))
    assert srq.status == "confirmed" and srq.assignee == "lee"


def test_apply_golden_does_not_revive_rejected_candidates(session: Session) -> None:
    """흡수행이 사람 거부(rejected)면 그 이메일 후보는 생존자 큐에 되살리지 않는다(연락처는 복제)."""
    from leadcrawler.schema import ContactRow
    from leadcrawler.storage.review import ReviewQueueRow, candidate_values_of, review_id_for

    session.add_all([
        DiscoveredCompanyRow(canonical_key="reg:dart:1", name="에이스", country="KR",
                             registry="dart", registry_id="1", domain="acme.com"),
        DiscoveredCompanyRow(canonical_key="dom:acme.com", name="에이스", country="KR", domain="acme.com"),
    ])
    session.flush()
    _promote(session, "reg:dart:1", "c_reg", emails=["ir@acme.com"])
    _promote(session, "dom:acme.com", "c_dom", emails=["IR@acme.com", "bad@acme.com"])
    arq = session.get(ReviewQueueRow, review_id_for("c_dom", "email"))
    arq.status = "rejected"
    session.flush()
    _merge_pair(session, datetime(2026, 9, 16, tzinfo=timezone.utc))
    srq = session.get(ReviewQueueRow, review_id_for("c_reg", "email"))
    assert srq.status == "pending" and candidate_values_of(srq) == ["ir@acme.com"]
    # 연락처는 대소문자 무시 합집합(IR@ 는 ir@ 와 같은 값).
    assert sorted(c.value for c in session.query(ContactRow).filter_by(company_id="c_reg")) == [
        "bad@acme.com", "ir@acme.com",
    ]


def test_apply_golden_rechain_merges_legacy_child_company(session: Session) -> None:
    """옛 머지(원장만 링크)로 흡수됐던 자식의 company 도 새 생존자로 합쳐진다(고아 방지)."""
    from leadcrawler.schema import CompanyRow, ContactRow

    session.add_all([
        DiscoveredCompanyRow(canonical_key="reg:dart:9", name="에이스 주식회사", country="KR",
                             registry="dart", registry_id="9", domain="acme.com"),
        DiscoveredCompanyRow(canonical_key="dom:acme.com", name="에이스", country="KR", domain="acme.com"),
        # 종전 방식으로 dom:acme.com 에 흡수됐지만 company 는 남아 있는 자식.
        DiscoveredCompanyRow(canonical_key="name:kr:에이스", name="(주)에이스", country="KR",
                             domain="acme.com", duplicate_of="dom:acme.com"),
    ])
    session.flush()
    _promote(session, "reg:dart:9", "c_reg", emails=["ir@acme.com"])
    _promote(session, "name:kr:에이스", "c_child", emails=["contact@acme.com"])
    members = load_cluster_members(session, ["reg:dart:9", "dom:acme.com"])
    goldens = resolve_all(members, [("reg:dart:9", "dom:acme.com")])
    assert apply_golden(session, goldens[0], now=lambda: datetime(2026, 9, 16, tzinfo=timezone.utc)) == 1
    session.flush()
    assert session.get(DiscoveredCompanyRow, "name:kr:에이스").duplicate_of == "reg:dart:9"
    assert session.get(CompanyRow, "c_child") is None
    assert {c.value for c in session.query(ContactRow).filter_by(company_id="c_reg")} == {
        "ir@acme.com", "contact@acme.com",
    }
