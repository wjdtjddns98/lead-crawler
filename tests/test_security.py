"""인증 코어 테스트 — scrypt 비밀번호 + DB 세션 토큰(네트워크 없음, SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from leadcrawler.config import Settings
from leadcrawler.security import (
    authenticate,
    create_session,
    create_user,
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
