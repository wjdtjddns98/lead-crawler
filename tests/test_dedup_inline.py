"""C5 inline 중복 승격 — 적재 시점 교차key 중복 링크(전부 오프라인·결정적).

핵심: 기존 정확 dedup 은 같은 도메인·다른 key 중복을 **미연결 행**으로 남긴다. C5 는
플래그 dedup_inline 으로 그런 교차key 중복을 적재 시점에 생존자로 링크(duplicate_of)한다.
플래그 off 면 기존 동작(미연결 touch) 그대로 — 회귀 0.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from leadcrawler.config import Settings
from leadcrawler.dedup_resolve.inline import find_inline_duplicate
from leadcrawler.schema import DiscoveredCompanyRow
from leadcrawler.sources.base import DiscoveredCompany, Segment
from leadcrawler.storage.db import init_db, session_scope


@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/inline.db", dry_run=True)
    init_db(settings)
    with session_scope(settings) as s:
        s.add(
            DiscoveredCompanyRow(
                canonical_key="dom:acme.com", name="Acme Corporation",
                country="KR", domain="acme.com",
            )
        )
        s.flush()
        yield s


def _dc(key, name, *, domain=None, country="KR", registry=None, registry_id=None) -> DiscoveredCompany:
    return DiscoveredCompany(
        canonical_key=key, name=name, country=country, domain=domain,
        registry=registry, registry_id=registry_id, source="test",
    )


# ── find_inline_duplicate 단위 ────────────────────────────────────────────────
def test_finds_auto_survivor_same_domain_and_name(session: Session) -> None:
    dc = _dc("reg:dart:1", "Acme Corp", domain="www.acme.com", registry="dart", registry_id="1")
    assert find_inline_duplicate(session, dc) == "dom:acme.com"  # 이름高+도메인root 일치=auto


def test_no_domain_yields_none(session: Session) -> None:
    assert find_inline_duplicate(session, _dc("name:kr:acme", "Acme Corporation")) is None


def test_different_domain_yields_none(session: Session) -> None:
    assert find_inline_duplicate(session, _dc("dom:other.com", "Acme Corporation", domain="other.com")) is None


def test_same_domain_but_unrelated_name_not_auto(session: Session) -> None:
    # 같은 도메인이지만 이름이 전혀 달라 auto(이름高) 미성립 → 링크 안 함(공유호스팅 방어).
    dc = _dc("reg:dart:9", "Zeta Mining Holdings", domain="acme.com", registry="dart", registry_id="9")
    assert find_inline_duplicate(session, dc) is None


def test_excludes_already_absorbed_survivor(session: Session) -> None:
    # 생존자 후보가 이미 흡수된 행이면 후보에서 제외(생존자만 링크 대상).
    session.add(DiscoveredCompanyRow(canonical_key="reg:root:1", name="Root", country="KR", domain="root.com"))
    row = session.get(DiscoveredCompanyRow, "dom:acme.com")
    row.duplicate_of = "reg:root:1"
    session.flush()
    dc = _dc("reg:dart:1", "Acme Corp", domain="acme.com", registry="dart", registry_id="1")
    assert find_inline_duplicate(session, dc) is None


# ── run_pipeline 통합: 교차key 링크 + 플래그 off 회귀 ─────────────────────────
def _two_segment_discover():
    """seg '건설'=기존(dom:acme.com), seg '제조'=교차key 같은도메인(reg:dart:1)."""

    def _fake(segment, settings, cost_ledger=None, *, sources=None):  # noqa: ARG001
        if segment.industry == "건설":
            return [_dc("dom:acme.com", "Acme Corporation", domain="acme.com")]
        return [_dc("reg:dart:1", "Acme Corp", domain="www.acme.com", registry="dart", registry_id="1")]

    return _fake


def _run(tmp_path, *, inline: bool, monkeypatch):
    import leadcrawler.pipeline.run as run_mod

    monkeypatch.setattr(run_mod, "discover_segment", _two_segment_discover())
    settings = Settings(database_url=f"sqlite:///{tmp_path}/run.db", dry_run=True, dedup_inline=inline)
    init_db(settings)
    from leadcrawler.pipeline import run_pipeline

    run_pipeline(
        [Segment(country="KR", industry="건설"), Segment(country="KR", industry="제조")],
        settings=settings, persist=True,
    )
    return settings


def test_inline_links_cross_key_duplicate(tmp_path, monkeypatch) -> None:
    settings = _run(tmp_path, inline=True, monkeypatch=monkeypatch)
    with session_scope(settings) as s:
        dup = s.get(DiscoveredCompanyRow, "reg:dart:1")
        assert dup is not None
        assert dup.duplicate_of == "dom:acme.com"  # 교차key 중복이 생존자로 링크됨
        assert dup.merged_by == "auto" and dup.merge_reason and "inline" in dup.merge_reason


def test_flag_off_leaves_duplicate_unlinked(tmp_path, monkeypatch) -> None:
    # 회귀 보장: 플래그 off 면 기존 동작 — 교차key 중복은 미연결 행으로 남는다.
    settings = _run(tmp_path, inline=False, monkeypatch=monkeypatch)
    with session_scope(settings) as s:
        dup = s.get(DiscoveredCompanyRow, "reg:dart:1")
        assert dup is not None and dup.duplicate_of is None  # 미연결(기존 동작)
