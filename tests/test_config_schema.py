"""설정 기본값 + DB 스키마 생성 테스트."""

from __future__ import annotations

from sqlalchemy import create_engine

from leadcrawler.config import get_settings
from leadcrawler.schema import Base


def test_dry_run_default_true() -> None:
    assert get_settings().dry_run is True


def test_schema_create_all_on_sqlite() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    names = set(Base.metadata.tables)
    assert {"discovered_company", "company", "contact", "email_validation"} <= names
