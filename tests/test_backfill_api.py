"""백필 admin API(#352 PR⑤) — 딸깍 시작/중지/현황/잔여 깔때기 (in-process, 네트워크 0)."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import leadcrawler.pipeline.backfill_process as bp  # noqa: E402
from leadcrawler.config import get_settings  # noqa: E402
from leadcrawler.security import ROLE_WORKER, create_user  # noqa: E402
from leadcrawler.storage.db import init_db, session_scope  # noqa: E402

_ADMIN = "관리자"
_WORKER = "직원"
_PW = "s3cret-pw-123"


class _FakeProc:
    pid = 4242

    def __init__(self) -> None:
        self._rc: int | None = None

    def poll(self):  # noqa: ANN202
        return self._rc

    def wait(self):  # noqa: ANN202
        return self._rc if self._rc is not None else -9

    def kill_tree(self) -> None:
        self._rc = -9

    def release(self) -> None:
        pass


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/bfapi.db")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as s:
        create_user(s, _ADMIN, _PW)  # 첫 계정 → admin 부트스트랩.
        create_user(s, _WORKER, _PW, role=ROLE_WORKER)
    bp._reset_running_guard_for_tests()
    monkeypatch.setattr(bp, "_CANCEL_POLL", 0.02)
    # 실 Popen 대신 생존형 가짜 자식 — API 경유로도 supervisor 가 정상 감독한다.
    monkeypatch.setattr(bp, "_default_launcher", lambda argv, log_path: _FakeProc())
    from leadcrawler.api.app import create_app

    yield create_app()
    get_settings.cache_clear()


def _client(app, username: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/auth/login", json={"username": username, "password": _PW})
    assert r.status_code == 200
    c.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return c


def _wait_status(client: TestClient, key: str, want: str, timeout: float = 8.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        body = client.get("/admin/backfill/status").json()
        if body[key]["status"] == want:
            return body
        time.sleep(0.05)
    raise AssertionError(f"{key} 가 {want} 에 도달하지 못함: {body}")


def test_worker_forbidden(app) -> None:
    worker = _client(app, _WORKER)
    assert worker.get("/admin/backfill/overview").status_code == 403
    assert worker.post("/admin/backfill/start", json={}).status_code == 403
    assert worker.get("/admin/backfill/status").status_code == 403
    assert worker.post("/admin/backfill/stop").status_code == 403


def test_partial_activation_rolls_back_track_c(app, monkeypatch) -> None:
    """C 성공 후 A 가 경쟁으로 막히면 — 409 + C 가 실제로 되감겨 cancelled 로 마감된다."""
    from leadcrawler.storage.backfill_job import BackfillBusy

    admin = _client(app, _ADMIN)
    real_start = bp.start_backfill

    def busy_on_a(settings, *, track, **kw):  # noqa: ANN001
        if track == "A":
            raise BackfillBusy("A")  # C 시작 직후 A 가 경쟁자에게 선점된 상황.
        return real_start(settings, track=track, **kw)

    monkeypatch.setattr("leadcrawler.pipeline.backfill_process.start_backfill", busy_on_a)
    r = admin.post("/admin/backfill/start", json={"countries": "KR"})
    assert r.status_code == 409
    # 롤백은 비동기(취소 플래그 → supervisor 마감) — cancelled 도달을 폴링으로 실증.
    _wait_status(admin, "resolve", "cancelled")
    # 되감긴 뒤에는 running 잔존이 없어 새 시작이 가능하다(고아 running 방지 증명).
    # undo() 금지 — 픽스처의 가짜 런처 패치까지 전부 되돌려 실 자식이 스폰된다(실측).
    monkeypatch.setattr("leadcrawler.pipeline.backfill_process.start_backfill", real_start)
    assert admin.post("/admin/backfill/start", json={}).status_code == 202
    admin.post("/admin/backfill/stop")
    _wait_status(admin, "fill", "cancelled")


def test_overview_returns_funnel_counts(app) -> None:
    admin = _client(app, _ADMIN)
    body = admin.get(
        "/admin/backfill/overview",
        params={"countries": "KR", "exclude_industries": "은행,보험", "exclude_listed": True},
    ).json()
    assert set(body) == {"resolve_pending", "fill_pending", "queue_pending"}
    assert all(isinstance(v, int) for v in body.values())


def test_start_status_duplicate_stop_flow(app) -> None:
    """딸깍 플로우 — 시작(두 트랙 동시) → 현황 → 중복 409 → 중지 → cancelled 마감."""
    admin = _client(app, _ADMIN)
    r = admin.post(
        "/admin/backfill/start",
        json={"countries": "KR", "exclude_industries": "은행", "exclude_listed": True},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["resolve"]["status"] == "running" and body["fill"]["status"] == "running"
    assert body["fill"]["countries"] == "KR" and body["fill"]["exclude_listed"] is True
    assert body["fill"]["triggered_by"] == _ADMIN

    assert admin.post("/admin/backfill/start", json={}).status_code == 409  # 중복.

    r = admin.post("/admin/backfill/stop")
    assert r.status_code == 200
    _wait_status(admin, "resolve", "cancelled")
    _wait_status(admin, "fill", "cancelled")
    assert admin.post("/admin/backfill/stop").status_code == 404  # 활성 없음.
    # 마감 후엔 새 시작 가능(활성 유니크 해제).
    assert admin.post("/admin/backfill/start", json={}).status_code == 202
    admin.post("/admin/backfill/stop")
    _wait_status(admin, "fill", "cancelled")


def test_status_idle_without_history(app) -> None:
    admin = _client(app, _ADMIN)
    body = admin.get("/admin/backfill/status").json()
    assert body["resolve"]["status"] == "idle" and body["fill"]["status"] == "idle"
