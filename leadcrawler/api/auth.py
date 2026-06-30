"""검증 웹앱 인증 — Bearer 토큰 의존성 + 로그인/로그아웃/내정보 라우트.

토큰은 ``Authorization: Bearer <token>`` 헤더로 받는다. 핵심 로직은 fastapi 비의존
:mod:`leadcrawler.security` 에 있고, 여기선 FastAPI 배선만 한다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy.orm import Session

from ..config import get_settings
from ..schema import UserRow
from ..security import (
    ROLE_ADMIN,
    LoginThrottle,
    authenticate,
    create_session,
    delete_expired_sessions,
    delete_session,
    user_for_token,
)
from .schemas import LoginRequest, LoginResponse, MeResponse


def _bearer(authorization: str | None) -> str:
    """``Authorization: Bearer <token>`` 에서 토큰만 추출(형식 안 맞으면 빈 문자열)."""
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def register_auth(
    app: FastAPI,
    get_db: Callable[[], Iterator[Session]],
    require_user: Callable[..., UserRow],
) -> None:
    """인증 라우트를 등록한다(로그인·로그아웃·내정보)."""
    settings = get_settings()
    # 무차별대입 완화 — 라우트 등록 시 1회 생성해 요청 간 실패 카운트를 공유(프로세스-로컬).
    throttle = LoginThrottle(
        max_failures=settings.login_max_failures,
        window_seconds=settings.login_failure_window_minutes * 60,
    )

    @app.post("/auth/login", response_model=LoginResponse)
    def login(body: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
        retry_after = throttle.retry_after(body.username)
        if retry_after > 0:
            # 인증(scrypt) 전에 차단 — 잠긴 키엔 CPU 도 안 쓴다. Retry-After 헤더로 안내.
            raise HTTPException(
                status_code=429,
                detail="로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.",
                headers={"Retry-After": str(retry_after)},
            )
        user = authenticate(db, body.username, body.password)
        if user is None:
            throttle.record_failure(body.username)
            # username/비밀번호 구분 없이 동일 메시지(사용자 열거 방지).
            raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다")
        throttle.clear(body.username)  # 성공 → 실패 카운트 리셋(정상 사용자 락아웃 방지).
        delete_expired_sessions(db)  # lazy GC — 만료 세션 정리(테이블 비대화 방지).
        token = create_session(db, user.id, ttl_hours=settings.web_session_ttl_hours)
        return LoginResponse(token=token, username=user.username, role=user.role)

    @app.post("/auth/logout")
    def logout(
        authorization: str | None = Header(default=None), db: Session = Depends(get_db)
    ) -> dict[str, bool]:
        delete_session(db, _bearer(authorization))
        return {"ok": True}

    @app.get("/auth/me", response_model=MeResponse)
    def me(user: UserRow = Depends(require_user)) -> MeResponse:
        return MeResponse(username=user.username, role=user.role)


def make_require_user(
    get_db: Callable[[], Iterator[Session]],
) -> Callable[..., UserRow]:
    """보호 라우트용 의존성 — 유효 토큰이 없으면 401."""

    def require_user(
        authorization: str | None = Header(default=None),
        db: Session = Depends(get_db),
    ) -> UserRow:
        user = user_for_token(db, _bearer(authorization))
        if user is None:
            raise HTTPException(status_code=401, detail="인증이 필요합니다")
        return user

    return require_user


def make_require_admin(
    require_user: Callable[..., UserRow],
) -> Callable[..., UserRow]:
    """관리자 전용 라우트 의존성 — 인증(require_user) 후 role==admin 이 아니면 403.

    require_user 를 그대로 의존성으로 재사용해 토큰 검증을 한 곳에서만 한다.
    """

    def require_admin(user: UserRow = Depends(require_user)) -> UserRow:
        if user.role != ROLE_ADMIN:
            raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")
        return user

    return require_admin
