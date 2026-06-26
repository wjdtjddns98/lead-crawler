"""중복후보 워크벤치(C4) — storage(적재·결정) + API(목록·머지·분리·권한) 테스트.

전부 in-process SQLite, 네트워크·과금 0. ``fastapi`` 미설치면 API 부분만 스킵한다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from leadcrawler.config import Settings, get_settings
from leadcrawler.dedup_resolve.report import build_report, load_company_records
from leadcrawler.schema import DiscoveredCompanyRow
from leadcrawler.storage import dedup_candidate as wb
from leadcrawler.storage.db import init_db, session_scope


@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/wb.db", dry_run=True)
    init_db(settings)
    with session_scope(settings) as s:
        yield s


def _seed_lexical_pair(s: Session) -> None:
    """동명·도메인불명 쌍 → near_dup 'lexical'(워크벤치 경계 티어) 1쌍을 만든다."""
    s.add_all(
        [
            DiscoveredCompanyRow(canonical_key="reg:kr:1", name="Orion Tech", country="KR"),
            DiscoveredCompanyRow(canonical_key="reg:kr:2", name="Orion Tech", country="KR"),
        ]
    )
    s.flush()


def _populate(s: Session) -> dict:
    report = build_report(load_company_records(s))
    return wb.populate_candidates(s, report)


# ── storage ──────────────────────────────────────────────────────────────────


def test_pair_id_is_order_independent() -> None:
    assert wb.pair_id("a", "b") == wb.pair_id("b", "a")
    assert wb.pair_id("a", "b") != wb.pair_id("a", "c")


def test_populate_ingests_boundary_and_is_idempotent(session: Session) -> None:
    _seed_lexical_pair(session)
    first = _populate(session)
    assert first["created"] == 1
    listing = wb.list_candidates(session)
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["tier"] == "lexical"
    assert item["status"] == "pending"
    assert item["name_a"] == "Orion Tech" and item["name_b"] == "Orion Tech"
    # 재적재는 멱등 — 새로 만들지 않고 갱신만.
    second = _populate(session)
    assert second["created"] == 0
    assert wb.list_candidates(session)["total"] == 1


def test_auto_tier_not_ingested(session: Session) -> None:
    # 동명+도메인root 일치 → 'auto'(자동해소) → 워크벤치 미적재.
    session.add_all(
        [
            DiscoveredCompanyRow(
                canonical_key="dom:acme.com", name="Acme Inc", country="KR", domain="acme.com"
            ),
            DiscoveredCompanyRow(
                canonical_key="name:kr:acme", name="Acme Inc", country="KR", domain="www.acme.com"
            ),
        ]
    )
    session.flush()
    stats = _populate(session)
    assert stats["created"] == 0
    assert wb.list_candidates(session)["total"] == 0


def test_merge_applies_golden_and_records_duplicate_of(session: Session) -> None:
    _seed_lexical_pair(session)
    _populate(session)
    cid = wb.list_candidates(session)["items"][0]["id"]
    result = wb.decide_merge(session, cid, decided_by="관리자")
    assert result is not None
    assert result["status"] == "merged"
    survivor = result["survivor_key"]
    assert survivor in ("reg:kr:1", "reg:kr:2")
    absorbed_key = "reg:kr:2" if survivor == "reg:kr:1" else "reg:kr:1"
    # 흡수된 행에 duplicate_of=생존자, 머지 감사 기록(가역).
    absorbed = session.get(DiscoveredCompanyRow, absorbed_key)
    assert absorbed.duplicate_of == survivor
    assert absorbed.merged_by == "관리자"
    # pending 목록에서 사라짐.
    assert wb.list_candidates(session, status="pending")["total"] == 0
    assert wb.list_candidates(session, status="merged")["total"] == 1


def test_separate_persists_and_not_resurrected(session: Session) -> None:
    _seed_lexical_pair(session)
    _populate(session)
    cid = wb.list_candidates(session)["items"][0]["id"]
    result = wb.decide_separate(session, cid, decided_by="직원")
    assert result["status"] == "separated"
    # 재적재해도 분리 결정이 부활하지 않음(같은 쌍 skip).
    stats = _populate(session)
    assert stats["created"] == 0
    assert stats["skipped"] == 1
    assert wb.list_candidates(session, status="pending")["total"] == 0
    assert wb.list_candidates(session, status="separated")["total"] == 1


def test_decide_on_already_decided_conflicts(session: Session) -> None:
    _seed_lexical_pair(session)
    _populate(session)
    cid = wb.list_candidates(session)["items"][0]["id"]
    wb.decide_separate(session, cid, decided_by="직원")
    with pytest.raises(wb.DedupConflict):
        wb.decide_merge(session, cid, decided_by="관리자")


def test_decide_missing_returns_none(session: Session) -> None:
    assert wb.decide_merge(session, "nope", decided_by="x") is None
    assert wb.decide_separate(session, "nope", decided_by="x") is None


def test_merge_stale_pair_conflicts(session: Session) -> None:
    # 한쪽 행이 다른 경로로 원장에서 사라진 stale 쌍 → 머지 불가(409/DedupConflict).
    _seed_lexical_pair(session)
    _populate(session)
    cid = wb.list_candidates(session)["items"][0]["id"]
    session.delete(session.get(DiscoveredCompanyRow, "reg:kr:2"))
    session.flush()
    with pytest.raises(wb.DedupConflict):
        wb.decide_merge(session, cid, decided_by="관리자")
    # 상태 미변경(여전히 pending — 재적재/새로고침이 정리).
    assert session.get(wb.DedupCandidateRow, cid).status == "pending"


def test_summary_counts(session: Session) -> None:
    _seed_lexical_pair(session)
    _populate(session)
    summ = wb.summary(session)
    assert summ == {"pending": 1, "merged": 0, "separated": 0, "total": 1}


# ── API e2e ──────────────────────────────────────────────────────────────────

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from leadcrawler.security import ROLE_WORKER, create_user  # noqa: E402

_ADMIN = "관리자"
_WORKER = "직원"
_PW = "s3cret-pw-123"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/wb_api.db")
    get_settings.cache_clear()
    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as s:
        _seed_lexical_pair(s)
        create_user(s, _ADMIN, _PW)  # 첫 계정 → admin.
        create_user(s, _WORKER, _PW, role=ROLE_WORKER)
    from leadcrawler.api.app import create_app

    yield create_app()
    get_settings.cache_clear()


def _client(app, username: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/auth/login", json={"username": username, "password": _PW})
    assert r.status_code == 200
    c.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return c


def test_api_requires_auth(app) -> None:
    assert TestClient(app).get("/dedup/candidates").status_code == 401


def test_api_refresh_admin_only(app) -> None:
    worker = _client(app, _WORKER)
    assert worker.post("/dedup/refresh").status_code == 403
    admin = _client(app, _ADMIN)
    r = admin.post("/dedup/refresh")
    assert r.status_code == 200
    assert r.json()["created"] == 1


def test_api_list_merge_flow(app) -> None:
    admin = _client(app, _ADMIN)
    admin.post("/dedup/refresh")
    worker = _client(app, _WORKER)
    items = worker.get("/dedup/candidates").json()["items"]
    assert len(items) == 1
    cid = items[0]["id"]
    r = worker.post(f"/dedup/candidates/{cid}/merge")
    assert r.status_code == 200
    assert r.json()["status"] == "merged"
    # 이미 처리된 후보 재머지 → 409.
    assert worker.post(f"/dedup/candidates/{cid}/merge").status_code == 409
    # pending 비고, summary 반영.
    assert worker.get("/dedup/candidates").json()["total"] == 0
    assert worker.get("/dedup/summary").json()["merged"] == 1


def test_api_separate_and_missing(app) -> None:
    admin = _client(app, _ADMIN)
    admin.post("/dedup/refresh")
    worker = _client(app, _WORKER)
    cid = worker.get("/dedup/candidates").json()["items"][0]["id"]
    assert worker.post(f"/dedup/candidates/{cid}/separate").status_code == 200
    assert worker.post("/dedup/candidates/nope/separate").status_code == 404
