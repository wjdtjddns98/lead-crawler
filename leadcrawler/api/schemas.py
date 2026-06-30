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
    form: str | None = None  # 문의폼 URL(이메일 없을 때 폼으로 처리)
    form_confidence: float | None = None  # 폼 신뢰도(없으면 None)
    form_low_confidence: bool = False  # 저신뢰 폴백 폼(사람 확인 필요) — 리뷰레인 표기용
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


class ClaimRequest(BaseModel):
    """당겨가기 작업범위 필터(전부 선택, 빈값=전체) — 직원이 스스로 거는 세션 필터.

    국가/업종은 ``/export``·``/send`` 와 동일한 쉼표구분 CSV 규약을 재사용한다. ``listed`` 는
    화이트리스트 검증(잘못된 값은 FastAPI 가 422). 본문 생략/빈 객체 = 전체(하위호환).
    """

    country: str = ""  # 쉼표구분 ISO2/별칭(country_match_set 로 별칭 확장)
    industry: str = ""  # 쉼표구분 업종(대소문자 무시 매칭)
    listed: Literal["", "listed", "unlisted", "unknown"] = ""  # 빈값=전체


class ConfirmRequest(BaseModel):
    """확정 요청 본문 — 사람이 고른 최종 이메일 후보(선택)."""

    selected: str | None = None


class LoginRequest(BaseModel):
    """로그인 요청 본문.

    길이상한만 둔다(과대 페이로드·scrypt 비용 폭증 방지). 하한은 두지 않는다 —
    기존 계정/정책 노출·열거를 피하고 빈 값은 인증에서 자연 거부된다(생성 제약과 동일 상한).
    """

    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


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


class CountryOption(BaseModel):
    """지원 국가 1건 — 크롤 타깃 국가 선택 UI(검색+리스트)용."""

    iso2: str  # 저장값(쉼표구분 ISO2 의 한 토큰)
    label: str  # 한글 표시명
    aliases: list[str] = []  # 검색용 별칭(영문/ISO3/한글 — 'UK'→영국 등 매칭)


class IndustryOption(BaseModel):
    """선택 가능한 표준 업종 1건 — 크롤 타깃 업종 선택 UI(검색+리스트)용.

    이 목록에서만 고르게 해, 자유 텍스트 입력이 매핑을 빗나가 업종 필터가 풀리는 것을 막는다.
    """

    value: str  # 저장값(쉼표구분 업종의 한 토큰, 한글 표준 업종명)
    label: str  # 표시명(한글)
    aliases: list[str] = []  # 검색용 별칭(영문 — 'construction'→건설 등 매칭)


class QueueFilterOptions(BaseModel):
    """작업범위 필터 옵션(직원 접근) — 국가/업종/상장 셀렉트의 단일 출처.

    옵션 출처는 ``/admin/countries``·``/admin/industries`` 와 동일하나, 직원(worker)도
    필요하므로 admin 라우트를 오염시키지 않고 별도 비관리자 경로로 노출한다.
    """

    countries: list[CountryOption]
    industries: list[IndustryOption]
    listed: list[str]  # 고정 3값(listed/unlisted/unknown)


class SendPreview(BaseModel):
    """발송 미리보기 — 수신 N명·일일 잔여·발신계정·표본(실발송 없음)."""

    recipients: int
    enabled: bool  # email_send_enabled — false 면 dry-run(실발송 차단)
    daily_cap: int
    remaining_today: int
    sender: str = ""
    sample: list[str] = []


class SendRequest(BaseModel):
    """확정큐 전체발송 요청 — 제목·본문·발신표시명은 사람이 직접 입력."""

    subject: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1)
    from_display: str = ""  # 발신 표시명(From 주소는 인증 계정으로 고정)
    country: str = ""  # 쉼표구분 국가 필터(빈값=전체)
    industry: str = ""  # 쉼표구분 업종 필터(빈값=전체)


class SendResult(BaseModel):
    """발송 결과 요약."""

    dry_run: bool
    recipients: int
    attempted: int = 0
    sent: int = 0
    failed: int = 0
    capped: int = 0  # 일일 상한 초과로 미발송


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


class CrawlJobRequest(BaseModel):
    """직접 크롤 실행 요청(관리자 전용) — 폼 즉석 입력값으로 즉시 크롤.

    크롤 타깃 저장과 무관하게, 이 요청의 국가/업종/상장/적재로 바로 1회전 크롤을 돈다.
    업종은 최소 1개(빈 업종은 전 집계원 대상이라 과도한 발견 방지).
    """

    countries: str = ""
    industries: str = Field(min_length=1, max_length=512)
    listed: Literal["unknown", "listed", "unlisted"] = "unknown"
    persist: bool = True
    # 확보 목표 실존 회사 수 — 도달 시 조기 종료. 0=세그먼트 전부 깊게 소진(continuous).
    target_count: int = Field(default=0, ge=0)

    @field_validator("industries", mode="before")
    @classmethod
    def _strip_industries(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


class CrawlJobInfo(BaseModel):
    """크롤 작업 현황 — 상태·진행 카운터(웹 폴링 표시용).

    ``status``: idle(작업 없음) | running | done | failed | cancelled. 카운터는
    discovered(중복제외 발견)·enriched(보강완료)·saved(실존 확인분)·segments_done/total.
    """

    id: str | None = None
    status: str = "idle"
    countries: str = ""
    industries: str = ""
    listed: str = "unknown"
    persist: bool = True
    segments_total: int = 0
    segments_done: int = 0
    discovered: int = 0
    enriched: int = 0
    saved: int = 0
    error: str | None = None
    cancel_requested: bool = False
    triggered_by: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None


class DedupCandidateItem(BaseModel):
    """중복후보 1쌍 — 양쪽 회사정보 + 사다리/LLM 근거 + 사람 결정 상태."""

    id: str
    key_a: str
    key_b: str
    name_a: str | None = None
    name_b: str | None = None
    country: str = ""
    domain_a: str | None = None
    domain_b: str | None = None
    tier: str  # domain | lexical | shortlist
    name_score: float = 0.0
    reason: str = ""
    llm_same: bool | None = None
    llm_confidence: float | None = None
    llm_reason: str | None = None
    llm_model: str | None = None
    status: str  # pending | merged | separated
    survivor_key: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    stale: bool = False  # 한쪽이 원장에서 사라짐 — 머지 불가(새로고침 유도)


class DedupCandidateList(BaseModel):
    """중복후보 목록 응답(페이지네이션 메타 포함)."""

    items: list[DedupCandidateItem]
    total: int
    limit: int
    offset: int


class DedupSummary(BaseModel):
    """워크벤치 대시보드 — 상태별 후보 건수."""

    pending: int = 0
    merged: int = 0
    separated: int = 0
    total: int = 0


class DedupRefreshResult(BaseModel):
    """후보 재적재 결과 — near_dup 사다리로 경계쌍을 멱등 적재(네트워크·과금 없음)."""

    created: int = 0
    updated: int = 0
    skipped: int = 0
    total_candidates: int = 0  # 리포트가 찾은 전체 후보(워크벤치 적재 대상 외 포함)
    total_records: int = 0  # 비교 대상 발견 레코드 수


class DedupRefreshStatus(BaseModel):
    """후보 재적재 백그라운드 작업 상태(폴링) — 대용량서 요청을 막지 않게 비동기 실행."""

    status: str  # idle | running | done | error
    started_at: str | None = None  # ISO8601(UTC)
    finished_at: str | None = None
    error: str | None = None  # status==error 일 때 사유
    result: DedupRefreshResult | None = None  # status==done 일 때 적재 결과
