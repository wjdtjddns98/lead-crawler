"""세그먼트 작업 큐 admin API(#398 PR⑤) — in-process, 네트워크 0(가짜 자식 프로세스)."""

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
    """생존형 가짜 자식 — kill_tree 전까지 poll() 이 None(살아있음)을 반환."""

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
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/segjobs.db")
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


def _wait_job_status(client: TestClient, job_id: str, want: str, timeout: float = 8.0) -> dict:
    end = time.monotonic() + timeout
    body = None
    while time.monotonic() < end:
        body = client.get(f"/admin/segment-jobs/{job_id}").json()
        if body["status"] == want:
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} 가 {want} 에 도달하지 못함: {body}")


def _create(client: TestClient, **overrides) -> dict:
    payload = {"countries": "KR", "industries": "은행"}
    payload.update(overrides)
    return client.post("/admin/segment-jobs", json=payload)


def test_worker_forbidden(app) -> None:
    worker = _client(app, _WORKER)
    payload = {"countries": "KR", "industries": "은행"}
    assert worker.post("/admin/segment-jobs", json=payload).status_code == 403
    assert worker.get("/admin/segment-jobs").status_code == 403
    assert worker.get("/admin/segment-jobs/preview", params=payload).status_code == 403


def test_create_immediate_then_queued(app) -> None:
    """활성 S 없으면 즉시 running, 두 번째는 queued+queue_position=1."""
    admin = _client(app, _ADMIN)
    r1 = _create(admin, industries="은행")
    assert r1.status_code == 201
    body1 = r1.json()
    assert body1["status"] == "running"
    assert body1["track"] == "S"
    assert body1["queue_position"] is None

    r2 = _create(admin, industries="보험")
    assert r2.status_code == 201
    body2 = r2.json()
    assert body2["status"] == "queued"
    assert body2["queue_position"] == 1
    assert body2["priority"] == 100  # 기본값.


def test_create_validation_422(app, monkeypatch) -> None:
    admin = _client(app, _ADMIN)
    # 미지원 국가.
    assert _create(admin, countries="XX").status_code == 422
    # 택소노미 밖 업종.
    assert _create(admin, industries="없는업종").status_code == 422
    # listed 잘못된 값(pydantic Literal 로 422).
    r = admin.post(
        "/admin/segment-jobs",
        json={"countries": "KR", "industries": "은행", "listed": "bogus"},
    )
    assert r.status_code == 422
    # regions 는 countries 에 KR 없으면 422.
    assert _create(admin, countries="US", regions="서울").status_code == 422
    # regions 값이 KR_REGIONS/all 밖이면 422.
    assert _create(admin, countries="KR", regions="Seoul").status_code == 422
    # 빈 CSV.
    r = admin.post("/admin/segment-jobs", json={"countries": "", "industries": "은행"})
    assert r.status_code == 422
    # '미분류' 는 발견 키워드로 무의미 + 광역 소스가 켜져 세그먼트 1개로 상한 우회 → 422.
    assert _create(admin, industries="미분류").status_code == 422
    # priority 범위 밖(PG int4 오버플로·음수 새치기) → 422.
    assert _create(admin, priority=2**40).status_code == 422
    assert _create(admin, priority=-1).status_code == 422
    # 세그먼트 상한 초과.
    monkeypatch.setenv("LEADCRAWLER_CRAWL_MAX_SEGMENTS", "1")
    get_settings.cache_clear()
    r = _create(admin, countries="KR", industries="은행,보험")  # 2 세그먼트 > 상한 1.
    assert r.status_code == 422
    get_settings.cache_clear()


def test_list_sorting_and_status_filter(app) -> None:
    admin = _client(app, _ADMIN)
    running = _create(admin, industries="은행").json()  # 즉시 running.
    low_pri = _create(admin, industries="보험", priority=50).json()  # queued, 우선순위 낮음(먼저).
    high_pri = _create(admin, industries="증권·자산운용", priority=200).json()  # queued, 나중.

    body = admin.get("/admin/segment-jobs").json()
    assert body["total"] == 3
    ids = [it["id"] for it in body["items"]]
    assert ids == [running["id"], low_pri["id"], high_pri["id"]]

    only_queued = admin.get("/admin/segment-jobs", params={"status": "queued"}).json()
    assert only_queued["total"] == 2
    assert all(it["status"] == "queued" for it in only_queued["items"])


