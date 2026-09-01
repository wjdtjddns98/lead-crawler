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


def test_worker_can_export_own_scope(worker: TestClient) -> None:
    """export 개방(PO 2026-07-14): worker 도 200 — 내용 스코프(자기 확정분만) 검증은
    tests/test_api.py::test_export_worker_own_scope_admin_full 쪽."""
    assert worker.get("/export").status_code == 200


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


# --- 직원별 일일 처리 통계 -------------------------------------------

def test_review_daily_stats(admin: TestClient) -> None:
    rid = admin.get("/queue").json()["items"][0]["id"]
    admin.post(f"/queue/{rid}/reject")
    admin.post(f"/queue/{rid}/confirm")  # 재처리 — 오늘 확정 1·거부 1 누적.
    body = admin.get("/admin/stats/review-daily").json()
    assert body["items"] == [{"username": _ADMIN, "confirmed": 1, "rejected": 1}]
    # 다른 일자로 조회하면 빈 목록 — date 파라미터의 하루 경계 스코프 검증.
    old = admin.get("/admin/stats/review-daily", params={"date": "2020-01-01"}).json()
    assert old["date"] == "2020-01-01"
    assert old["items"] == []


def test_worker_cannot_view_daily_stats(worker: TestClient) -> None:
    assert worker.get("/admin/stats/review-daily").status_code == 403


# --- 크롤 타깃(웹앱 관리자 설정) ------------------------------------

def test_get_crawl_target_default(admin: TestClient) -> None:
    # 미설정이면 .env 기본값을 폼 초기값으로 돌려준다(updated_by 없음).
    body = admin.get("/admin/crawl-target").json()
    assert body["industries"]  # .env report_industries(기본 '건설')
    assert body["updated_by"] is None


def test_set_then_get_crawl_target(admin: TestClient) -> None:
    r = admin.put(
        "/admin/crawl-target",
        json={"countries": "KR,US", "industries": "건설,반도체", "listed": "listed",
              "persist": True},
    )
    assert r.status_code == 200
    got = admin.get("/admin/crawl-target").json()
    assert got["countries"] == "KR,US"
    assert got["industries"] == "건설,반도체"
    assert got["listed"] == "listed"
    assert got["updated_by"] == _ADMIN  # 설정자 기록.
    assert got["updated_at"] is not None


def test_list_countries(admin: TestClient) -> None:
    from leadcrawler.sources.countries import supported_countries

    rows = admin.get("/admin/countries").json()
    assert len(rows) == len(supported_countries())  # 지원 국가 전체.
    by_iso = {r["iso2"]: r["label"] for r in rows}
    # 한글 라벨이 별칭에서 정확히 추출된다.
    assert by_iso["KR"] == "대한민국"
    assert by_iso["US"] == "미국"
    assert by_iso["GB"] == "영국"
    assert rows[0]["iso2"] == "US"  # 우선순위 순서 보존(supported_countries 순).
    # 별칭 노출 — 'UK'로 검색해도 영국(GB)이 잡히게 한다.
    gb = next(r for r in rows if r["iso2"] == "GB")
    assert "uk" in gb["aliases"]


def test_worker_cannot_list_countries(worker: TestClient) -> None:
    assert worker.get("/admin/countries").status_code == 403


def test_list_industries(admin: TestClient) -> None:
    from leadcrawler.sources.industry import supported_industries

    rows = admin.get("/admin/industries").json()
    assert len(rows) == len(supported_industries())
    by_val = {r["value"]: r for r in rows}
    # 크롤 업종 옵션은 큐/발송 탭과 같은 택소노미 라벨 어휘로 노출된다.
    assert "건설·엔지니어링" in by_val
    assert by_val["건설·엔지니어링"]["label"] == "건설·엔지니어링"
    # 영문 별칭으로 검색 가능하게 노출('construction'→건설·엔지니어링).
    assert "construction" in by_val["건설·엔지니어링"]["aliases"]


def test_worker_cannot_list_industries(worker: TestClient) -> None:
    assert worker.get("/admin/industries").status_code == 403


def test_worker_cannot_read_target(worker: TestClient) -> None:
    assert worker.get("/admin/crawl-target").status_code == 403


def test_worker_cannot_set_target(worker: TestClient) -> None:
    r = worker.put("/admin/crawl-target", json={"industries": "건설"})
    assert r.status_code == 403


def test_invalid_listed_422(admin: TestClient) -> None:
    # listed 는 Literal → 스키마 레이어에서 422(자기문서화·유효값 노출).
    r = admin.put("/admin/crawl-target", json={"industries": "건설", "listed": "bogus"})
    assert r.status_code == 422


def test_industries_required_422(admin: TestClient) -> None:
    r = admin.put("/admin/crawl-target", json={"industries": "", "countries": "KR"})
    assert r.status_code == 422  # min_length=1 → pydantic 422.


def test_whitespace_industries_422(admin: TestClient) -> None:
    # 공백만 입력은 트림 후 빈 문자열 → 422(저장됐다가 조용히 무시되는 갱 차단).
    r = admin.put("/admin/crawl-target", json={"industries": "   "})
    assert r.status_code == 422


def test_scheduler_uses_db_target(admin: TestClient) -> None:
    # 관리자가 설정한 DB 타깃을 스케줄러가 우선 사용(.env 폴백 아님).
    from leadcrawler.config import get_settings
    from leadcrawler.scheduler import _effective_target

    admin.put(
        "/admin/crawl-target",
        json={"countries": "JP", "industries": "반도체", "listed": "unlisted",
              "persist": False},
    )
    inds, ctys, listed, persist = _effective_target(get_settings())
    assert inds == ["반도체"]
    assert ctys == ["JP"]
    assert listed == "unlisted"
    assert persist is False


