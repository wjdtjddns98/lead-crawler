"""검증 웹앱 API 입출력 스키마 (Pydantic v2)."""

from __future__ import annotations

from enum import Enum
from typing import Literal
from urllib.parse import urlparse

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
    listed: str = "unknown"  # 상장여부 listed/unlisted/unknown — 원장(발견) 값, FE 컬럼 표시용
    market: str | None = None  # 상장 시장 보드(KOSPI/KOSDAQ/KONEX/NASDAQ/NYSE/OTC/PSE…, 미상=None)
    homepage: str | None = None
    site_alive: bool = False
    form: str | None = None  # 문의폼 URL(이메일 없을 때 폼으로 처리)
    note: str | None = None  # 검수자 기타 메모(문의폼 미발송 사유 등) — 엑셀 L(기타) 컬럼
    has_attachment: bool | None = None  # 첨부파일 유무(검수자 체크, None=미확인)
    manager: str | None = None  # 상대 회사 담당자명(검수자 기입) — 엑셀 H(담당자) 컬럼
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
    region: str = ""  # 쉼표구분 지역(시/도·도시, 대소문자 무시 매칭), 빈값=전체


class ConfirmRequest(BaseModel):
    """확정 요청 본문 — 사람이 고른 최종 이메일(선택)+홈페이지(선택)+문의폼 유무(선택).

    ``homepage`` 는 신뢰불가 입력(사람이 직접 입력하는 URL)이라 스킴(http/https)·호스트
    존재를 형식 검증한다 — 실패 시 FastAPI 가 422. ``None`` = 변경 없음(하위호환),
    빈 문자열("")도 형식 위반이라 422 로 거부된다. ``has_form``(#241) 은 문의폼 유무
    교정값(``None`` = 변경 없음): ``False`` = 폼 없음(저장된 폼 삭제), ``True`` = 폼 있음
    (URL 미상이면 홈페이지를 진입 링크로 저장 — 홈페이지도 없으면 400).
    ``note`` 는 검수자 기타 메모(문의폼 미발송 사유 등, 엑셀 L 컬럼): ``None`` = 변경
    없음, 빈 문자열 = 메모 지움. ``has_attachment`` 는 첨부파일 유무 체크(``None`` = 변경
    없음), ``manager`` 는 상대 회사 담당자명(엑셀 H 컬럼, ``None`` = 변경 없음·빈
    문자열 = 지움) — 검증 UI 의 기타메모 아래 입력란.
    ``remove_emails`` 는 실제로 존재하지 않아 삭제할 이메일 목록(빈 목록/``None`` = 삭제
    없음) — 후보와 연락처에서 지운다. 남은 이메일이 없고 문의폼이 있으면 엑셀 J 가
    "사이트 내 문의폼"이 된다. 삭제 대상은 저장된 값과 대조되므로 형식 검증 대신 개수·
    길이 상한만 둔다(과대 페이로드 차단).
    """

    selected: str | None = None
    homepage: str | None = Field(default=None, max_length=512)
    has_form: bool | None = None
    note: str | None = Field(default=None, max_length=512)
    has_attachment: bool | None = None
    manager: str | None = Field(default=None, max_length=64)
    remove_emails: list[str] | None = Field(default=None, max_length=50)

    @field_validator("note", "manager")
    @classmethod
    def _reject_nul(cls, v: str | None) -> str | None:
        # NUL 바이트는 PG TEXT/VARCHAR 저장 시 오류(500) — 422 로 조기 거절(Codex 리뷰 채택).
        if v is not None and "\x00" in v:
            raise ValueError("제어문자(NUL)는 허용되지 않습니다")
        return v

    @field_validator("homepage")
    @classmethod
    def _validate_homepage(cls, v: str | None) -> str | None:
        if v is None:
            return None
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("올바른 홈페이지 URL 이 아닙니다(http/https, 호스트 필요)")
        return v

    @field_validator("remove_emails")
    @classmethod
    def _check_remove_emails(cls, v: list[str] | None) -> list[str] | None:
        return _check_email_lengths(v)


