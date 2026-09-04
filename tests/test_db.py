"""DB 엔진·세션 관리 테스트(SQLite, 네트워크 없음)."""

from __future__ import annotations

import pytest

from leadcrawler.config import Settings
from leadcrawler.schema import DiscoveredCompanyRow
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.repository import load_seen_keys


def test_init_db_and_session_commit(tmp_path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db", dry_run=True)
    init_db(settings)
    with session_scope(settings) as s:
        s.add(DiscoveredCompanyRow(canonical_key="dom:a.com", name="A"))
    # 별도 세션에서 커밋이 반영됐는지 확인.
    with session_scope(settings) as s:
        assert load_seen_keys(s) == {"dom:a.com"}


def test_session_scope_rolls_back_on_error(tmp_path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/t2.db", dry_run=True)
    init_db(settings)
    with pytest.raises(RuntimeError), session_scope(settings) as s:
        s.add(DiscoveredCompanyRow(canonical_key="dom:b.com", name="B"))
        raise RuntimeError("boom")
    with session_scope(settings) as s:
        assert load_seen_keys(s) == set()
