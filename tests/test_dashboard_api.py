"""보유 데이터 대시보드 집계 API — /dashboard/summary (in-process, 네트워크 0)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from leadcrawler.config import get_settings  # noqa: E402
from leadcrawler.schema import (  # noqa: E402
    CompanyRow,
    ContactRow,
    DiscoveredCompanyRow,
)
from leadcrawler.security import create_user  # noqa: E402
from leadcrawler.storage.db import init_db, session_scope  # noqa: E402
from leadcrawler.storage.repository import company_id_for  # noqa: E402
from leadcrawler.storage.review import enqueue_email_review  # noqa: E402

_PW = "s3cret-pw-123"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/dash.db")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as s:
        create_user(s, "관리자", _PW)
        # 원장 3분면: 승격 1 / 도메인 있음·미승격 1 / 도메인 없음·미승격 1.
        s.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:alive.co.kr", name="승격사", country="대한민국",
            industry="증권·자산운용", source="import", domain="alive.co.kr",
        ))
        s.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:wait.co.kr", name="대기사", country="KR",
            industry="게임", source="nps", domain="wait.co.kr",
        ))
        s.add(DiscoveredCompanyRow(
            canonical_key="name:kr:무도메인", name="무도메인", country="KR",
            industry="", source="import",
        ))
        s.flush()
        cid = company_id_for("dom:kr:alive.co.kr")
        s.add(CompanyRow(
            id=cid, canonical_key="dom:kr:alive.co.kr", name="승격사",
            country="대한민국", industry="증권·자산운용", site_alive=True,
        ))
        s.add(ContactRow(id="ct-dash-1", company_id=cid, type="email", value="ir@alive.co.kr"))
        s.flush()
        enqueue_email_review(s, cid, ["ir@alive.co.kr"])
    from leadcrawler.api.app import create_app

    yield create_app()
    get_settings.cache_clear()


def test_dashboard_summary_shape_and_folding(app) -> None:
    """원장 3분면 합=total, 국가 ISO2 접기('대한민국'→KR), 빈 업종='미분류', 큐 분해."""
    c = TestClient(app)
    r = c.post("/auth/login", json={"username": "관리자", "password": _PW})
    assert r.status_code == 200
    c.headers.update({"Authorization": f"Bearer {r.json()['token']}"})

    body = c.get("/dashboard/summary").json()
    ledger = body["ledger"]
    assert ledger == {
        "total": 3, "promoted": 1,
        "domained_unpromoted": 1, "undomained_unpromoted": 1,
    }
    comp = body["companies"]
    assert comp["total"] == 1 and comp["with_email"] == 1
    assert comp["by_country"] == [{"country": "KR", "n": 1}]  # '대한민국'→ISO2 접기.
    assert comp["by_industry"] == [{"industry": "증권·자산운용", "n": 1}]
    q = body["queue"]
    assert q["pending_unclaimed"] == 1 and q["pending_claimed"] == 0
    assert q["confirmed"] == 0 and q["rejected"] == 0


def test_dashboard_requires_auth(app) -> None:
    assert TestClient(app).get("/dashboard/summary").status_code in (401, 403)
