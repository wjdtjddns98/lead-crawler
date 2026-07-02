"""검증 웹앱 FastAPI 앱 — 직원용 검증 워크벤치 백엔드.

검증 큐 조회 → 후보 확정/거부 → 확정분 엑셀 export 의 최소 라우터를 제공한다.
``fastapi`` 는 선택적 extra(``api``) 이므로 미설치 시 이 모듈은 import 되지 않고,
기본 테스트는 건너뛴다. DB 는 로컬 자원이라 ``dry_run`` 과 무관하게 사용한다.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

from typing import Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from .. import __version__
from ..config import get_settings
from ..logging import get_logger
from ..outreach import preview as outreach_preview
from ..outreach import send_campaign
from ..schema import CompanyRow, ReviewQueueRow, UserRow
from ..sources.countries import country_match_set, korean_label, supported_countries
from ..sources.taxonomy import INDUSTRY_TAXONOMY, UNCLASSIFIED
from ..storage.db import get_engine, get_sessionmaker
from ..storage.export import ExcelExporter
from ..storage.repository import load_leads, register_edited_email
from ..storage.review import (
    CONFIRMED,
    REJECTED,
    ReviewConflict,
    claim_work,
    count_reviews,
    get_review,
    my_work,
    query_reviews,
    set_review_status,
)
from .admin import register_admin
from .auth import make_require_admin, make_require_user, register_auth
from .dedup import register_dedup
from .schemas import (
    ClaimRequest,
    ConfirmRequest,
    CountryOption,
    IndustryOption,
    QueueFilterOptions,
    QueueResponse,
    ReviewItem,
    ReviewStatus,
    SendPreview,
    SendRequest,
    SendResult,
)

# 상장 필터 화이트리스트 — 쿼리/본문 검증용(빈값=전체).
_ListedFilter = Literal["", "listed", "unlisted", "unknown"]

log = get_logger("api")


def get_db() -> Iterator[Session]:
    """요청 단위 DB 세션 의존성(commit/rollback/close)."""
    session = get_sessionmaker(get_settings())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_app() -> FastAPI:
    """FastAPI 앱 인스턴스를 생성한다."""
    app = FastAPI(title="lead-crawler 검증 웹앱", version=__version__)
    # 당겨가기(claim) 배타성은 PG 의 FOR UPDATE SKIP LOCKED 에 의존한다 — SQLite 는 행잠금이
    # 없어 다중 사용자 동시 점유에서 충돌이 날 수 있다. 운영(다중 직원)은 반드시 PostgreSQL.
    if get_engine(get_settings()).dialect.name != "postgresql":
        log.warning("api.sqlite_no_concurrency_guard")  # 멀티유저면 PG 필수.
    # 인증: /health·/auth/login 외 모든 데이터 라우트는 require_user 로 보호.
    require_user = make_require_user(get_db)
    require_admin = make_require_admin(require_user)  # 관리자 전용(계정관리·export).
    register_auth(app, get_db, require_user)
    register_admin(app, get_db, require_admin)
    register_dedup(app, get_db, require_user, require_admin)  # 중복후보 워크벤치(C4).

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/queue", response_model=QueueResponse)
    def list_queue(
        status: ReviewStatus | None = Query(default=None, description="상태 필터"),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        country: str = Query(default="", description="쉼표구분 국가(ISO2/별칭), 빈값=전체"),
        industry: str = Query(default="", description="쉼표구분 업종, 빈값=전체"),
        listed: _ListedFilter = Query(default="", description="상장여부, 빈값=전체"),
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> QueueResponse:
        """검증 큐 항목을 조회한다(상태·국가/업종/상장 작업범위 필터·페이지네이션).

        점유(claim) 중인 행은 목록·``total`` 에서 제외된다(전체큐 = 아직 아무도 안
        받아간 작업). ``total`` 도 동일 필터를 반영해 '이 범위 잔여건수' 표시에 쓴다.
        """
        status_val = status.value if status is not None else None
        countries = _split_csv(country)
        industries = _split_csv(industry)
        listed_val = listed or None
        items = query_reviews(
            db, status=status_val, limit=limit, offset=offset,
            countries=countries, industries=industries, listed=listed_val,
        )
        return QueueResponse(
            items=[ReviewItem(**it) for it in items],
            total=count_reviews(
                db, status=status_val,
                countries=countries, industries=industries, listed=listed_val,
            ),
            limit=limit,
            offset=offset,
        )

    @app.get("/queue/filters", response_model=QueueFilterOptions)
    def queue_filters(
        user: UserRow = Depends(require_user),
    ) -> QueueFilterOptions:
        """작업범위 필터 옵션(직원 접근) — 국가/업종/상장 셀렉트 단일 출처.

        ``/admin/*`` 과 동일 출처지만 직원(worker)도 필요하므로 admin 라우트를 오염시키지
        않고 비관리자 경로로 노출한다(상장여부는 고정 3값).

        구분(업종) 옵션은 크롤 타깃용 ``supported_industries()`` 가 아니라 큐 행에 실제
        저장되는 **구분 택소노미**(:data:`INDUSTRY_TAXONOMY` + 미분류)다 — 필터 매칭은
        ``CompanyRow.industry`` 문자열 일치이므로 저장 어휘와 같아야 0건 매치가 안 난다.
        """
        return QueueFilterOptions(
            countries=[
                CountryOption(iso2=c.iso2, label=korean_label(c), aliases=list(c.aliases))
                for c in supported_countries()
            ],
            industries=[
                IndustryOption(value=label, label=label)
                for label in (*INDUSTRY_TAXONOMY, UNCLASSIFIED)
            ],
            listed=["listed", "unlisted", "unknown"],
        )

    @app.post("/queue/claim", response_model=list[ReviewItem])
    def claim_queue(
        payload: ClaimRequest = Body(default_factory=ClaimRequest),
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> list[ReviewItem]:
        """새 작업을 배치(+30)만큼 추가 점유하고 내 작업분 전체를 반환한다(총량 cap 상한).

        "작업 받기" 1회 = +batch. 남은 작업이 있어도 다른 세그먼트 지시를 받아 미리
        받아둘 수 있다(선취 — cap 도달 시 신규 배정 0). 점유는 처리(확정/거부) 전까지
        계정에 영구 귀속(반납·TTL 복귀 없음 — 회수는 관리자 ``/admin/users/{id}/reclaim``).
        본문 ``ClaimRequest`` 의 국가/업종/상장 작업범위는 **신규 배정에만** 적용되고,
        응답엔 필터 무관 내 점유 전체가 담긴다. 부작용 없는 조회는 ``GET /queue/mine``.
        """
        s = get_settings()
        items = claim_work(
            db, user.id, batch=s.review_claim_batch, cap=s.review_claim_cap,
            countries=_split_csv(payload.country), industries=_split_csv(payload.industry),
            listed=payload.listed or None,
        )
        return [ReviewItem(**it) for it in items]

    @app.get("/queue/mine", response_model=list[ReviewItem])
    def my_queue(
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> list[ReviewItem]:
        """내 점유 작업분 조회 — 부작용 없음(새로고침·재로그인 복원용, 추가 점유는 claim)."""
        return [ReviewItem(**it) for it in my_work(db, user.id)]

    @app.get("/queue/{review_id}", response_model=ReviewItem)
    def get_queue_item(
        review_id: str,
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> ReviewItem:
        """단건 검증 항목."""
        item = get_review(db, review_id)
        if item is None:
            raise HTTPException(status_code=404, detail="검증 항목을 찾을 수 없습니다")
        return ReviewItem(**item)

    @app.post("/queue/{review_id}/confirm", response_model=ReviewItem)
    def confirm(
        review_id: str,
        body: ConfirmRequest | None = None,
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> ReviewItem:
        """후보를 확정한다(발송 대상 확정). 담당자=로그인 사용자, 선택 이메일 기록.

        ``selected`` 가 기존 후보에 없는 값이면 사람이 직접 입력/수정한 이메일로 보고
        연락처+후보로 등록한 뒤 확정한다(오타 교정·이메일 추가). 형식 오류는 400.
        """
        selected = body.selected if body else None
        if selected and selected.strip():
            selected = selected.strip()
            try:
                register_edited_email(db, review_id, selected)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            selected = None
        return _set_status(db, review_id, CONFIRMED, user, selected=selected)

    @app.post("/queue/{review_id}/reject", response_model=ReviewItem)
    def reject(
        review_id: str,
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> ReviewItem:
        """후보를 거부한다(발송 제외). 담당자=로그인 사용자."""
        return _set_status(db, review_id, REJECTED, user)

    @app.get("/export")
    def export(
        country: str = Query(default="", description="쉼표구분 국가(ISO2) 필터, 빈값=전체"),
        industry: str = Query(default="", description="쉼표구분 업종 필터, 빈값=전체"),
        db: Session = Depends(get_db),
        _admin: UserRow = Depends(require_admin),
    ) -> FileResponse:
        """확정(confirmed) 리드를 고정 12컬럼 엑셀로 내려받는다(관리자 전용).

        ``country``/``industry`` 로 국가·업종별 선택 추출(빈값=전체). 국가는 별칭까지
        대소문자 무시 매칭('KR'↔'대한민국'), 업종은 대소문자 무시 매칭.
        """
        stmt = (
            select(ReviewQueueRow.company_id)
            .join(CompanyRow, ReviewQueueRow.company_id == CompanyRow.id)
            .where(ReviewQueueRow.status == CONFIRMED)
        )
        countries = _split_csv(country)
        industries = _split_csv(industry)
        if countries:
            stmt = stmt.where(func.lower(CompanyRow.country).in_(country_match_set(countries)))
        if industries:
            stmt = stmt.where(func.lower(CompanyRow.industry).in_({i.lower() for i in industries}))
        company_ids = list(db.scalars(stmt).all())
        leads = load_leads(db, company_ids=company_ids)
        # 요청마다 고유 임시파일 — 동시 export 의 파일 경합 방지. 응답 후 삭제.
        fd, tmp = tempfile.mkstemp(prefix="leadcrawler_", suffix=".xlsx")
        os.close(fd)
        ExcelExporter().export(leads, tmp)
        return FileResponse(
            tmp,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="leads_confirmed.xlsx",
            background=BackgroundTask(os.unlink, tmp),
        )

    @app.get("/send/preview", response_model=SendPreview)
    def send_preview(
        country: str = Query(default=""),
        industry: str = Query(default=""),
        db: Session = Depends(get_db),
        _admin: UserRow = Depends(require_admin),
    ) -> SendPreview:
        """발송 전 미리보기 — 수신 N명·일일 잔여 상한·표본(실발송 없음, 관리자 전용)."""
        result = outreach_preview(
            get_settings(), db,
            countries=_split_csv(country), industries=_split_csv(industry),
        )
        return SendPreview(**result)

    @app.post("/send", response_model=SendResult)
    def send(
        payload: SendRequest,
        db: Session = Depends(get_db),
        admin: UserRow = Depends(require_admin),
    ) -> SendResult:
        """확정큐 대상 전체발송(관리자 전용). email_send_enabled 가 꺼져 있으면 dry-run.

        제목·본문·발신표시명은 사람 입력. 수신주소당 1통(재발송 방지)·일일 상한·레이트리밋.
        """
        result = send_campaign(
            get_settings(), db,
            subject=payload.subject, body=payload.body, from_display=payload.from_display,
            countries=_split_csv(payload.country), industries=_split_csv(payload.industry),
            sent_by=admin.username,
        )
        return SendResult(**result)

    return app


def _split_csv(value: str) -> list[str]:
    """쉼표구분 문자열을 트림된 토큰 목록으로(빈 토큰 제거)."""
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def _set_status(
    db: Session, review_id: str, status: str, actor: UserRow, *, selected: str | None = None
) -> ReviewItem:
    """상태 변경 공통 — 담당자=로그인 사용자. 404/후보밖 400/타인점유 409 + 감사기록."""
    try:
        item = set_review_status(
            db,
            review_id,
            status,
            assignee=actor.username,
            assignee_id=actor.id,
            selected=selected,
        )
    except ReviewConflict as exc:  # 타인이 점유 중 → 409(영구 배정 — 시간 경과 무관).
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:  # 후보에 없는 selected → 400.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="검증 항목을 찾을 수 없습니다")
    return ReviewItem(**item)


# 모듈 레벨 별칭 — `uvicorn leadcrawler.api.app:app` 표준 명령이 그대로 동작하게 한다.
# 팩토리(create_app)는 테스트용으로 유지하고, 여기서는 기본 설정으로 앱 한 개를 만든다.
app = create_app()
