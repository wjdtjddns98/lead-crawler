"""설정 기본값 + DB 스키마 생성 테스트."""

from __future__ import annotations

from sqlalchemy import create_engine

from leadcrawler.config import get_settings
from leadcrawler.schema import Base


def test_dry_run_default_true() -> None:
    assert get_settings().dry_run is True


def test_discovery_user_agent_has_contact_url() -> None:
    # Wikidata WDQS 는 연락처(URL) 없는 UA 를 403 거부 — 기본 UA 가 회귀로 비준수
    # 형태로 되돌아가지 않도록 보호(2026-06-19 실연동 확인).
    ua = get_settings().discovery_user_agent
    assert "http" in ua.lower(), f"discovery_user_agent 에 연락처 URL 필요: {ua!r}"


def test_schema_create_all_on_sqlite() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    names = set(Base.metadata.tables)
    assert {"discovered_company", "company", "contact", "email_validation"} <= names
