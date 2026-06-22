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
from ..security import authenticate, create_session, delete_session, user_for_token
from .schemas import LoginRequest, LoginResponse


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

    @app.post("/auth/login", response_model=LoginResponse)
    def login(body: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
        user = authenticate(db, body.username, body.password)
        if user is None:
            # username/비밀번호 구분 없이 동일 메시지(사용자 열거 방지).
            raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다")
        token = create_session(db, user.id, ttl_hours=get_settings().web_session_ttl_hours)
        return LoginResponse(token=token, username=user.username)

    @app.post("/auth/logout")
    def logout(
        authorization: str | None = Header(default=None), db: Session = Depends(get_db)
    ) -> dict[str, bool]:
        delete_session(db, _bearer(authorization))
        return {"ok": True}

    @app.get("/auth/me")
    def me(user: UserRow = Depends(require_user)) -> dict[str, str]:
        return {"username": user.username}


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