# 직접 크롤(웹 트리거·현황·취소) 기능은 2026-09-01 제거됨 — 세그먼트 작업 큐로 일원화.
# (구 테스트: test_worker_cannot_start_crawl 등 /admin/crawl* 라우트 테스트 전부 삭제.
#  /admin/crawl-target 만 유지 — 위 크롤-타깃 테스트 참고.)


def _uid(client: TestClient, username: str) -> str:
    rows = client.get("/admin/users").json()
    return next(r["id"] for r in rows if r["username"] == username)


# ── 회사 DB 검색(/admin/companies) ──────────────────────────────────────────


def test_company_search_matches_name_homepage_email(admin):
    for q in ("아크메", "ACME.COM", "ir@acme"):
        r = admin.get("/admin/companies", params={"q": q})
        assert r.status_code == 200, q
        body = r.json()
        assert body["total"] == 1, q
        item = body["items"][0]
        assert item["name"] == "아크메"
        assert item["homepage"] == "https://acme.com"
        assert item["emails"] == [{"value": "ir@acme.com", "role": "ir", "status": "valid"}]
        assert item["form"] is None
        assert item["review_status"] == "pending"  # save_lead 가 큐에 적재.
        assert item["listed"] == "unknown"


def test_company_search_no_match_and_wildcards_escaped(admin):
    assert admin.get("/admin/companies", params={"q": "없는회사"}).json()["total"] == 0
    # '%' 는 문자 그대로 — 전체 매치가 아니어야 한다.
    assert admin.get("/admin/companies", params={"q": "%"}).json()["total"] == 0
    assert admin.get("/admin/companies", params={"q": "_"}).json()["total"] == 0
    assert admin.get("/admin/companies", params={"q": " "}).json()["total"] == 0


def test_company_search_requires_admin(app):
    worker = _client(app, _WORKER)
    assert worker.get("/admin/companies", params={"q": "아크메"}).status_code == 403
    assert TestClient(app).get("/admin/companies", params={"q": "아크메"}).status_code == 401


def test_company_search_rejects_empty_q(admin):
    assert admin.get("/admin/companies").status_code == 422
    assert admin.get("/admin/companies", params={"q": ""}).status_code == 422


def test_company_search_review_status_filter(admin):
    from sqlalchemy import select

    from leadcrawler.schema import CompanyRow, DiscoveredCompanyRow, ReviewQueueRow

    # 큐 미적재 회사(동일 검색어 매치)는 어떤 상태 필터에도 안 걸린다.
    with session_scope(get_settings()) as s:
        s.add(DiscoveredCompanyRow(canonical_key="dom:acme2.kr", name="아크메둘"))
        s.flush()
        s.add(CompanyRow(id="c-acme2", canonical_key="dom:acme2.kr", name="아크메둘", country="KR"))

    params = {"q": "아크메", "review_status": "confirmed"}
    assert admin.get("/admin/companies", params=params).json()["total"] == 0
    assert admin.get("/admin/companies", params={"q": "아크메", "review_status": "pending"}).json()[
        "total"
    ] == 1
    with session_scope(get_settings()) as s:
        rq = s.scalars(select(ReviewQueueRow).where(ReviewQueueRow.field == "email")).one()
        rq.status = "confirmed"
    body = admin.get("/admin/companies", params=params).json()
    assert body["total"] == 1 and body["items"][0]["review_status"] == "confirmed"
    # enum 밖 값은 422, 파라미터 생략 시 기존 전체 검색 그대로(미적재 회사 포함 2곳).
    assert admin.get("/admin/companies", params={"q": "아크메", "review_status": "??"}).status_code == 422
    assert admin.get("/admin/companies", params={"q": "아크메"}).json()["total"] == 2


def test_company_search_pagination_name_eng_and_unqueued(app, admin):
    from leadcrawler.schema import CompanyRow, DiscoveredCompanyRow

    with session_scope(get_settings()) as s:
        # 큐 미적재 회사 2곳(직접 삽입, 원장 FK 먼저) — 그중 하나만 원장 영문명 매치.
        s.add(DiscoveredCompanyRow(canonical_key="dom:a.jp", name="エー"))
        s.add(DiscoveredCompanyRow(canonical_key="dom:b.jp", name="ビー", name_eng="Bee Holdings"))
        s.flush()
        s.add(CompanyRow(id="c-a", canonical_key="dom:a.jp", name="エー", country="JP"))
        s.add(CompanyRow(id="c-b", canonical_key="dom:b.jp", name="ビー", country="JP"))
    r = admin.get("/admin/companies", params={"q": "bee hold"}).json()
    assert [i["id"] for i in r["items"]] == ["c-b"]
    assert r["items"][0]["review_status"] is None and r["items"][0]["emails"] == []
    # 페이지네이션: 장음부호 'ー' 로 두 JP 회사만 매치 → limit 1 두 페이지가 서로소·total 일치.
    p1 = admin.get("/admin/companies", params={"q": "ー", "limit": 1}).json()
    p2 = admin.get("/admin/companies", params={"q": "ー", "limit": 1, "offset": 1}).json()
    assert p1["total"] == p2["total"] == 2
    assert {p1["items"][0]["id"], p2["items"][0]["id"]} == {"c-a", "c-b"}
