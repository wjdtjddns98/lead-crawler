"""검증 웹앱 FastAPI 앱 — 직원용 검증 워크벤치 백엔드.

검증 큐 조회 → 후보 확정/거부 → 확정분 엑셀 export 의 최소 라우터를 제공한다.
``fastapi`` 는 선택적 extra(``api``) 이므로 미설치 시 이 모듈은 import 되지 않고,
기본 테스트는 건너뛴다. DB 는 로컬 자원이라 ``dry_run`` 과 무관하게 사용한다.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from typing import Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from .. import __version__
from ..config import get_settings
from ..logging import get_logger, patch_proactor_connection_lost
from ..outreach import preview as outreach_preview
from ..outreach import send_campaign
from ..schema import CompanyRow, ReviewQueueRow, UserRow
from ..security import ROLE_ADMIN
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
    list_markets,
    list_regions,
    my_history,
    my_work,
    query_reviews,
    queue_stock,
    set_review_status,
)
from ..storage.dashboard import holdings_summary
from .admin import register_admin
from .auth import make_require_admin, make_require_user, register_auth
from .dedup import register_dedup
from .schemas import (
    ClaimRequest,
    ConfirmRequest,
    CountryOption,
    DashboardSummaryResponse,
    IndustryOption,
    QueueFilterOptions,
    QueueResponse,
    QueueStockResponse,
    QueueStockRow,
    RejectRequest,
    ReviewItem,
    ReviewStatus,
    SendPreview,
    SendRequest,
    SendResult,
)

# 상장 필터 화이트리스트 — 쿼리/본문 검증용(빈값=전체).
_ListedFilter = Literal["", "listed", "unlisted", "unknown"]

# 큐 목록 서버 정렬 키(#238) — storage 화이트리스트(QUEUE_SORT_KEYS)와 동일 어휘, 빈값=기본.
_QueueSortKey = Literal["", "name", "country", "industry", "listed", "form", "status"]

log = get_logger("api")

# 프론트 빌드 산출물(web/dist) 위치 — editable install(리포 루트) 기준. 테스트에서 monkeypatch.
_WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


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


# 백필 재개 1회성 가드 — 모듈 레벨 app=create_app() 과 uvicorn factory 의 이중 호출 대비.
_backfill_resume_done = False


def create_app() -> FastAPI:
    """FastAPI 앱 인스턴스를 생성한다."""
    patch_proactor_connection_lost()  # Windows: 원격 RST 로 끊긴 접속의 종료 트레이스백 억제.
    app = FastAPI(title="lead-crawler 검증 웹앱", version=__version__)
    # 당겨가기(claim) 배타성은 PG 의 FOR UPDATE SKIP LOCKED 에 의존한다 — SQLite 는 행잠금이
    # 없어 다중 사용자 동시 점유에서 충돌이 날 수 있다. 운영(다중 직원)은 반드시 PostgreSQL.
    if get_engine(get_settings()).dialect.name != "postgresql":
        log.warning("api.sqlite_no_concurrency_guard")  # 멀티유저면 PG 필수.
    # 인증: /health·/auth/login 외 모든 데이터 라우트는 require_user 로 보호.
    require_user = make_require_user(get_db)
    require_admin = make_require_admin(require_user)  # 관리자 전용(계정관리·발송 등).
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
        region: str = Query(default="", description="쉼표구분 지역(시/도·도시), 빈값=전체"),
        market: str = Query(default="", description="쉼표구분 시장 보드(KOSPI/KOSDAQ…), 빈값=전체"),
        sort_by: _QueueSortKey = Query(
            default="", description="정렬 컬럼(#238), 빈값=LIFO(최신 크롤분 최상단)"
        ),
        sort_dir: Literal["asc", "desc"] = Query(default="asc", description="정렬 방향"),
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> QueueResponse:
        """검증 큐 항목을 조회한다(상태·국가/업종/상장/지역/시장 작업범위 필터·페이지네이션).

        점유(claim) 중인 행은 목록·``total`` 에서 제외된다(전체큐 = 아직 아무도 안
        받아간 작업). ``total`` 도 동일 필터를 반영해 '이 범위 잔여건수' 표시에 쓴다.
        지역·시장 필터는 미상(주소 없는 소스 유입·시장 미기입) 행을 자연히 제외한다.
        ``sort_by``/``sort_dir``(#238) 는 전체 결과 기준 서버 정렬이라 페이지를 넘겨도
        순서가 일관된다. 미지정 시 기본 = LIFO(status 업무순위 안에서 큐 적재시각
        created_at 역순 — 최신 적재분 최상단, #352 에서 발견 first_seen 근사키 대체).
        """
        status_val = status.value if status is not None else None
        countries = _split_csv(country)
        industries = _split_csv(industry)
        listed_val = listed or None
        regions = _split_csv(region)
        markets = _split_csv(market)
        items = query_reviews(
            db, status=status_val, limit=limit, offset=offset,
            countries=countries, industries=industries, listed=listed_val, regions=regions,
            markets=markets, sort_by=sort_by or None, sort_dir=sort_dir,
        )
        return QueueResponse(
            items=[ReviewItem(**it) for it in items],
            total=count_reviews(
                db, status=status_val,
                countries=countries, industries=industries, listed=listed_val,
                regions=regions, markets=markets,
            ),
            limit=limit,
            offset=offset,
        )

    @app.get("/queue/filters", response_model=QueueFilterOptions)
    def queue_filters(
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> QueueFilterOptions:
        """작업범위 필터 옵션(직원 접근) — 국가/업종/상장/지역 셀렉트 단일 출처.

        ``/admin/*`` 과 동일 출처지만 직원(worker)도 필요하므로 admin 라우트를 오염시키지
        않고 비관리자 경로로 노출한다(상장여부는 고정 3값).

        구분(업종) 옵션은 크롤 타깃용 ``supported_industries()`` 가 아니라 큐 행에 실제
        저장되는 **구분 택소노미**(:data:`INDUSTRY_TAXONOMY` + 미분류)다 — 필터 매칭은
        ``CompanyRow.industry`` 문자열 일치이므로 저장 어휘와 같아야 0건 매치가 안 난다.
        지역 옵션도 같은 이유로 고정 목록이 아니라 실제 수집된 값 distinct(정렬)다.
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
            regions=list_regions(db),
            markets=list_markets(db),
        )

    @app.get("/queue/stock", response_model=QueueStockResponse)
    def queue_stock_report(
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> QueueStockResponse:
        """세그먼트별 대기 재고 집계 — (국가 × 업종 × 상장) 조합의 pending·미점유 수.

        FE 계약(P0, 2026-08-19): 필터 옵션에 잔량 뱃지를 달고 **재고 0 조합을 비활성**해
        작업자가 빈 조합을 골라 "적재된 큐가 없어 못 뽑는" 헛걸음을 없앤다.
        - rows 는 n>0 조합만(없는 조합 = 0). 뱃지의 (country, industry, listed) 값을
          **그대로** ``/queue``·``/queue/claim`` 필터 파라미터로 보내면 같은 수가 나온다
          (왕복 계약 — '미분류' 는 서버가 빈 업종 행으로 대칭 매칭).
        - 국가는 등록국이면 ISO2(``/queue/filters.countries`` 와 동일 어휘), 미등록 표기는
          원문(옵션 목록에 없을 수 있음 — 뱃지 없이 무시 가능), '' 는 국가 미상(필터로
          도달 불가 — 비활성 렌더). 지역·시장 축은 미포함(뱃지 없이 기존 동작 유지).
        - 호출 정책: 필터 패널 진입 시 1회 + claim/confirm/reject 후 갱신, **폴링 금지**
          (호출당 원장 group by ~140ms). 집계는 요청 시점 스냅샷(동시 claim 으로 소폭
          어긋날 수 있음).
        """
        rows = queue_stock(db)
        return QueueStockResponse(
            rows=[QueueStockRow(**r) for r in rows],
            total=sum(r["n"] for r in rows),
        )

    @app.get("/dashboard/summary", response_model=DashboardSummaryResponse)
    def dashboard_summary(
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> DashboardSummaryResponse:
        """보유 데이터 대시보드 스냅샷 — 발견 원장·승격 회사·검증 큐 현황 한 응답.

        FE 계약(2026-08-21): 대시보드 진입 시 1회 호출(+수동 새로고침) — **폴링 금지**
        (원장 group by 수회). 국가(ISO2 접기)·업종('미분류' 접기) 어휘는 ``/queue/stock``
        과 동일해, 대시보드 숫자를 큐 필터 파라미터로 그대로 되짚을 수 있다.
        """
        return DashboardSummaryResponse(**holdings_summary(db))

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
            listed=payload.listed or None, regions=_split_csv(payload.region),
        )
        return [ReviewItem(**it) for it in items]

    @app.get("/queue/mine", response_model=list[ReviewItem])
    def my_queue(
        status: ReviewStatus | None = Query(
            default=None, description="생략=내 점유(pending). confirmed/rejected=처리 이력"
        ),
        limit: int = Query(default=200, ge=1, le=200, description="이력 조회 시 최근 N건"),
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> list[ReviewItem]:
        """내 작업분 조회 — 부작용 없음(새로고침·재로그인 복원용, 추가 점유는 claim).

        ``status`` 생략(또는 ``pending``)은 기존 동작(내 점유 pending 전체) 그대로다.
        ``confirmed``/``rejected`` 는 내가 처리한 해당 상태 이력을 ``reviewed_at``
        최신순으로 최근 ``limit``(기본 200)건 반환한다(#191).
        """
        if status is None or status is ReviewStatus.PENDING:
            return [ReviewItem(**it) for it in my_work(db, user.id)]
        items = my_history(db, user.id, status=status.value, limit=limit)
        return [ReviewItem(**it) for it in items]

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
        ``homepage``(#185) 가 주어지면(``None``=변경 없음) 회사 홈페이지를 갱신한다 —
        URL 형식은 ``ConfirmRequest`` 가 이미 검증했으므로(무효면 422) 여기선 그대로 전달.
        ``has_form``(#241) 은 문의폼 유무 교정값(``None``=변경 없음) — 폼 있음인데
        홈페이지조차 없어 저장할 URL 이 없으면 400. ``note`` 는 검수자 기타 메모
        (문의폼 미발송 사유 등, ``None``=변경 없음·빈 문자열=지움) — 엑셀 L 컬럼.
        ``remove_emails`` 는 실존하지 않아 삭제할 이메일 목록 — 후보·연락처에서 지운다
        (삭제 후 이메일이 없고 폼이 있으면 엑셀 J="사이트 내 문의폼"). 같은 요청의
        ``selected`` 는 삭제 후 남은 후보여야 한다(삭제한 주소를 고르면 400).
        ``has_attachment``/``manager`` 는 첨부파일 유무 체크·담당자명(엑셀 H 컬럼),
        ``phone`` 은 대표 전화 교정(엑셀 C 컬럼·빈 문자열=지움) — 규약은
        :class:`ConfirmRequest` 독스트링 참조(``None``=변경 없음).
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
        homepage = body.homepage if body else None
        has_form = body.has_form if body else None
        note = body.note if body else None
        return _set_status(
            db, review_id, CONFIRMED, user,
            selected=selected, homepage=homepage, has_form=has_form, note=note,
            has_attachment=body.has_attachment if body else None,
            manager=body.manager if body else None,
            phone=body.phone if body else None,
            remove_emails=body.remove_emails if body else None,
        )

    @app.post("/queue/{review_id}/reject", response_model=ReviewItem)
    def reject(
        review_id: str,
        body: RejectRequest | None = None,
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> ReviewItem:
        """후보를 거부한다(발송 제외). 담당자=로그인 사용자.

        ``remove_emails`` 가 주어지면 실존하지 않는 이메일을 후보·연락처에서 삭제한다
        (본문 없이 호출하면 상태만 변경 — 하위호환).
        """
        return _set_status(
            db, review_id, REJECTED, user,
            remove_emails=body.remove_emails if body else None,
        )

    @app.get("/export")
    def export(
        country: str = Query(default="", description="쉼표구분 국가(ISO2) 필터, 빈값=전체"),
        industry: str = Query(default="", description="쉼표구분 업종 필터, 빈값=전체"),
        date_from: str = Query(default="", description="확정/거부 처리일 시작(YYYY-MM-DD, KST)"),
        date_to: str = Query(default="", description="확정/거부 처리일 끝·포함(YYYY-MM-DD, KST)"),
        status: ReviewStatus = Query(
            default=ReviewStatus.CONFIRMED,
            description="confirmed(기본)=확정분, rejected=거부분. pending 은 422",
        ),
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> FileResponse:
        """확정(confirmed) — 또는 ``status=rejected`` 로 거부 — 리드를 고정 12컬럼 엑셀로 내려받는다.

        ``status`` 기본값은 confirmed(하위호환). rejected 는 거부 처리 이력을 같은 서식·같은
        권한 범위·같은 필터(국가·업종·처리일)로 내려받는다(파일명 ``leads_rejected.xlsx``).
        pending 은 처리 이력이 아니므로 422.
        권한 범위(PO 결정 2026-07-14): 관리자=전체 확정분, 일반 사용자(worker)=자기가
        처리한 확정분만(assignee_id 귀속 — ``/queue/mine?status=confirmed`` 와 동일 기준).
        ``country``/``industry`` 로 국가·업종별 선택 추출(빈값=전체). 국가는 별칭까지
        대소문자 무시 매칭('KR'↔'대한민국'), 업종은 대소문자 무시 매칭.
        ``date_from``/``date_to`` 는 확정 처리 시각(``reviewed_at``) 기준 KST 하루 경계
        필터(포함 범위) — 컬럼 도입 전 구데이터(reviewed_at NULL)는 날짜 필터 시 제외된다.
        """
        if status is ReviewStatus.PENDING:
            raise HTTPException(status_code=422, detail="status 는 confirmed 또는 rejected")
        stmt = (
            select(ReviewQueueRow.company_id)
            .join(CompanyRow, ReviewQueueRow.company_id == CompanyRow.id)
            .where(ReviewQueueRow.status == status.value)
        )
        if user.role != ROLE_ADMIN:
            stmt = stmt.where(ReviewQueueRow.assignee_id == user.id)
        countries = _split_csv(country)
        industries = _split_csv(industry)
        if countries:
            stmt = stmt.where(func.lower(CompanyRow.country).in_(country_match_set(countries)))
        if industries:
            stmt = stmt.where(func.lower(CompanyRow.industry).in_({i.lower() for i in industries}))
        start = _kst_day_start_utc(date_from) if date_from else None
        end = _kst_day_start_utc(date_to) + timedelta(days=1) if date_to else None
        if start is not None and end is not None and start >= end:
            raise HTTPException(status_code=422, detail="date_from 이 date_to 보다 늦습니다")
        if start is not None:
            stmt = stmt.where(ReviewQueueRow.reviewed_at >= start)
        if end is not None:
            stmt = stmt.where(ReviewQueueRow.reviewed_at < end)
        company_ids = list(db.scalars(stmt).all())
        leads = load_leads(db, company_ids=company_ids)
        # 요청마다 고유 임시파일 — 동시 export 의 파일 경합 방지. 응답 후 삭제.
        fd, tmp = tempfile.mkstemp(prefix="leadcrawler_", suffix=".xlsx")
        os.close(fd)
        ExcelExporter().export(leads, tmp)
        return FileResponse(
            tmp,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"leads_{status.value}.xlsx",
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

    # 프론트 빌드(web/dist) 정적 서빙 — 내부망 단일 프로세스 배포용(같은 출처라 CORS·
    # VITE_API_BASE 불필요). API 라우트가 먼저 등록돼 있어 마운트가 API 를 가리지 않고,
    # 빌드 디렉터리가 없으면 생략(개발은 vite 프록시 그대로).
    # 조건이 index.html 이 아니라 **디렉터리**인 이유: `vite build --watch`(#141 런처)가
    # 시작할 때 dist 내용물을 비웠다 다시 채우는데, 그 빈 순간에 서버가 뜨면 index 조건은
    # 마운트를 영구 생략해 / 가 404 로 고정된다(실사고). StaticFiles 는 요청마다 디스크를
    # 읽으므로 디렉터리만 있으면 마운트해 두면 빌드가 채워지는 즉시 자가 치유된다.
    if _WEB_DIST.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="web")

    # 크롤실행(즉시 크롤) 워치독은 2026-09-01 제거됨 — 세그먼트 작업 큐로 일원화.

    # 서버 재시작 시 백필 자동 재개(#352) — running 잔존 잡을 세대+1 로 재스폰한다
    # (취소 플래그 우선 재확인은 resume 내부 계약). dry_run 게이트는 start_watchdog 과
    # 동일 관례(테스트·시뮬레이션에서 실 프로세스 스폰 금지). 초기화 실패는 로그만
    # 남기고 서버 기동을 막지 않는다.
    global _backfill_resume_done
    if not get_settings().dry_run and not _backfill_resume_done:
        _backfill_resume_done = True  # create_app 다중 호출(모듈 app + factory) 중복 방지.
        try:
            from ..pipeline.backfill_process import resume_active_jobs, start_segment_ticker

            # 티커를 먼저 — resume 가 예외로 죽어도 반복 회차 시작이 막히지 않게(Codex 리뷰 MED).
            start_segment_ticker(get_settings())  # 반복 잡 회차 시작용 큐 티커(60s 폴링).
            resume_active_jobs(get_settings())
        except Exception as exc:  # pragma: no cover — 재개 실패는 로그로만(기동 우선).
            from ..logging import get_logger

            get_logger("api.app").info("backfill.resume.startup_error", err=str(exc))

    return app


def _split_csv(value: str) -> list[str]:
    """쉼표구분 문자열을 트림된 토큰 목록으로(빈 토큰 제거)."""
    return [t.strip() for t in (value or "").split(",") if t.strip()]


# 날짜 필터 경계 — 관리탭 일별 집계(storage/audit.py)와 동일한 KST 고정 오프셋 기준.
_KST = timezone(timedelta(hours=9))


def _kst_day_start_utc(day: str) -> datetime:
    """YYYY-MM-DD(KST 하루 시작)를 UTC aware datetime 으로. 형식·범위 오류는 422.

    라운드트립 검사로 콤팩트(20260721)·주차(2026-W29-1) 등 비표준 표기를 거부하고,
    극단 연도의 KST→UTC 변환 OverflowError 도 422 로 수렴시킨다(500 방지).
    """
    try:
        d = date.fromisoformat(day)
        if d.isoformat() != day:
            raise ValueError(day)
        return datetime(d.year, d.month, d.day, tzinfo=_KST).astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail="날짜 형식은 YYYY-MM-DD") from exc


def _set_status(
    db: Session,
    review_id: str,
    status: str,
    actor: UserRow,
    *,
    selected: str | None = None,
    homepage: str | None = None,
    has_form: bool | None = None,
    note: str | None = None,
    has_attachment: bool | None = None,
    manager: str | None = None,
    phone: str | None = None,
    remove_emails: list[str] | None = None,
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
            homepage=homepage,
            has_form=has_form,
            note=note,
            has_attachment=has_attachment,
            manager=manager,
            phone=phone,
            remove_emails=remove_emails,
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
