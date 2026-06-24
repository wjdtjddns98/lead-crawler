"""검증 웹앱 API 테스트 — 큐 조회/확정/거부/export (in-process, 네트워크 0).

``fastapi`` 미설치(선택적 extra) 면 전체 모듈을 스킵한다.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from leadcrawler.config import get_settings  # noqa: E402
from leadcrawler.models import (  # noqa: E402
    Company,
    CompanyLead,
    Contact,
    ContactType,
    EmailRole,
    EmailValidation,
    ValidationStatus,
)
from leadcrawler.security import create_user  # noqa: E402
from leadcrawler.storage.db import init_db, session_scope  # noqa: E402
from leadcrawler.storage.repository import save_lead  # noqa: E402

_USER = "심사원"
_PW = "s3cret-pw"


def _seed(settings) -> None:
    lead = CompanyLead(
        company=Company(
            canonical_key="dom:acme.com",
            name="아크메",
            country="KR",
            industry="건설",
            domain="acme.com",
            homepage="https://acme.com",
            is_active=True,
            site_alive=True,
        ),
        email=Contact(type=ContactType.EMAIL, value="ir@acme.com", role=EmailRole.IR),
        email_validation=EmailValidation(status=ValidationStatus.VALID, mx=True, smtp=True),
    )
    with session_scope(settings) as s:
        save_lead(s, lead, source="test")
        create_user(s, _USER, _PW)


@pytest.fixture
def anon(tmp_path, monkeypatch) -> TestClient:
    """미인증 클라이언트(토큰 없음)."""
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/api.db")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings)
    _seed(settings)
    from leadcrawler.api.app import create_app

    return TestClient(create_app())


@pytest.fixture
def client(anon: TestClient) -> TestClient:
    """로그인된 클라이언트 — Authorization 헤더 기본 부착."""
    r = anon.post("/auth/login", json={"username": _USER, "password": _PW})
    assert r.status_code == 200
    anon.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return anon


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_queue_list(client: TestClient) -> None:
    r = client.get("/queue")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert [c["value"] for c in item["candidates"]] == ["ir@acme.com"]
    assert item["selected"] == "ir@acme.com"
    assert item["status"] == "pending"
    assert item["name"] == "아크메"
    assert item["email_status"] == "valid"


def test_queue_status_filter(client: TestClient) -> None:
    assert client.get("/queue", params={"status": "pending"}).json()["total"] == 1
    assert client.get("/queue", params={"status": "confirmed"}).json()["total"] == 0


def test_get_item_and_404(client: TestClient) -> None:
    rid = client.get("/queue").json()["items"][0]["id"]
    assert client.get(f"/queue/{rid}").json()["id"] == rid
    assert client.get("/queue/r_missing").status_code == 404


def test_confirm_then_export(client: TestClient) -> None:
    rid = client.get("/queue").json()["items"][0]["id"]
    r = client.post(f"/queue/{rid}/confirm")
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    assert r.json()["assignee"] == _USER  # 담당자=로그인 사용자(본문 무관).
    # 확정 후 export — 12컬럼 xlsx 가 내려와야 함.
    ex = client.get("/export")
    assert ex.status_code == 200
    assert "spreadsheetml" in ex.headers["content-type"]
    assert len(ex.content) > 0


def test_export_filter_by_country_industry(client: TestClient) -> None:
    import io

    from openpyxl import load_workbook

    rid = client.get("/queue").json()["items"][0]["id"]
    client.post(f"/queue/{rid}/confirm")  # 시드 리드(KR/건설) 확정.

    def data_rows(resp) -> int:
        return load_workbook(io.BytesIO(resp.content)).active.max_row - 1  # 헤더 제외.

    # 매칭(KR/건설) → 1행. 별칭(소문자 'kr')으로도 잡혀야 한다.
    assert data_rows(client.get("/export?country=KR&industry=건설")) == 1
    assert data_rows(client.get("/export?country=대한민국")) == 1  # 한글 별칭 매칭.
    # 불일치 국가/업종 → 0행(헤더만).
    assert data_rows(client.get("/export?country=JP")) == 0
    assert data_rows(client.get("/export?industry=반도체")) == 0


def test_reject(client: TestClient) -> None:
    rid = client.get("/queue").json()["items"][0]["id"]
    assert client.post(f"/queue/{rid}/reject").json()["status"] == "rejected"


def test_confirm_with_selection(client: TestClient) -> None:
    rid = client.get("/queue").json()["items"][0]["id"]
    r = client.post(f"/queue/{rid}/confirm", json={"selected": "ir@acme.com"})
    assert r.status_code == 200
    assert r.json()["selected"] == "ir@acme.com" and r.json()["status"] == "confirmed"


def test_confirm_edited_email_registers(client: TestClient) -> None:
    # 후보에 없는 '유효한' 이메일은 사람이 직접 수정/입력한 것으로 등록 후 확정된다.
    rid = client.get("/queue").json()["items"][0]["id"]
    r = client.post(f"/queue/{rid}/confirm", json={"selected": "fixed@acme.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "confirmed"
    assert body["selected"] == "fixed@acme.com"  # 수정값이 선택으로 기록.
    assert "fixed@acme.com" in [c["value"] for c in body["candidates"]]  # 후보로 등록.
    # 확정 후 export 에도 수정 이메일이 반영(연락처로 등록되므로).
    assert client.get("/export").status_code == 200


def test_confirm_invalid_format_400(client: TestClient) -> None:
    # 이메일 형식이 아니면 등록 거부(400) — 가비지 후보 차단.
    rid = client.get("/queue").json()["items"][0]["id"]
    r = client.post(f"/queue/{rid}/confirm", json={"selected": "not-an-email"})
    assert r.status_code == 400


def test_confirm_missing_404(client: TestClient) -> None:
    assert client.post("/queue/r_missing/confirm").status_code == 404


def test_invalid_status_422(client: TestClient) -> None:
    # 허용되지 않은 상태 필터는 FastAPI 가 422 로 거부(조용한 빈 결과 방지).
    assert client.get("/queue", params={"status": "bogus"}).status_code == 422


# --- 인증 ------------------------------------------------------------

def test_health_is_public(anon: TestClient) -> None:
    assert anon.get("/health").status_code == 200  # 헬스체크는 비보호.


def test_protected_routes_401_without_token(anon: TestClient) -> None:
    assert anon.get("/queue").status_code == 401
    assert anon.get("/export").status_code == 401
    assert anon.post("/queue/x/confirm").status_code == 401
    assert anon.get("/auth/me").status_code == 401


def test_login_wrong_password_401(anon: TestClient) -> None:
    r = anon.post("/auth/login", json={"username": _USER, "password": "wrong"})
    assert r.status_code == 401


def test_login_unknown_user_401(anon: TestClient) -> None:
    r = anon.post("/auth/login", json={"username": "ghost", "password": _PW})
    assert r.status_code == 401


def test_bad_token_401(anon: TestClient) -> None:
    anon.headers.update({"Authorization": "Bearer not-a-real-token"})
    assert anon.get("/queue").status_code == 401


def test_me_returns_username(client: TestClient) -> None:
    assert client.get("/auth/me").json()["username"] == _USER


def test_logout_invalidates_token(client: TestClient) -> None:
    assert client.get("/queue").status_code == 200  # 로그인 상태.
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/queue").status_code == 401  # 폐기된 토큰 → 거부.


def test_logout_without_token_ok(anon: TestClient) -> None:
    # 토큰 없이 로그아웃해도 200(멱등·무해).
    assert anon.post("/auth/logout").status_code == 200


def test_expired_session_rejected(anon: TestClient, monkeypatch) -> None:
    # TTL 0 → 즉시 만료(create_session 은 최소 1시간이지만 now 를 미래로 보정).
    from datetime import datetime, timedelta, timezone

    from leadcrawler import security

    r = anon.post("/auth/login", json={"username": _USER, "password": _PW})
    token = r.json()["token"]
    # 검증 시점을 14시간 뒤로 → 기본 TTL(12h) 초과로 만료 처리.
    future = datetime.now(timezone.utc) + timedelta(hours=14)
    monkeypatch.setattr(security, "_utcnow", lambda: future)
    anon.headers.update({"Authorization": f"Bearer {token}"})
    assert anon.get("/queue").status_code == 401
