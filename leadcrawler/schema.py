"""SQLAlchemy 2.0 테이블 정의 (PostgreSQL 운영, 테스트는 SQLite).

ERD 문서와 1:1 대응. PG 전용 타입을 피해 SQLite 로도 ``create_all`` 가능하게 한다.
운영(24/7 병렬·재크롤)을 고려해:
- 자식 FK 는 ``ON DELETE CASCADE`` (부모 삭제 시 고아행 방지),
- 조회 경로(FK 컬럼·논리키)에 인덱스,
- 회사 논리키 ``canonical_key`` 는 ``UNIQUE`` 로 중복 회사 행을 DB 레벨에서 차단,
- NOT NULL + 기본값 컬럼엔 ``server_default`` (ORM 우회 raw insert 안전).

빈 문자열 컨벤션: country/industry/source 등은 "미상"을 ``''`` 로 표기한다(NULL 아님).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """선언적 베이스."""


class DiscoveredCompanyRow(Base):
    __tablename__ = "discovered_company"

    canonical_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(512))
    country: Mapped[str] = mapped_column(String(8), default="", server_default=text("''"))
    industry: Mapped[str] = mapped_column(String(128), default="", server_default=text("''"))
    listed: Mapped[str] = mapped_column(
        String(16), default="unknown", server_default=text("'unknown'")
    )
    registry: Mapped[str | None] = mapped_column(String(32), nullable=True)
    registry_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    segment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"))
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    last_crawled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CompanyRow(Base):
    __tablename__ = "company"
    __table_args__ = (UniqueConstraint("canonical_key", name="uq_company_canonical_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    canonical_key: Mapped[str] = mapped_column(
        ForeignKey("discovered_company.canonical_key"), index=True
    )
    name: Mapped[str] = mapped_column(String(512))
    country: Mapped[str] = mapped_column(String(8), default="", server_default=text("''"))
    industry: Mapped[str] = mapped_column(String(128), default="", server_default=text("''"))
    homepage: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    existence_confidence: Mapped[float] = mapped_column(
        Float, default=0.0, server_default=text("0")
    )
    site_alive: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class ContactRow(Base):
    __tablename__ = "contact"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(16))
    value: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="unknown", server_default=text("'unknown'"))
    extract_method: Mapped[str] = mapped_column(
        String(16), default="static", server_default=text("'static'")
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0, server_default=text("0"))


class EmailValidationRow(Base):
    __tablename__ = "email_validation"

    contact_id: Mapped[str] = mapped_column(
        ForeignKey("contact.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), default="unknown", server_default=text("'unknown'"))
    mx: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    domain_match: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # SMTP RCPT 프로브 결과(nullable): True=수신확정, False=없음, NULL=미시도/판정불가.
    smtp: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CostLedgerRow(Base):
    """유료 외부 호출 1건의 과금 기록 — 월 예산(monthly_budget_krw) 추적용.

    ``month_key``(YYYY-MM)에 인덱스를 둬 월 누계 집계를 빠르게 한다. 실제 호출이
    일어난 건만 적재한다(dry_run·무료 경로는 행 없음).
    """

    __tablename__ = "cost_ledger"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    units: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    unit_cost_krw: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cost_krw: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    month_key: Mapped[str] = mapped_column(String(7), index=True)


class ReviewQueueRow(Base):
    __tablename__ = "review_queue"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )
    field: Mapped[str] = mapped_column(String(32))
    candidates: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default=text("'pending'"))
    # 마지막 처리자 username(비정규 표시용 — 계정 삭제/개명에도 이력 흔적 보존).
    assignee: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 마지막 처리자 계정 FK — 계정 삭제 시 NULL(귀속은 사라져도 review_audit 이력은 남음).
    assignee_id: Mapped[str | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 마지막 상태 변경(확정/거부) 시각 — "누가"에 더해 "언제"를 큐 행에 기록.
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 동시 처리 점유 — 어느 직원이 이 항목을 당겨갔는지(배타 배정). 6명 동시 검증 시 충돌
    # 방지. claimed_at 기준 TTL 경과하면 미처리 점유는 풀로 복귀(다른 직원이 가져감).
    claimed_by: Mapped[str | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 사람이 고른 최종 이메일 후보(candidates 중 1건). 미선택이면 NULL(=기본 대표 사용).
    selected: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # 선택을 사람이 명시했는지. False(자동 기본값)면 재크롤마다 best 로 갱신, True 면 보존.
    selected_by_human: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )


class ReviewAuditRow(Base):
    """검증 처리 감사 로그 — confirm/reject 1건마다 append-only 적재(불변 이력).

    review_queue 는 '현재 상태'만 보유해 재처리 시 이전 처리자가 덮인다. 이 테이블은
    누가·언제·무엇을(확정/거부, 선택 이메일) 했는지 전 이력을 남겨 책임추적을 보장한다.
    actor 계정이 삭제돼도 ``actor_username`` 스냅샷으로 이력은 보존된다(FK 는 SET NULL).
    """

    __tablename__ = "review_audit"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("review_queue.id", ondelete="CASCADE"), index=True
    )
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 처리 시점의 username 스냅샷(계정 삭제 후에도 '누가' 가 남도록 비정규 보관).
    actor_username: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    action: Mapped[str] = mapped_column(String(16))  # confirmed | rejected
    selected: Mapped[str | None] = mapped_column(String(320), nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )


class CrawlTargetRow(Base):
    """다음 크롤 타깃 — 웹앱 관리자가 클릭으로 설정, 스케줄러가 매일 읽는다.

    단일행(id='current')으로 "현재 타깃"을 보관한다. 행이 없거나 비면 스케줄러는 .env
    (report_*) 로 폴백한다. 국가·업종은 쉼표구분 CSV, listed 는 unknown/listed/unlisted.
    """

    __tablename__ = "crawl_target"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # 단일행 'current'
    countries: Mapped[str] = mapped_column(String(256), default="", server_default=text("''"))
    industries: Mapped[str] = mapped_column(String(512), default="", server_default=text("''"))
    listed: Mapped[str] = mapped_column(
        String(16), default="unknown", server_default=text("'unknown'")
    )
    persist: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class CrawlJobRow(Base):
    """직접 크롤 작업 1건의 상태·진행 카운터 — 웹앱에서 트리거, 진행현황 폴링용.

    스케줄러(매일 1회)와 달리 관리자가 웹에서 즉시 돌리는 단발 크롤. 백그라운드 스레드가
    이 행의 카운터(발견/처리/저장·세그먼트 진행)를 갱신하고, 프론트가 주기 폴링으로 표시한다.
    ``cancel_requested`` 를 켜면 실행 스레드가 다음 기업 처리 전에 협조적으로 멈춘다.
    status: running | done | failed | cancelled. started_at 인덱스로 '최근 작업'을 빠르게 찾는다.
    """

    __tablename__ = "crawl_job"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # 'cj_' + uuid12
    status: Mapped[str] = mapped_column(
        String(16), default="running", server_default=text("'running'"), index=True
    )
    countries: Mapped[str] = mapped_column(String(256), default="", server_default=text("''"))
    industries: Mapped[str] = mapped_column(String(512), default="", server_default=text("''"))
    listed: Mapped[str] = mapped_column(
        String(16), default="unknown", server_default=text("'unknown'")
    )
    persist: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    segments_total: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    segments_done: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    discovered: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    enriched: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    saved: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 협조적 취소 플래그 — 다른 트랜잭션(취소 요청)이 켜면 실행 스레드가 다음 폴링에서 중단.
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    triggered_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserRow(Base):
    """검증 웹앱 직원 계정 — 로그인·assignee 식별. 비밀번호는 scrypt 해시만 저장."""

    __tablename__ = "app_user"  # 'user' 는 PG 예약어라 회피.

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # 권한 — 'admin'(계정관리·엑셀 export) | 'worker'(검증 처리만). 기본 worker.
    role: Mapped[str] = mapped_column(String(16), default="worker", server_default=text("'worker'"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )


class AuthSessionRow(Base):
    """로그인 세션 — 불투명 토큰의 sha256 만 저장(평문 미보관)."""

    __tablename__ = "auth_session"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class EmailSendLogRow(Base):
    """아웃리치 발송 로그 — 수신주소당 1행(재발송 방지 + 발송 이력/책임추적).

    PK 는 이메일 주소에서 결정적으로 파생해, 같은 주소에 두 번 발송하지 않도록 한다
    (status='sent' 면 스킵). 실패(status='failed')는 재시도 시 덮어쓴다. dry-run 은
    status='dryrun' 으로 남겨 미리보기 추적(실발송 카운트와 구분).
    """

    __tablename__ = "email_send_log"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # 'e_' + sha1(email)
    email: Mapped[str] = mapped_column(String(255), index=True)
    company_id: Mapped[str] = mapped_column(String(40), index=True)
    subject: Mapped[str] = mapped_column(String(512), default="", server_default=text("''"))
    status: Mapped[str] = mapped_column(String(16), index=True)  # sent | failed | dryrun
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sent_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), index=True
    )