def _check_email_lengths(v: list[str] | None) -> list[str] | None:
    """삭제 대상 이메일 길이 상한 — 저장된 주소(String(320))보다 길면 422 로 조기 거절."""
    if v is not None and any(len(e) > 320 for e in v):
        raise ValueError("이메일 주소가 너무 깁니다(최대 320자)")
    return v


class RejectRequest(BaseModel):
    """거부 요청 본문(선택) — 실존하지 않는 이메일 삭제만 지원한다.

    본문 없이 호출하면 기존과 동일하게 상태만 거부로 바꾼다(하위호환). ``remove_emails``
    의 의미·상한은 :class:`ConfirmRequest` 와 같다.
    """

    remove_emails: list[str] | None = Field(default=None, max_length=50)

    @field_validator("remove_emails")
    @classmethod
    def _check_remove_emails(cls, v: list[str] | None) -> list[str] | None:
        return _check_email_lengths(v)


class LoginRequest(BaseModel):
    """로그인 요청 본문.

    길이상한만 둔다(과대 페이로드·scrypt 비용 폭증 방지). 하한은 두지 않는다 —
    기존 계정/정책 노출·열거를 피하고 빈 값은 인증에서 자연 거부된다(생성 제약과 동일 상한).
    앞뒤 공백은 트림한다(QA①: 복사·모바일 자동완성이 붙이는 공백으로 로그인 실패 방지 —
    계정 생성도 동일 트림이라 공백 포함 자격증명은 애초에 존재하지 않는다).
    """

    username: str = Field(max_length=64)
    password: str = Field(max_length=256)

    @field_validator("username", "password", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


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
    claimed: int = 0  # 현재 점유 중 pending 건수 — 관리자 회수(reclaim) 판단용.
    last_action_at: str | None = None


class CreateUserRequest(BaseModel):
    """계정 생성 요청(관리자 전용).

    앞뒤 공백은 트림 후 길이 검증한다(QA①: 로그인이 트림하므로 공백 포함 자격증명이
    만들어지면 영원히 로그인 불가 — 생성·로그인 양쪽 동일 규칙으로 불일치 차단).
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    role: str = "worker"

    @field_validator("username", "password", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


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


class ReviewDailyStatsItem(BaseModel):
    """직원 1명의 하루 처리량(확정/거부)."""

    username: str
    confirmed: int = 0
    rejected: int = 0


class ReviewDailyStats(BaseModel):
    """직원별 하루(KST) 처리 통계 — GET /admin/stats/review-daily 응답."""

    date: str  # 집계 일자(YYYY-MM-DD, KST)
    items: list[ReviewDailyStatsItem]


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
    regions: list[str] = []  # 실제 수집된 지역 distinct(정렬) — 빈 목록=지역 데이터 없음
    markets: list[str] = []  # 실제 수집된 시장 보드 distinct(정렬) — 빈 목록=FE 폴백 어휘 사용


class QueueStockRow(BaseModel):
    """세그먼트 1칸의 대기 재고 — (국가, 업종, 상장) 조합의 pending·미점유 수."""

    country: str  # 등록국=ISO2(표기 혼재 접음), 미등록 표기=원문, ''=국가 미상(필터 도달 불가)
    industry: str  # 구분 라벨(빈값은 '미분류'로 접음 — 필터도 '미분류'→빈값 대칭 매칭)
    listed: Literal["listed", "unlisted", "unknown"]  # 빈값/NULL 은 unknown 으로 정규화됨
    n: int = Field(ge=1)  # rows 는 n>0 조합만 담는다(없는 조합 = 0)


class QueueStockResponse(BaseModel):
    """필터 조합별 잔량 집계 — FE 가 필터 옵션에 잔량 뱃지를 달고 0 조합을 비활성한다.

    rows 는 n>0 인 조합만 담는다(없는 조합 = 0). 지역·시장 축은 미포함(조합 폭발 방지 —
    FE 는 이 두 축엔 뱃지를 달지 않는다). total = rows 합 = ``GET /queue?status=pending``
    (무필터) 의 total 과 동치(pending·미점유 전체). 호출 정책: 필터 패널 진입 시 1회 +
    claim/confirm/reject 후 갱신 — **폴링 금지**(호출당 원장 group by, FE 계약).
    """

    rows: list[QueueStockRow]
    total: int = Field(ge=0)


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
    skipped: int = 0  # 동시 캠페인 선점/기발송 스킵(pydantic 이 조용히 드롭하지 않게 명시)
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

    크롤 타깃 저장과 무관하게, 이 요청의 국가/업종/상장/적재로 바로 크롤을 돈다.
    업종은 최소 1개(빈 업종은 전 집계원 대상이라 과도한 발견 방지).
    """

    countries: str = ""
    industries: str = Field(min_length=1, max_length=512)
    listed: Literal["unknown", "listed", "unlisted"] = "unknown"
    persist: bool = True
    # 확보 목표 실존 회사 수 — 라운드 안에서 도달 시 조기 종료. 0=세그먼트 전부 깊게 소진.
    target_count: int = Field(default=0, ge=0)
    # True 면 취소 전까지 1회전(라운드)을 반복하는 연속 크롤(24/7 베이스). False=단발.
    continuous: bool = False
    # KR 지역별 검색 팬아웃 — 'all'=17개 시/도 전부, 또는 쉼표구분('서울,경기').
    # KR 세그먼트에만 적용(다른 국가는 무시), 빈값(기본)=팬아웃 없음.
    regions: str = Field(default="", max_length=512)
    # 발견 모드 — True 면 비싼 이메일 escalation(헤드리스·OCR·이메일API·Vision)을 끄고
    # static 만으로 빠르게 발견·site_alive·큐적재만 한다(불필요 렌더 호출 절감). 무이메일
    # 회사는 별도 채우기 패스(backfill_reenrich)가 나중에 헤드리스/OCR 로 이메일을 채운다.
    discovery_only: bool = False

    @field_validator("industries", mode="before")
    @classmethod
    def _strip_industries(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


class CrawlJobInfo(BaseModel):
    """크롤 작업 현황 — 상태·진행 카운터(웹 폴링 표시용).

    ``status``: idle(작업 없음) | running | done | failed | cancelled. 카운터는
    discovered(중복제외 발견)·enriched(보강완료)·saved(실존 확인분)·segments_done/total.
    ``mode``: once(단발) | continuous(연속 — 취소까지 반복, 카운터는 현재 라운드 기준이고
    ``rounds_done`` 이 완료 라운드 수).
    """

    id: str | None = None
    status: str = "idle"
    countries: str = ""
    industries: str = ""
    listed: str = "unknown"
    persist: bool = True
    # 실행옵션 스냅샷 — pydantic 은 미선언 키를 조용히 버리므로(extra=ignore) 명시 필수.
    target_count: int = 0
    regions: str = ""
    discovery_only: bool = False
    segments_total: int = 0
    segments_done: int = 0
    discovered: int = 0
    enriched: int = 0
    saved: int = 0
    mode: str = "once"
    rounds_done: int = 0
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


class BackfillStartRequest(BaseModel):
    """딸깍 백필 시작(#352) — 조건 하나로 C(도메인해석)·A(이메일) 두 트랙 자동 가동.

    트랙은 내부 구현 개념이라 요청에 노출하지 않는다. 조건 전부 기본값이면 전세계
    전체 대상(진짜 원클릭). 값 형식은 쉼표구분 CSV(crawl_target 관례).
    """

    countries: str = Field(default="", max_length=256)
    # 포함식(이 업종만, '미분류'=라벨 빈값) — exclude_industries 와 동시 지정 시 422(#372).
    industries: str = Field(default="", max_length=1024)
    exclude_industries: str = Field(default="", max_length=1024)
    exclude_listed: bool = False


class BackfillJobInfo(BaseModel):
    """백필 작업 현황(트랙 1개분) — status: idle(작업 없음) | running | failed |
    cancelled | budget_exhausted. 지속형 consumer 라 '완료'는 없다(대상 소진=대기)."""

    id: str | None = None
    track: str = ""
    status: str = "idle"
    countries: str = ""
    industries: str = ""
    exclude_industries: str = ""
    exclude_listed: bool = False
    batch: int = 0
    workers: int = 0
    max_batches: int = 0
    min_queue: int = 0
    initial_target: int = 0
    remaining: int = 0
    processed: int = 0
    resolved: int = 0
    promoted: int = 0
    emails: int = 0
    batches_done: int = 0
    generation: int = 0
    recycles: int = 0
    crash_restarts: int = 0
    pid: int | None = None
    cancel_requested: bool = False
    stop_reason: str | None = None
    error: str | None = None
    triggered_by: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    progress_at: str | None = None
    finished_at: str | None = None


class BackfillStatusResponse(BaseModel):
    """통합 진행 카드 — 트랙별 최신 작업(운영자 화면은 두 카운터를 합쳐 깔때기로)."""

    resolve: BackfillJobInfo  # C 트랙(도메인 해석→승격).
    fill: BackfillJobInfo  # A 트랙(이메일 채우기).


class BackfillOverview(BaseModel):
    """상시 잔여 패널 — 조건 변경 시 즉시 재계산되는 깔때기 카운트.

    queue_pending 은 국가 필터만 반영한다(큐 카운트는 포함식 업종 필터 체계라
    제외식과 불일치 — 화면엔 근사치로 표기).
    """

    resolve_pending: int  # 도메인 없음·미승격(해석 대기).
    fill_pending: int  # 도메인 있음·이메일 없음(채우기 대기).
    queue_pending: int  # 워크벤치 미점유 pending.


class LedgerSummary(BaseModel):
    """발견 원장 4분면 — total = promoted + domained_unpromoted + undomained_unpromoted + absorbed.

    absorbed 는 dedup 이 다른 canonical_key 로 흡수한 행(duplicate_of 기록) — 승격 백로그가
    아니므로 분리 집계한다(백로그 두 값이 실제 소화 대상만 담는다는 계약).
    """

    total: int = Field(ge=0)
    promoted: int = Field(ge=0)  # company 로 승격 완료(실존 확인분).
    domained_unpromoted: int = Field(ge=0)  # 도메인 확정·미승격(promote 백필 대상).
    undomained_unpromoted: int = Field(ge=0)  # 도메인 미확정(resolve 대기).
    absorbed: int = Field(ge=0)  # dedup 흡수분(승격 대상 아님 — 감사용 잔존).


class CountryCount(BaseModel):
    country: str  # 등록국=ISO2 접기, 미등록 표기=원문, ''=국가 미상(queue_stock 과 동일 어휘).
    n: int = Field(ge=0)


class IndustryCount(BaseModel):
    industry: str  # 구분 라벨(빈값은 '미분류'로 접음).
    n: int = Field(ge=0)


class CompaniesSummary(BaseModel):
    """승격(실존) 회사 현황 — 분포는 n 내림차순 정렬.

    발견 원장 없는 고아 company 가 있으면 total 이 ledger.promoted 를 넘을 수 있다
    (queue_stock 과 동일 현상 — 버그 아님, FE 는 두 수를 등치로 그리지 말 것).
    """

    total: int = Field(ge=0)
    with_email: int = Field(ge=0)  # 이메일 연락처 1개 이상 보유 회사 수(distinct).
    by_country: list[CountryCount]
    by_industry: list[IndustryCount]


class QueueSummary(BaseModel):
    """검증 큐 상태별 카운트 — pending 은 점유 여부로 분해."""

    pending_unclaimed: int = Field(ge=0)
    pending_claimed: int = Field(ge=0)
    confirmed: int = Field(ge=0)
    rejected: int = Field(ge=0)


class DashboardSummaryResponse(BaseModel):
    """보유 데이터 대시보드 스냅샷 — 원장·회사·큐 한 응답(2026-08-21 FE 계약).

    호출 정책: 대시보드 진입 시 1회(+수동 새로고침) — 폴링 금지(원장 group by 수회).
    국가·업종 어휘는 /queue/stock 과 동일해 숫자를 큐 필터로 되짚을 수 있다.
    """

    ledger: LedgerSummary
    companies: CompaniesSummary
    queue: QueueSummary
