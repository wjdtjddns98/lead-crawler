"""인증 코어 테스트 — scrypt 비밀번호 + DB 세션 토큰(네트워크 없음, SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from leadcrawler.config import Settings
from leadcrawler.security import (
    LoginThrottle,
    authenticate,
    create_session,
    create_user,
    delete_expired_sessions,
    delete_session,
    hash_password,
    user_for_token,
    verify_password,
)
from leadcrawler.storage.db import session_scope
from leadcrawler.storage.db import init_db

UTC = timezone.utc


def _settings(tmp_path) -> Settings:
    s = Settings(database_url=f"sqlite:///{tmp_path}/auth.db")
    init_db(s)
    return s


# --- 비밀번호 해시 ----------------------------------------------------

def test_password_roundtrip() -> None:
    h = hash_password("hunter2")
    assert h.startswith("scrypt$")
    assert verify_password("hunter2", h)
    assert not verify_password("hunter3", h)


def test_password_salts_differ() -> None:
    # 같은 비밀번호도 솔트가 달라 해시가 매번 다르다(레인보우 테이블 방지).
    assert hash_password("same") != hash_password("same")


def test_verify_malformed_hash_is_false() -> None:
    assert not verify_password("x", "garbage")
    assert not verify_password("x", "md5$deadbeef")


# --- 인증 + 세션 ------------------------------------------------------

def test_authenticate_success_and_failure(tmp_path) -> None:
    s = _settings(tmp_path)
    with session_scope(s) as db:
        create_user(db, "kim", "pw-correct")
    with session_scope(s) as db:
        assert authenticate(db, "kim", "pw-correct") is not None
        assert authenticate(db, "kim", "pw-wrong") is None
        assert authenticate(db, "ghost", "pw-correct") is None


def test_session_create_and_validate(tmp_path) -> None:
    s = _settings(tmp_path)
    with session_scope(s) as db:
        user = create_user(db, "lee", "pw")
        token = create_session(db, user.id, ttl_hours=12)
    with session_scope(s) as db:
        u = user_for_token(db, token)
        assert u is not None and u.username == "lee"


def test_session_expired_rejected(tmp_path) -> None:
    s = _settings(tmp_path)
    past = datetime(2020, 1, 1, tzinfo=UTC)
    with session_scope(s) as db:
        user = create_user(db, "old", "pw")
        token = create_session(db, user.id, ttl_hours=1, now=past)
    with session_scope(s) as db:
        # 검증 시점(now)이 만료 이후 → None.
        assert user_for_token(db, token, now=past + timedelta(hours=2)) is None


def test_logout_deletes_session(tmp_path) -> None:
    s = _settings(tmp_path)
    with session_scope(s) as db:
        user = create_user(db, "out", "pw")
        token = create_session(db, user.id, ttl_hours=12)
    with session_scope(s) as db:
        delete_session(db, token)
    with session_scope(s) as db:
        assert user_for_token(db, token) is None


def test_unknown_token_is_none(tmp_path) -> None:
    s = _settings(tmp_path)
    with session_scope(s) as db:
        assert user_for_token(db, "nope") is None
        assert user_for_token(db, "") is None


def test_delete_expired_sessions(tmp_path) -> None:
    s = _settings(tmp_path)
    past = datetime(2020, 1, 1, tzinfo=UTC)
    with session_scope(s) as db:
        user = create_user(db, "gc", "pw")
        create_session(db, user.id, ttl_hours=1, now=past)  # 만료될 세션
        live = create_session(db, user.id, ttl_hours=12)  # 유효 세션
    with session_scope(s) as db:
        removed = delete_expired_sessions(db, now=past + timedelta(hours=2))
        assert removed == 1
    with session_scope(s) as db:
        assert user_for_token(db, live) is not None  # 유효 세션은 보존.


def test_create_user_strips_username(tmp_path) -> None:
    s = _settings(tmp_path)
    with session_scope(s) as db:
        create_user(db, "  spaced  ", "pw")
    with session_scope(s) as db:
        assert authenticate(db, "spaced", "pw") is not None  # 트림된 아이디로 로그인.


def test_duplicate_username_raises(tmp_path) -> None:
    from sqlalchemy.exc import IntegrityError

    s = _settings(tmp_path)
    with session_scope(s) as db:
        create_user(db, "dup", "pw")
    with pytest.raises(IntegrityError), session_scope(s) as db:
        create_user(db, "dup", "pw2")


# --- 로그인 무차별대입 스로틀 ----------------------------------------

class _Clock:
    """주입형 시계 — 테스트가 시간을 결정적으로 전진시킨다."""

    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def test_throttle_locks_after_max_failures() -> None:
    clock = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    th = LoginThrottle(max_failures=3, window_seconds=900, now=clock)
    assert th.retry_after("kim") == 0  # 초기 미잠금.
    for _ in range(3):
        assert th.retry_after("kim") == 0
        th.record_failure("kim")
    # 3회 실패 → 잠김(가장 오래된 실패 + 윈도우까지 대기).
    assert th.retry_after("kim") == 900


def test_throttle_unlocks_after_window() -> None:
    clock = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    th = LoginThrottle(max_failures=3, window_seconds=900, now=clock)
    for _ in range(3):
        th.record_failure("kim")
    assert th.retry_after("kim") > 0
    clock.advance(901)  # 가장 오래된 실패가 윈도우 밖 → 임계 미만으로 복귀.
    assert th.retry_after("kim") == 0


def test_throttle_clear_on_success_resets() -> None:
    clock = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    th = LoginThrottle(max_failures=3, window_seconds=900, now=clock)
    th.record_failure("kim")
    th.record_failure("kim")
    th.clear("kim")  # 성공 → 카운트 리셋.
    th.record_failure("kim")
    assert th.retry_after("kim") == 0  # 리셋 후 1회뿐이라 미잠금.


def test_throttle_is_per_username() -> None:
    clock = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    th = LoginThrottle(max_failures=2, window_seconds=900, now=clock)
    th.record_failure("kim")
    th.record_failure("kim")
    assert th.retry_after("kim") > 0  # kim 잠김.
    assert th.retry_after("lee") == 0  # 다른 사용자는 영향 없음.


def test_throttle_key_normalizes_case_and_space() -> None:
    clock = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    th = LoginThrottle(max_failures=2, window_seconds=900, now=clock)
    th.record_failure("Kim")
    th.record_failure("  kim ")  # 대소문자·공백 무시하고 동일 키.
    assert th.retry_after("kim") > 0


def test_throttle_unlocks_exactly_at_window_boundary() -> None:
    # 경계: now == 가장 오래된 실패 + window 이면 strict `>` cutoff 로 해제(off-by-one 방지).
    clock = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    th = LoginThrottle(max_failures=2, window_seconds=900, now=clock)
    th.record_failure("kim")
    th.record_failure("kim")
    assert th.retry_after("kim") == 900  # 막 잠김.
    clock.advance(899)
    assert th.retry_after("kim") == 1  # 1초 남음.
    clock.advance(1)  # 정확히 window 경과 → 해제.
    assert th.retry_after("kim") == 0


def test_throttle_evicts_oldest_key_over_cap(monkeypatch) -> None:
    # 미인증 키 폭증 방어 — _MAX_KEYS 초과 시 가장 오래된 키가 축출돼 메모리가 바운드된다.
    clock = _Clock(datetime(2026, 1, 1, tzinfo=UTC))
    th = LoginThrottle(max_failures=5, window_seconds=10_000, now=clock)
    monkeypatch.setattr(th, "_MAX_KEYS", 3)
    for name in ("a", "b", "c"):
        th.record_failure(name)
        clock.advance(1)  # 각 키의 최근 실패 시각을 다르게(축출 대상 결정적).
    th.record_failure("d")  # 4번째 → cap(3) 초과 → 가장 오래된 'a' 축출.
    assert len(th._fails) == 3
    assert "a" not in th._fails
    assert {"b", "c", "d"} == set(th._fails)
