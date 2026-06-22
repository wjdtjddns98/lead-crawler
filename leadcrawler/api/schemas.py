"""검증 웹앱 API 입출력 스키마 (Pydantic v2)."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class ReviewStatus(str, Enum):
    """검증 큐 상태 — 쿼리 필터 검증용(잘못된 값은 FastAPI 가 422)."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class CandidateInfo(BaseModel):
    """이메일 후보 1건 + 그 후보의 검증 신호(다중 후보 선택 UI 용)."""

    value: str
    email_status: str | None = None
    email_mx: bool | None = None
    email_smtp: bool | None = None


class ReviewItem(BaseModel):
    """검증 큐 한 항목(회사·이메일검증 정보 평탄화)."""

    id: str
    company_id: str
    field: str
    candidates: list[CandidateInfo]
    selected: str | None = None  # 사람이 고른 최종 이메일(미선택이면 대표=선두 후보)
    status: str
    assignee: str | None = None
    name: str
    country: str = ""
    industry: str = ""
    homepage: str | None = None
    site_alive: bool = False
    # 선택된 후보의 검증 신호(이메일 컬럼 표시용, 없으면 None).
    email_status: str | None = None
    email_mx: bool | None = None
    email_smtp: bool | None = None


class QueueResponse(BaseModel):
    """큐 목록 응답(페이지네이션 메타 포함)."""

    items: list[ReviewItem]
    total: int
    limit: int
    offset: int


class ConfirmRequest(BaseModel):
    """확정 요청 본문 — 사람이 고른 최종 이메일 후보(선택)."""

    selected: str | None = None


class LoginRequest(BaseModel):
    """로그인 요청 본문."""

    username: str
    password: str


class LoginResponse(BaseModel):
    """로그인 성공 응답 — 평문 토큰은 여기서만 1회 전달."""

    token: str
    username: str
