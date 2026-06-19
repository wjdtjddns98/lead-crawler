"""파이프라인 영속화 + DB 기반 dedup 시드 테스트(제약 ①·②)."""

from __future__ import annotations

from sqlalchemy import select

from leadcrawler.config import Settings
from leadcrawler.pipeline import run_pipeline
from leadcrawler.schema import CompanyRow
from leadcrawler.sources.base import Segment
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.repository import load_seen_keys


def test_persist_writes_ledger_and_companies(tmp_path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/p.db", dry_run=True)
    init_db(settings)
    leads = run_pipeline(
        [Segment(country="KR", industry="건설")], settings=settings, persist=True
    )
    assert leads
    with session_scope(settings) as s:
        # dry_run 은 전부 active → 원장·회사 수가 리드 수와 일치.
        assert len(load_seen_keys(s)) == len(leads)
        assert len(s.scalars(select(CompanyRow)).all()) == len(leads)


def test_persist_dedup_seed_skips_on_rerun(tmp_path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/p2.db", dry_run=True)
    init_db(settings)
    seg = Segment(country="KR", industry="건설")
    first = run_pipeline([seg], settings=settings, persist=True)
    assert first
    # 같은 세그먼트 재실행 — DB 원장이 시드가 되어 전부 dedup(제약 ①).
    second = run_pipeline([seg], settings=settings, persist=True)
    assert second == []
