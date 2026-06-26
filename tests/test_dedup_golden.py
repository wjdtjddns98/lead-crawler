"""C3 골든레코드 survivorship — 생존자/캐노니컬 선정·클러스터링·가역 머지(전부 오프라인)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from leadcrawler.config import Settings
from leadcrawler.dedup_resolve.golden import (
    ClusterMember,
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
