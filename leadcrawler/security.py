"""인증 코어 — 비밀번호 해시(scrypt)와 DB 백엔드 세션 토큰(불투명).

**새 의존성 없이 표준 라이브러리만** 쓴다(``hashlib.scrypt``·``secrets``·``hmac``).
- 비밀번호: scrypt 로 솔트+해시. 저장 포맷 ``scrypt$N$r$p$salt_b64$hash_b64``.
- 세션: 난수 토큰(평문)은 로그인 응답으로 **한 번만** 반환하고, DB 엔 토큰의 sha256 만
  저장한다(DB 유출 시에도 토큰 원문 복원 불가). 만료(expires_at)는 검증 시점에 확인.

fastapi 에 의존하지 않으므로 CLI(사용자 생성)와 API(로그인) 양쪽에서 재사용한다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .schema import AuthSessionRow, UserRow

# 권한 역할.
ROLE_ADMIN = "admin"
ROLE_WORKER = "worker"
_VALID_ROLES = frozenset({ROLE_ADMIN, ROLE_WORKER})


def validate_role(role: str) -> str:
    """역할 문자열을 검증해 그대로 반환한다(허용 외면 ValueError). 검증 단일화용."""
    if role not in _VALID_ROLES:
        raise ValueError(f"허용되지 않은 역할: {role}")
    return role

# scrypt 파라미터(OWASP 권고 수준). 메모리 ≈ N*r*128 ≈ 16MB(<기본 maxmem 32MB).
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32
_SALT_BYTES = 16


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN)


def hash_password(password: str) -> str:
    """비밀번호를 scrypt 로 해시한다(솔트 포함, 저장 가능한 문자열)."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """평문과 저장된 해시를 상수시간 비교한다(형식 오류·불일치는 False)."""
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = _scrypt(password, salt, int(n), int(r), int(p))
    except Exception:
        return False
    return hmac.compare_digest(actual, expected)


def _token_hash(token: str) -> str:
    """세션 토큰의 sha256 16진수(DB 저장용 — 평문 미보관)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(dt: datetime) -> datetime:
    """SQLite 가 naive 로 돌려주는 datetime 을 UTC aware 로 보정(비교 안전)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# 미존재 사용자에도 항상 scrypt 1회를 돌려 로그인 응답시간을 평탄화한다(사용자 열거
# 타이밍 사이드채널 완화). 모듈 로드 시 1회 계산.
_DUMMY_PASSWORD_HASH = hash_password("timing-equalizer-not-a-real-account")


def count_users(session: Session) -> int:
    """전체 계정 수(부트스트랩 — 첫 계정 자동 admin 판정용)."""
    return int(session.scalar(select(func.count()).select_from(UserRow)) or 0)


def count_admins(session: Session, *, active_only: bool = True) -> int:
    """admin 계정 수(마지막 admin 강등/비활성 방지용)."""
    stmt = select(func.count()).select_from(UserRow).where(UserRow.role == ROLE_ADMIN)
    if active_only:
        stmt = stmt.where(UserRow.is_active.is_(True))
    return int(session.scalar(stmt) or 0)


def create_user(
    session: Session, username: str, password: str, *, role: str = ROLE_WORKER
) -> UserRow:
    """직원 계정을 만든다(username 중복은 DB UNIQUE 가 차단).

    ``role`` 미지정이면 worker. 단, **첫 계정은 자동으로 admin** 으로 승격해 관리자
    부재(락아웃)를 막는다. 유효하지 않은 역할은 ValueError.
    """
    validate_role(role)
    effective_role = ROLE_ADMIN if count_users(session) == 0 else role
    user = UserRow(
        id=uuid4().hex[:12],
        username=username.strip(),  # 앞뒤 공백 제거(프론트 트림과 일관 — 로그인 불일치 방지).
        password_hash=hash_password(password),
        role=effective_role,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def authenticate(session: Session, username: str, password: str) -> UserRow | None:
    """username/비밀번호를 검증해 활성 사용자면 반환, 아니면 None.

    사용자 유무와 무관하게 scrypt 를 항상 1회 수행해 타이밍 차이를 줄인다(열거 완화).
    """
    user = session.scalar(
        select(UserRow).where(UserRow.username == username, UserRow.is_active.is_(True))
    )
    stored = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    ok = verify_password(password, stored)
    if user is None or not ok:
        return None
    return user


def create_session(
    session: Session,
    user_id: str,
    *,
    ttl_hours: int,
    now: datetime | None = None,
) -> str:
    """세션을 만들고 **평문 토큰**을 반환한다(DB 엔 해시만 저장)."""
    now = now or _utcnow()
    token = secrets.token_urlsafe(32)
    session.add(
        AuthSessionRow(
            token_hash=_token_hash(token),
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(hours=max(1, ttl_hours)),
        )
    )
    session.flush()
    return token


def user_for_token(
    session: Session, token: str, *, now: datetime | None = None
) -> UserRow | None:
    """유효(미만료) 세션 토큰이면 해당 활성 사용자를 반환, 아니면 None."""
    if not token:
        return None
    now = now or _utcnow()
    row = session.get(AuthSessionRow, _token_hash(token))
    if row is None or _aware(row.expires_at) <= now:
        return None
    user = session.get(UserRow, row.user_id)
    return user if user is not None and user.is_active else None


def delete_session(session: Session, token: str) -> None:
    """세션을 폐기한다(로그아웃). 없으면 무시."""
    if not token:
        return
    row = session.get(AuthSessionRow, _token_hash(token))
    if row is not None:
        session.delete(row)


def delete_user_sessions(session: Session, user_id: str) -> int:
    """특정 사용자의 모든 세션을 즉시 폐기한다(계정 비활성화 시 토큰 즉시 무효화).

    ``user_for_token`` 이 런타임에 ``is_active`` 를 확인하므로 보안상 이미 차단되지만,
    비활성화 즉시 세션 행을 지워 죽은 토큰이 남지 않게 한다(즉시 무효화 보장)."""
    result = session.execute(delete(AuthSessionRow).where(AuthSessionRow.user_id == user_id))
    return int(result.rowcount or 0)


def delete_expired_sessions(session: Session, *, now: datetime | None = None) -> int:
    """만료된 세션 행을 정리한다(테이블 비대화 방지). 삭제 건수를 반환.

    만료 토큰은 :func:`user_for_token` 가 이미 거부하므로 보안 영향은 없고 위생 목적.
    로그인 시 lazy 로 호출해 무인 24/7 운영에서 행이 단조 증가하지 않게 한다.
    """
    now = now or _utcnow()
    result = session.execute(delete(AuthSessionRow).where(AuthSessionRow.expires_at <= now))
    return int(result.rowcount or 0)
