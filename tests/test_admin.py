"""RBAC·감사 이력·관리자 API 테스트 (in-process, 네트워크 0).

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
from leadcrawler.security import ROLE_ADMIN, ROLE_WORKER, create_user  # noqa: E402
from leadcrawler.storage.db import init_db, session_scope  # noqa: E402
from leadcrawler.storage.repository import save_lead  # noqa: E402

_ADMIN = "관리자"
_WORKER = "직원"
_PW = "s3cret-pw-123"


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
        admin = create_user(s, _ADMIN, _PW)  # 첫 계정 → 자동 admin(부트스트랩).
        worker = create_user(s, _WORKER, _PW, role=ROLE_WORKER)
        assert admin.role == ROLE_ADMIN
        assert worker.role == ROLE_WORKER


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/admin.db")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings)
    _seed(settings)
    from leadcrawler.api.app import create_app

    return create_app()


def _client(app, username: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/auth/login", json={"username": username, "password": _PW})
    assert r.status_code == 200
    c.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return c


@pytest.fixture
def admin(app) -> TestClient:
    return _client(app, _ADMIN)


@pytest.fixture
def worker(app) -> TestClient:
    return _client(app, _WORKER)


# --- 부트스트랩·로그인 역할 ------------------------------------------

def test_first_user_is_admin(admin: TestClient) -> None:
    assert admin.get("/auth/me").json()["role"] == ROLE_ADMIN


def test_second_user_is_worker(worker: TestClient) -> None:
    assert worker.get("/auth/me").json()["role"] == ROLE_WORKER


def test_login_returns_role(app) -> None:
    c = TestClient(app)
    body = c.post("/auth/login", json={"username": _WORKER, "password": _PW}).json()
    assert body["role"] == ROLE_WORKER


# --- RBAC: worker 차단 -----------------------------------------------

def test_worker_cannot_list_users(worker: TestClient) -> None:
    assert worker.get("/admin/users").status_code == 403


def test_worker_cannot_export(worker: TestClient) -> None:
    assert worker.get("/export").status_code == 403


def test_worker_can_review(worker: TestClient) -> None:
    rid = worker.get("/queue").json()["items"][0]["id"]
    assert worker.post(f"/queue/{rid}/confirm").status_code == 200  # 검증은 worker 도 가능.


def test_admin_can_export(admin: TestClient) -> None:
    rid = admin.get("/queue").json()["items"][0]["id"]
    admin.post(f"/queue/{rid}/confirm")
    assert admin.get("/export").status_code == 200


# --- 계정 관리 -------------------------------------------------------

def test_admin_lists_users(admin: TestClient) -> None:
    rows = admin.get("/admin/users").json()
    names = {r["username"]: r for r in rows}
    assert names[_ADMIN]["role"] == ROLE_ADMIN
    assert names[_WORKER]["role"] == ROLE_WORKER


def test_admin_creates_user(admin: TestClient) -> None:
    r = admin.post(
        "/admin/users",
        json={"username": "신입", "password": "pw-12345678", "role": "worker"},
    )
    assert r.status_code == 201
    assert r.json()["username"] == "신입" and r.json()["role"] == "worker"


def test_create_duplicate_user_409(admin: TestClient) -> None:
    r = admin.post(
        "/admin/users", json={"username": _WORKER, "password": "pw-12345678"}
    )
    assert r.status_code == 409


def test_create_user_bad_role_400(admin: TestClient) -> None:
    r = admin.post(
        "/admin/users",
        json={"username": "x", "password": "pw-12345678", "role": "superuser"},
    )
    assert r.status_code == 400


def test_create_user_short_password_422(admin: TestClient) -> None:
    r = admin.post("/admin/users", json={"username": "x", "password": "short"})
    assert r.status_code == 422  # min_length=8 → pydantic 422.


def test_change_role(admin: TestClient) -> None:
    uid = _uid(admin, _WORKER)
    r = admin.post(f"/admin/users/{uid}/role", json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"


# --- 마지막 관리자 보호 ----------------------------------------------

def test_cannot_demote_last_admin(admin: TestClient) -> None:
    uid = _uid(admin, _ADMIN)
    r = admin.post(f"/admin/users/{uid}/role", json={"role": "worker"})
    assert r.status_code == 400  # 유일 관리자 강등 거부.


def test_cannot_deactivate_self(admin: TestClient) -> None:
    uid = _uid(admin, _ADMIN)
    r = admin.post(f"/admin/users/{uid}/active", params={"active": False})
    assert r.status_code == 400


def test_cannot_demote_self_even_with_other_admin(admin: TestClient) -> None:
    # 두 번째 관리자를 만들어 '마지막 관리자' 가드를 풀어도, 본인 강등은 별도로 차단돼야.
    admin.post(
        "/admin/users",
        json={"username": "admin2", "password": "pw-12345678", "role": "admin"},
    )
    uid = _uid(admin, _ADMIN)
    r = admin.post(f"/admin/users/{uid}/role", json={"role": "worker"})
    assert r.status_code == 400  # 본인 강등 차단.


def test_can_deactivate_worker(admin: TestClient) -> None:
    uid = _uid(admin, _WORKER)
    r = admin.post(f"/admin/users/{uid}/active", params={"active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False


def test_deactivated_user_cannot_login(admin: TestClient, app) -> None:
    uid = _uid(admin, _WORKER)
    admin.post(f"/admin/users/{uid}/active", params={"active": False})
    c = TestClient(app)
    assert c.post("/auth/login", json={"username": _WORKER, "password": _PW}).status_code == 401


def test_deactivate_revokes_existing_token(admin: TestClient, app) -> None:
    # 이미 로그인한 토큰도 비활성화 즉시 무효화돼야(세션 행 삭제).
    wc = TestClient(app)
    tok = wc.post("/auth/login", json={"username": _WORKER, "password": _PW}).json()["token"]
    wc.headers.update({"Authorization": f"Bearer {tok}"})
    assert wc.get("/queue").status_code == 200  # 비활성 전엔 정상.
    uid = _uid(admin, _WORKER)
    admin.post(f"/admin/users/{uid}/active", params={"active": False})
    assert wc.get("/queue").status_code == 401  # 세션 폐기 → 즉시 거부.


# --- 감사 이력 -------------------------------------------------------

def test_audit_records_confirm(admin: TestClient) -> None:
    rid = admin.get("/queue").json()["items"][0]["id"]
    admin.post(f"/queue/{rid}/confirm", json={"selected": "ir@acme.com"})
    log = admin.get("/admin/audit").json()
    assert len(log) == 1
    entry = log[0]
    assert entry["action"] == "confirmed"
    assert entry["actor_username"] == _ADMIN
    assert entry["selected"] == "ir@acme.com"
    assert entry["company_name"] == "아크메"


def test_audit_keeps_history_across_reprocessing(admin: TestClient) -> None:
    rid = admin.get("/queue").json()["items"][0]["id"]
    admin.post(f"/queue/{rid}/reject")
    admin.post(f"/queue/{rid}/confirm")  # 재처리 — 이력 2건 누적.
    log = admin.get("/admin/audit").json()
    assert [e["action"] for e in log] == ["confirmed", "rejected"]  # 최신순.


def test_reviewed_at_populated(admin: TestClient) -> None:
    rid = admin.get("/queue").json()["items"][0]["id"]
    assert admin.get(f"/queue/{rid}").json()["reviewed_at"] is None
    admin.post(f"/queue/{rid}/confirm")
    assert admin.get(f"/queue/{rid}").json()["reviewed_at"] is not None


def test_worker_cannot_view_audit(worker: TestClient) -> None:
    assert worker.get("/admin/audit").status_code == 403


def _uid(client: TestClient, username: str) -> str:
    rows = client.get("/admin/users").json()
    return next(r["id"] for r in rows if r["username"] == username)
