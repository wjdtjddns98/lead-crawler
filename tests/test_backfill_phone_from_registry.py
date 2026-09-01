"""등록처 대표전화 → contact(phone) 일회성 백필 스크립트 테스트."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import select

from leadcrawler.config import Settings
from leadcrawler.schema import CompanyRow, ContactRow, DiscoveredCompanyRow
from leadcrawler.storage.db import get_sessionmaker, init_db
from leadcrawler.storage.repository import company_id_for

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_phone_from_registry.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_phone_from_registry", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _company(session, key: str, phone: str | None, *, existing_phone: str | None = None):
    session.add(DiscoveredCompanyRow(
        canonical_key=key, name=key, country="KR", industry="게임", source="nps", phone=phone,
    ))
    session.flush()  # company.canonical_key FK → 발견행 먼저(관계 미선언이라 flush 순서 보장 없음).
    cid = company_id_for(key)
    session.add(CompanyRow(id=cid, canonical_key=key, name=key, country="KR", industry="게임"))
    if existing_phone:
        session.add(ContactRow(id=f"c_{cid[:20]}", company_id=cid, type="phone",
                               value=existing_phone))
    return cid


def test_backfill_only_companies_without_phone(tmp_path) -> None:
    mod = _load()
    s = Settings(database_url=f"sqlite:///{tmp_path}/bp.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)
    with sm() as session:
        target = _company(session, "name:kr:대상", "02-3011-2518")
        _company(session, "name:kr:이미있음", "02-1111-1111", existing_phone="02-9999-9999")
        _company(session, "name:kr:등록처없음", None)
        _company(session, "name:kr:빈문자열", "")
        session.commit()

        # 리포트만 — 무변경.
        rows = mod.backfill(session, apply=False)
        assert [(r[0], r[2]) for r in rows] == [(target, "02-3011-2518")]
        assert session.scalar(select(ContactRow).where(ContactRow.company_id == target)) is None

        # 적용 — 파이프라인 폴백과 같은 표기(api·0.9). 재실행은 대상 0(멱등).
        mod.backfill(session, apply=True)
        row = session.scalar(select(ContactRow).where(ContactRow.company_id == target))
        assert (row.value, row.extract_method, row.confidence) == ("02-3011-2518", "api", 0.9)
        assert mod.backfill(session, apply=True) == []
        assert session.scalar(
            select(ContactRow.value).where(ContactRow.company_id == company_id_for("name:kr:이미있음"))
        ) == "02-9999-9999"
