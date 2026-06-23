"""검증 웹앱 API 입출력 스키마 (Pydantic v2)."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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
    reviewed_at: str | None = None  # 마지막 처리(확정/거부) 시각 ISO8601(미처리면 None)
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
    role: str = "worker"


class MeResponse(BaseModel):
    """현재 로그인 사용자 정보(프론트 권한 분기용)."""

    username: str
    role: str


class UserStatsItem(BaseModel):
    """관리자 화면의 계정 1행 — 권한·활성 + 처리 통계."""

    id: str
    username: str
    role: str
    is_active: bool
    created_at: str | None = None
    confirmed: int = 0
    rejected: int = 0
    last_action_at: str | None = None


class CreateUserRequest(BaseModel):
    """계정 생성 요청(관리자 전용)."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    role: str = "worker"


class RoleUpdateRequest(BaseModel):
    """역할 변경 요청(관리자 전용)."""

    role: str


class AuditEntry(BaseModel):
    """검증 처리 감사 로그 1건."""

    id: str
    review_id: str
    actor_username: str
    action: str
    selected: str | None = None
    company_name: str = ""
    at: str | None = None


class CrawlTargetInfo(BaseModel):
    """현재 크롤 타깃(스케줄러가 매일 읽는 값) — 관리자 화면 표시·폼 초기값."""

    countries: str = ""  # 쉼표구분 ISO2(빈값=지원 전체국)
    industries: str = ""  # 쉼표구분 업종
    listed: str = "unknown"  # unknown(전체) | listed(상장) | unlisted(비상장)
    persist: bool = True
    updated_by: str | None = None
    updated_at: str | None = None


class CrawlTargetRequest(BaseModel):
    """크롤 타깃 설정 요청(관리자 전용). 업종은 최소 1개, listed 는 3종 중 하나."""

    countries: str = ""
    industries: str = Field(min_length=1, max_length=512)
    listed: Literal["unknown", "listed", "unlisted"] = "unknown"
    persist: bool = True

    @field_validator("industries", mode="before")
    @classmethod
    def _strip_industries(cls, v: object) -> object:
        # 공백만 입력("   ")이 min_length 를 통과해 빈 타깃으로 저장→.env 폴백되는 갱
        # 을 막는다(트림 후 min_length 검증 → 빈 업종은 422).
        return v.strip() if isinstance(v, str) else v
