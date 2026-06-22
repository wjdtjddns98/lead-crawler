"""검증 웹앱 API 입출력 스키마 (Pydantic v2)."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class ReviewStatus(str, Enum):
    """검증 큐 상태 — 쿼리 필터 검증용(잘못된 값은 FastAPI 가 422)."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ReviewItem(BaseModel):
    """검증 큐 한 항목(회사·이메일검증 정보 평탄화)."""

    id: str
    company_id: str
    field: str
    candidates: list[str]
    status: str
    assignee: str | None = None
    name: str
    country: str = ""
    industry: str = ""
    homepage: str | None = None
    site_alive: bool = False
    # 이메일 연락처가 있을 때의 검증 신호(없으면 None).
    email_status: str | None = None
    email_mx: bool | None = None
    email_smtp: bool | None = None


class QueueResponse(BaseModel):
    """큐 목록 응답(페이지네이션 메타 포함)."""

    items: list[ReviewItem]
    total: int
    limit: int
    offset: int


class ActionRequest(BaseModel):
    """확정/거부 요청 본문(담당자 선택)."""

    assignee: str | None = None
