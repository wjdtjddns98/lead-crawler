"""dedup 배치 리포트 — DB 적재→리포트→JSON 저장 end-to-end(SQLite)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from leadcrawler.config import Settings
from leadcrawler.dedup_resolve.report import load_company_records, run_dedup_report
from leadcrawler.schema import DiscoveredCompanyRow
from leadcrawler.storage.db import init_db, session_scope


@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    """격리된 파일 SQLite 세션(FK 강제 ON)."""
    settings = Settings(database_url=f"sqlite:///{tmp_path}/dedup.db", dry_run=True)
    init_db(settings)
    with session_scope(settings) as s:
        yield s


def _seed(s: Session) -> None:
    s.add_all(
        [
            DiscoveredCompanyRow(
                canonical_key="dom:acme.com", name="Acme Corporation",
                country="KR", domain="acme.com",
            ),
            DiscoveredCompanyRow(
                canonical_key="name:kr:acme", name="Acme Inc",
                country="KR", domain="www.acme.com",
            ),
            DiscoveredCompanyRow(
                canonical_key="dom:zeta.com", name="Zeta Mining",
                country="KR", domain="zeta.com",
            ),
        ]
    )
    s.flush()


def test_report_finds_auto_duplicate(session: Session, tmp_path) -> None:
    _seed(session)
    out = tmp_path / "report.json"
    rpt = run_dedup_report(session, out)
    assert rpt.total_records == 3
    assert rpt.total_candidates == 1
    assert rpt.auto_removable == 1
    assert rpt.keep_both == 0
    assert rpt.skipped_blocks == []
    assert rpt.by_tier.get("auto") == 1
    # JSON 파일이 결정적으로 저장됨.
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["total_candidates"] == 1
    assert data["candidates"][0]["tier"] == "auto"
    assert {data["candidates"][0]["key_a"], data["candidates"][0]["key_b"]} == {
        "dom:acme.com", "name:kr:acme",
    }


def test_load_excludes_merged_by_default(session: Session, tmp_path) -> None:
    _seed(session)
    # name:kr:acme 를 머지 처리된 행으로 표시 → 기본 제외.
    row = session.get(DiscoveredCompanyRow, "name:kr:acme")
    assert row is not None
    row.duplicate_of = "dom:acme.com"
    session.flush()

    assert len(load_company_records(session)) == 2  # 머지된 행 제외
    assert len(load_company_records(session, include_merged=True)) == 3

    rpt = run_dedup_report(session, tmp_path / "r2.json")
    assert rpt.total_records == 2
    assert rpt.total_candidates == 0  # 짝이 제외돼 후보 사라짐

    rpt_all = run_dedup_report(session, tmp_path / "r3.json", include_merged=True)
    assert rpt_all.total_records == 3
    assert rpt_all.auto_removable == 1


def test_empty_db_yields_empty_report(session: Session, tmp_path) -> None:
    rpt = run_dedup_report(session, tmp_path / "empty.json")
    assert rpt.total_records == 0
    assert rpt.total_candidates == 0
    assert rpt.by_tier == {}


def test_report_with_stub_judge_populates_verdicts(session: Session, tmp_path) -> None:
    # 도메인 다른 동명 쌍 → C1 은 keep_both(판정 제외), 도메인 불명 쌍 → lexical(판정 대상).
    s = session
    s.add_all(
        [
            DiscoveredCompanyRow(canonical_key="reg:kr:1", name="Orion Tech", country="KR", domain=None),
            DiscoveredCompanyRow(canonical_key="reg:kr:2", name="Orion Tech", country="KR", domain=None),
        ]
    )
    s.flush()
    from leadcrawler.dedup_resolve.llm_judge import StubJudge

    rpt = run_dedup_report(s, tmp_path / "judged.json", judge=StubJudge())
    # 이름高·도메인불명 → lexical 쇼트리스트 1쌍이 스텁 판정됨(도메인 불명이라 미확정).
    assert rpt.llm_judged_count == 1
    assert len(rpt.judged) == 1
    assert rpt.judged[0].candidate.tier == "lexical"
    assert rpt.judged[0].verdict.same is False  # 스텁: 도메인 불명 → 사람 위임
