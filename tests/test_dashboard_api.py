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
        admin = create_user(s, "관리자", _PW)
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
        # dedup 흡수행 — 승격 백로그가 아니라 absorbed 로만 집계돼야 한다(리뷰 HIGH).
        s.add(DiscoveredCompanyRow(
            canonical_key="name:kr:흡수됨", name="흡수됨", country="KR",
            industry="게임", source="import", domain="wait.co.kr",
            duplicate_of="dom:kr:wait.co.kr",
        ))
        s.flush()
        cid = company_id_for("dom:kr:alive.co.kr")
        s.add(CompanyRow(
            id=cid, canonical_key="dom:kr:alive.co.kr", name="승격사",
            country="대한민국", industry="증권·자산운용", site_alive=True,
        ))
        # 국가 접기 병합('대한민국'+'KR'→KR 합산)·빈 업종 '미분류' 접기용 두 번째 회사.
        cid2 = company_id_for("dom:kr:second.co.kr")
        s.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:second.co.kr", name="둘째사", country="KR",
            industry="", source="nps", domain="second.co.kr",
        ))
        s.flush()
        s.add(CompanyRow(
            id=cid2, canonical_key="dom:kr:second.co.kr", name="둘째사",
            country="KR", industry="", site_alive=True,
        ))
        # 다중 이메일에도 with_email 은 회사 단위 distinct(부풀림 금지 회귀 가드).
        s.add(ContactRow(id="ct-dash-1", company_id=cid, type="email", value="ir@alive.co.kr"))
        s.add(ContactRow(id="ct-dash-2", company_id=cid, type="email", value="pr@alive.co.kr"))
        s.flush()
        enqueue_email_review(s, cid, ["ir@alive.co.kr"])
        rid2 = enqueue_email_review(s, cid2, ["x@second.co.kr"])
        # 점유 분해(pending_claimed) 커버 — 점유자만 표시.
        from leadcrawler.schema import ReviewQueueRow

        s.get(ReviewQueueRow, rid2).claimed_by = admin.id  # FK: user 테이블 참조.
    from leadcrawler.api.app import create_app

    yield create_app()
    get_settings.cache_clear()


def test_dashboard_summary_shape_and_folding(app) -> None:
    """원장 4분면 합=total(흡수행 분리), 국가 접기 병합, 빈 업종='미분류', 큐 점유 분해."""
    c = TestClient(app)
    r = c.post("/auth/login", json={"username": "관리자", "password": _PW})
    assert r.status_code == 200
    c.headers.update({"Authorization": f"Bearer {r.json()['token']}"})

    body = c.get("/dashboard/summary").json()
    ledger = body["ledger"]
    assert ledger == {
        "total": 5, "promoted": 2, "domained_unpromoted": 1,
        "undomained_unpromoted": 1, "absorbed": 1,  # 흡수행은 백로그 밖(4분면 계약).
    }
    comp = body["companies"]
    assert comp["total"] == 2 and comp["with_email"] == 1  # 다중 이메일도 distinct 1.
    assert comp["by_country"] == [{"country": "KR", "n": 2}]  # '대한민국'+'KR' 병합.
    assert comp["by_industry"] == [
        {"industry": "미분류", "n": 1},  # 빈 업종 접기(동률은 라벨 오름차순).
        {"industry": "증권·자산운용", "n": 1},
    ]
    q = body["queue"]
    assert q["pending_unclaimed"] == 1 and q["pending_claimed"] == 1
    assert q["confirmed"] == 0 and q["rejected"] == 0


def test_dashboard_requires_auth(app) -> None:
    assert TestClient(app).get("/dashboard/summary").status_code in (401, 403)