def test_detail_404(app) -> None:
    admin = _client(app, _ADMIN)
    assert admin.get("/admin/segment-jobs/bf_nope").status_code == 404


def test_cancel_running_queued_and_terminal_409(app) -> None:
    admin = _client(app, _ADMIN)
    running = _create(admin, industries="은행").json()
    queued = _create(admin, industries="보험").json()

    # queued → 즉시 cancelled.
    r = admin.post(f"/admin/segment-jobs/{queued['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    # 종료건 재취소 → 409.
    assert admin.post(f"/admin/segment-jobs/{queued['id']}/cancel").status_code == 409

    # running → 협조 취소(cancel_requested=True) 후 비동기 cancelled 도달.
    r = admin.post(f"/admin/segment-jobs/{running['id']}/cancel")
    assert r.status_code == 200
    assert r.json()["cancel_requested"] is True
    _wait_job_status(admin, running["id"], "cancelled")


def test_pause_queued_immediate_and_running_cooperative(app) -> None:
    admin = _client(app, _ADMIN)
    running = _create(admin, industries="은행").json()
    queued = _create(admin, industries="보험").json()

    r = admin.post(f"/admin/segment-jobs/{queued['id']}/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"

    # 이미 종료(cancelled)건 pause → 409. running 을 그대로 점유시켜 두어 신규 잡이 확실히
    # queued 로 남게 한다(비워지는 순간 대기열 자동전개와의 경합 회피).
    cancelled = _create(admin, industries="증권·자산운용").json()
    assert cancelled["status"] == "queued"
    r = admin.post(f"/admin/segment-jobs/{cancelled['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert admin.post(f"/admin/segment-jobs/{cancelled['id']}/pause").status_code == 409

    # running → 협조 중단(수초 내 paused).
    r = admin.post(f"/admin/segment-jobs/{running['id']}/pause")
    assert r.status_code == 200  # 즉시 paused 는 아니어도 running/paused 중 하나.
    _wait_job_status(admin, running["id"], "paused")


def test_resume_to_queued_or_running(app) -> None:
    admin = _client(app, _ADMIN)
    running = _create(admin, industries="은행").json()
    queued = _create(admin, industries="보험").json()
    admin.post(f"/admin/segment-jobs/{queued['id']}/pause")

    # 다른 S 가 여전히 running 중이면 resume → queued.
    r = admin.post(f"/admin/segment-jobs/{queued['id']}/resume")
    assert r.status_code == 200 and r.json()["status"] == "queued"

    # 재개 불가 상태(이미 queued)에서 다시 resume → 409.
    assert admin.post(f"/admin/segment-jobs/{queued['id']}/resume").status_code == 409
    # 미존재 → 404.
    assert admin.post("/admin/segment-jobs/bf_nope/resume").status_code == 404

    # paused 로 되돌려 둔다 — running 취소가 트리거하는 대기열 자동전개(설계 §3)가 이
    # 잡을 queued 상태에서 먼저 채가는 경합을 피한다(paused 는 자동전개 대상이 아님).
    admin.post(f"/admin/segment-jobs/{queued['id']}/pause")
    admin.post(f"/admin/segment-jobs/{running['id']}/cancel")
    _wait_job_status(admin, running["id"], "cancelled")

    # running 슬롯이 비었으니 재개하면 즉시(또는 자동전개로 곧) running.
    r = admin.post(f"/admin/segment-jobs/{queued['id']}/resume")
    assert r.status_code == 200
    _wait_job_status(admin, queued["id"], "running")


def test_patch_priority_409_running_ok_queued(app) -> None:
    admin = _client(app, _ADMIN)
    running = _create(admin, industries="은행").json()
    queued = _create(admin, industries="보험").json()

    r = admin.patch(f"/admin/segment-jobs/{running['id']}", json={"priority": 10})
    assert r.status_code == 409

    r = admin.patch(f"/admin/segment-jobs/{queued['id']}", json={"priority": 10})
    assert r.status_code == 200 and r.json()["priority"] == 10

    assert admin.patch("/admin/segment-jobs/bf_nope", json={"priority": 1}).status_code == 404


def test_preview_values(app) -> None:
    admin = _client(app, _ADMIN)
    r = admin.get("/admin/segment-jobs/preview", params={"countries": "KR", "industries": "은행"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"segments", "promote_pending", "max_segments"}
    assert body["segments"] >= 1
    assert isinstance(body["promote_pending"], int)
    # 검증 위반은 미리보기도 422(생성 없이).
    r = admin.get("/admin/segment-jobs/preview", params={"countries": "XX", "industries": "은행"})
    assert r.status_code == 422


def test_existing_backfill_routes_unaffected(app) -> None:
    """기존 /admin/backfill/* 회귀 없음 — 트랙 S 잡 존재와 무관하게 idle."""
    admin = _client(app, _ADMIN)
    _create(admin, industries="은행")
    body = admin.get("/admin/backfill/status").json()
    assert body["resolve"]["status"] == "idle" and body["fill"]["status"] == "idle"


def test_duplicate_and_alias_tokens_are_collapsed(app) -> None:
    """'KR,kr,대한민국'·'은행,은행' 은 세그먼트 1개 — 중복 유료 발견 차단(리뷰 HIGH)."""
    admin = _client(app, _ADMIN)
    r = admin.get(
        "/admin/segment-jobs/preview",
        params={"countries": "KR,kr,대한민국", "industries": "은행,은행"},
    )
    assert r.status_code == 200 and r.json()["segments"] == 1
    assert "max_segments" in r.json()
    body = _create(admin, countries="KR,kr,대한민국", industries="은행,은행").json()
    assert body["countries"] == "KR" and body["industries"] == "은행"


def test_pause_already_paused_409_and_other_track_404(app) -> None:
    """이미 paused 재요청은 409(설계 §5), 트랙 A id 는 모든 라우트에서 404."""
    admin = _client(app, _ADMIN)
    _create(admin, industries="은행")  # running 점유.
    queued = _create(admin, industries="보험").json()
    assert admin.post(f"/admin/segment-jobs/{queued['id']}/pause").status_code == 200
    assert admin.post(f"/admin/segment-jobs/{queued['id']}/pause").status_code == 409

    from leadcrawler.storage.backfill_job import create_backfill_job

    with session_scope(get_settings()) as s:
        a = create_backfill_job(s, track="C", countries="KR")
        aid = a.id
    for path in (f"/{aid}", f"/{aid}/cancel", f"/{aid}/pause", f"/{aid}/resume"):
        method = admin.get if path == f"/{aid}" else admin.post
        assert method(f"/admin/segment-jobs{path}").status_code == 404
    assert admin.patch(f"/admin/segment-jobs/{aid}", json={"priority": 1}).status_code == 404


def test_create_returns_201_even_if_dispatch_fails(app, monkeypatch) -> None:
    """적재는 커밋됨 — 전개 실패는 500 이 아니라 201+queued(중복 재제출 방지, 리뷰 MED)."""
    admin = _client(app, _ADMIN)

    def boom(settings):  # noqa: ANN001
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(bp, "dispatch_next_segment_job", boom)
    r = _create(admin, industries="은행")
    assert r.status_code == 201 and r.json()["status"] == "queued"


def test_cancel_queued_is_atomic_against_activate(app) -> None:
    """queued 취소는 원자 조건부 UPDATE — 이미 running 이면 강제 종료 대신 협조 취소."""
    admin = _client(app, _ADMIN)
    running = _create(admin, industries="은행").json()
    assert running["status"] == "running"
    r = admin.post(f"/admin/segment-jobs/{running['id']}/cancel")
    assert r.status_code == 200 and r.json()["cancel_requested"] is True
    _wait_job_status(admin, running["id"], "cancelled")
