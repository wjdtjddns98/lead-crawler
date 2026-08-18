"""관리자 전용 라우트 — 계정 관리(생성·역할·활성)와 검증 감사 로그 조회.

전 라우트가 ``require_admin`` 의존성으로 보호된다(role==admin 아니면 403). 권한 변경·
비활성화에는 **마지막 관리자 보호** 가드를 둬 관리자 부재(락아웃)를 막는다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..pipeline.background import CrawlBusy, CrawlTooLarge, trigger_crawl_job
from ..schema import CrawlTargetRow, UserRow
from ..security import (
    ROLE_ADMIN,
    count_admins,
    create_user,
    delete_user_sessions,
    validate_role,
)
from ..sources.countries import korean_label, supported_countries
from ..sources.industry import supported_industries
from ..storage.audit import daily_review_stats, recent_audit, user_stats
from ..storage.review import admin_reclaim
from ..storage.crawl_job import (
    active_crawl_job,
    crawl_job_dict,
    latest_crawl_job,
    recent_crawl_jobs,
    request_cancel,
)
from ..storage.crawl_target import get_crawl_target, set_crawl_target
from .schemas import (
    AuditEntry,
    BackfillJobInfo,
    BackfillOverview,
    BackfillStartRequest,
    BackfillStatusResponse,
    CountryOption,
    CrawlJobInfo,
    CrawlJobRequest,
    CrawlTargetInfo,
    CrawlTargetRequest,
    CreateUserRequest,
    IndustryOption,
    ReviewDailyStats,
    RoleUpdateRequest,
    UserStatsItem,
)


def _lock_admin_rows(db: Session) -> None:
    """활성 관리자 집합에 행잠금을 걸어 마지막-관리자 가드의 TOCTOU 경합을 막는다.

    count 직전에 호출하면 동시 요청이 같은 마지막 관리자를 각자 강등/비활성하려는
    경합에서 두 번째 트랜잭션이 첫 커밋까지 대기 후 재평가한다. SQLite(테스트)에선
    ``with_for_update`` 가 무시되지만 단일 라이터라 경합 자체가 없어 무해하다."""
    db.execute(
        select(UserRow.id)
        .where(UserRow.role == ROLE_ADMIN, UserRow.is_active.is_(True))
        .with_for_update()
    ).all()


def register_admin(
    app: FastAPI,
    get_db: Callable[[], Iterator[Session]],
    require_admin: Callable[..., UserRow],
) -> None:
    """관리자 라우트를 등록한다."""

    @app.get("/admin/users", response_model=list[UserStatsItem])
    def list_users(
        db: Session = Depends(get_db),
        _admin: UserRow = Depends(require_admin),
    ) -> list[UserStatsItem]:
        """계정별 권한·활성 + 처리 통계(확정/거부/마지막처리)."""
        return [UserStatsItem(**row) for row in user_stats(db)]

    @app.post("/admin/users", response_model=UserStatsItem, status_code=201)
    def add_user(
        body: CreateUserRequest,
        db: Session = Depends(get_db),
        _admin: UserRow = Depends(require_admin),
    ) -> UserStatsItem:
        """새 직원 계정을 만든다(역할 지정). username 중복은 409."""
        try:
            validate_role(body.role)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            user = create_user(db, body.username, body.password, role=body.role)
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409, detail="이미 존재하는 아이디입니다"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return UserStatsItem(
            id=user.id,
            username=user.username,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at.isoformat() if user.created_at else None,
            confirmed=0,
            rejected=0,
            last_action_at=None,
        )

    @app.post("/admin/users/{user_id}/role", response_model=UserStatsItem)
    def change_role(
        user_id: str,
        body: RoleUpdateRequest,
        db: Session = Depends(get_db),
        admin: UserRow = Depends(require_admin),
    ) -> UserStatsItem:
        """역할을 변경한다. 본인 강등·마지막 활성 관리자 강등은 400(락아웃 방지)."""
        try:
            validate_role(body.role)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        user = db.get(UserRow, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="계정을 찾을 수 없습니다")
        demoting = user.role == ROLE_ADMIN and body.role != ROLE_ADMIN
        # 본인 강등 차단 — set_active 의 본인 비활성 차단과 대칭(실수로 자기 권한 상실 방지).
        if demoting and user.id == admin.id:
            raise HTTPException(status_code=400, detail="본인 계정은 강등할 수 없습니다")
        # 마지막 활성 관리자 강등 거부 — count 전 행잠금으로 동시 강등 경합(TOCTOU) 차단.
        _lock_admin_rows(db)
        if demoting and user.is_active and count_admins(db) <= 1:
            raise HTTPException(status_code=400, detail="마지막 관리자는 강등할 수 없습니다")
        user.role = body.role
        db.flush()
        return _stats_item(db, user)

    @app.post("/admin/users/{user_id}/active", response_model=UserStatsItem)
    def set_active(
        user_id: str,
        active: bool = Query(..., description="활성 여부"),
        db: Session = Depends(get_db),
        admin: UserRow = Depends(require_admin),
    ) -> UserStatsItem:
        """계정을 활성/비활성한다. 본인·마지막 관리자 비활성은 400(락아웃 방지)."""
        user = db.get(UserRow, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="계정을 찾을 수 없습니다")
        if not active:
            if user.id == admin.id:
                raise HTTPException(status_code=400, detail="본인 계정은 비활성화할 수 없습니다")
            # 마지막 활성 관리자 비활성 거부 — count 전 행잠금으로 동시 비활성 경합 차단.
            _lock_admin_rows(db)
            if user.role == ROLE_ADMIN and user.is_active and count_admins(db) <= 1:
                raise HTTPException(
                    status_code=400, detail="마지막 관리자는 비활성화할 수 없습니다"
                )
        user.is_active = active
        if not active:
            delete_user_sessions(db, user.id)  # 비활성 즉시 기존 토큰 폐기.
        db.flush()
        return _stats_item(db, user)

    @app.post("/admin/users/{user_id}/reclaim")
    def reclaim_user_claims(
        user_id: str,
        db: Session = Depends(get_db),
        admin: UserRow = Depends(require_admin),
    ) -> dict[str, int]:
        """해당 계정이 점유한 pending 항목을 전부 풀로 회수한다(영구 배정의 유일한 해제).

        퇴사·장기부재 계정 대응. 회수분은 즉시 다른 직원이 당겨갈 수 있고, 감사 로그에
        action="reclaim" 으로 남는다.
        """
        user = db.get(UserRow, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="계정을 찾을 수 없습니다")
        n = admin_reclaim(db, user_id, actor_id=admin.id, actor_username=admin.username)
        return {"reclaimed": n}

    @app.get("/admin/stats/review-daily", response_model=ReviewDailyStats)
    def review_daily(
        stat_date: date | None = Query(
            default=None, alias="date", description="집계 일자(YYYY-MM-DD, KST 기준·기본 오늘)"
        ),
        db: Session = Depends(get_db),
        _admin: UserRow = Depends(require_admin),
    ) -> ReviewDailyStats:
        """직원별 하루 처리량(확정/거부) — 검수 생산성 조회. reclaim 등 운영 액션 제외."""
        return ReviewDailyStats(**daily_review_stats(db, day=stat_date))

    @app.get("/admin/audit", response_model=list[AuditEntry])
    def audit(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        _admin: UserRow = Depends(require_admin),
    ) -> list[AuditEntry]:
        """최근 검증 처리 이력(누가·언제·무엇), 최신순."""
        return [AuditEntry(**row) for row in recent_audit(db, limit=limit, offset=offset)]

    @app.get("/admin/countries", response_model=list[CountryOption])
    def list_countries(
        _admin: UserRow = Depends(require_admin),
    ) -> list[CountryOption]:
        """크롤이 지원하는 국가 목록(우선순위 순) — 타깃 국가 선택 UI 의 단일 출처."""
        return [
            CountryOption(iso2=c.iso2, label=korean_label(c), aliases=list(c.aliases))
            for c in supported_countries()
        ]

    @app.get("/admin/industries", response_model=list[IndustryOption])
    def list_industries(
        _admin: UserRow = Depends(require_admin),
    ) -> list[IndustryOption]:
        """선택 가능한 표준 업종 목록 — 타깃 업종 선택 UI 의 단일 출처.

        기본은 코드/검색어 매핑으로 풀리는 라벨(supported_industries). 여기에
        3층 통합매핑(ksic_industry_map — NPS 발견이 서빙 가능)이 보유한 라벨을
        합집합으로 추가해, 매핑이 채워지면 택소노미 42 전체가 자동 노출된다
        (FE 계약: 응답 스키마 동일·옵션만 증가하는 additive 변경).
        """
        from ..sources.industry import industry_search_term
        from ..storage.db import get_sessionmaker
        from ..storage.nps import NpsStore
        from ..sources.taxonomy import INDUSTRY_TAXONOMY

        base = {ko: en for ko, en in supported_industries()}
        mapped = NpsStore(get_sessionmaker()).mapped_labels()
        # 택소노미 정의 순서 유지 — base ∪ 3층 매핑 라벨.
        return [
            IndustryOption(
                value=label, label=label, aliases=[base.get(label) or industry_search_term(label)]
            )
            for label in INDUSTRY_TAXONOMY
            if label in base or label in mapped
        ]

    @app.get("/admin/crawl-target", response_model=CrawlTargetInfo)
    def read_crawl_target(
        db: Session = Depends(get_db),
        _admin: UserRow = Depends(require_admin),
    ) -> CrawlTargetInfo:
        """현재 크롤 타깃을 반환한다(미설정이면 .env 기본값으로 폼 초기값 제공)."""
        row = get_crawl_target(db)
        if row is not None:
            return _target_info(row)
        s = get_settings()  # DB 미설정 → 스케줄러가 폴백하는 .env 값을 그대로 표시.
        return CrawlTargetInfo(
            countries=s.report_countries,
            industries=s.report_industries,
            listed="unknown",
            persist=s.report_persist,
        )

    @app.put("/admin/crawl-target", response_model=CrawlTargetInfo)
    def update_crawl_target(
        body: CrawlTargetRequest,
        db: Session = Depends(get_db),
        admin: UserRow = Depends(require_admin),
    ) -> CrawlTargetInfo:
        """다음 크롤 타깃을 설정한다(관리자). listed 는 unknown/listed/unlisted."""
        try:
            row = set_crawl_target(
                db,
                countries=body.countries,
                industries=body.industries,
                listed=body.listed,
                persist=body.persist,
                updated_by=admin.username,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _target_info(row)

    @app.post("/admin/crawl", response_model=CrawlJobInfo, status_code=202)
    def start_crawl(
        body: CrawlJobRequest,
        admin: UserRow = Depends(require_admin),
    ) -> CrawlJobInfo:
        """폼 입력값으로 즉시 크롤을 돌린다(관리자, 백그라운드 실행).

        타깃 저장과 무관하게 이 요청값으로 바로 실행한다. ``continuous=true`` 면 취소
        전까지 라운드를 반복하는 연속 크롤(24/7 베이스). 진행현황은 GET /admin/crawl 로
        폴링한다. 이미 진행 중이면 409, 세그먼트 상한 초과면 422.
        """
        try:
            info = trigger_crawl_job(
                get_settings(),
                countries=body.countries,
                industries=body.industries,
                listed=body.listed,
                persist=body.persist,
                triggered_by=admin.username,
                target_count=body.target_count,
                continuous=body.continuous,
                regions=body.regions,
                discovery_only=body.discovery_only,
            )
        except CrawlBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CrawlTooLarge as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return CrawlJobInfo(**info)

    @app.get("/admin/crawl", response_model=CrawlJobInfo)
    def crawl_status(
        db: Session = Depends(get_db),
        _admin: UserRow = Depends(require_admin),
    ) -> CrawlJobInfo:
        """가장 최근 크롤 작업의 상태·진행 카운터(없으면 idle)."""
        row = latest_crawl_job(db)
        if row is None:
            return CrawlJobInfo()  # 작업 이력 없음 → idle.
        return CrawlJobInfo(**crawl_job_dict(row))

    @app.get("/admin/crawl/history", response_model=list[CrawlJobInfo])
    def crawl_history(
        limit: int = Query(default=20, ge=1, le=100),
        db: Session = Depends(get_db),
        _admin: UserRow = Depends(require_admin),
    ) -> list[CrawlJobInfo]:
        """최근 크롤 작업 이력(최신순, 최대 limit 건)."""
        return [CrawlJobInfo(**crawl_job_dict(row)) for row in recent_crawl_jobs(db, limit)]

    @app.post("/admin/crawl/cancel", response_model=CrawlJobInfo)
    def cancel_crawl(
        db: Session = Depends(get_db),
        _admin: UserRow = Depends(require_admin),
    ) -> CrawlJobInfo:
        """진행 중 크롤에 취소를 요청한다(실행 스레드가 다음 기업 전 협조적 중단). 없으면 404."""
        row = active_crawl_job(db)
        if row is None:
            raise HTTPException(status_code=404, detail="진행 중인 크롤이 없습니다")
        request_cancel(db, row.id)
        # 실행 스레드가 그 사이 종료했을 수 있으니 재조회해 정확한 현재 상태를 돌려준다.
        db.refresh(row)
        return CrawlJobInfo(**crawl_job_dict(row))

    # ------------------------------------------------------------------
    # 백필 제어(#352) — 딸깍 원칙: 조건 하나로 C(도메인해석)·A(이메일) 자동 병행.
    # 트랙은 내부 개념이라 요청엔 없고, 응답에서만 resolve/fill 로 구분 표기한다.
    # ------------------------------------------------------------------

    def _backfill_filters(countries: str, exclude_industries: str, exclude_listed: bool):
        """CSV 요청값 → count 함수 인자(쉼표 분해·빈값 None 수렴)."""
        from .app import _split_csv

        c = _split_csv(countries) or None
        e = _split_csv(exclude_industries) or None
        return c, e, bool(exclude_listed)

    def _track_info(settings, track: str) -> BackfillJobInfo:  # noqa: ANN001
        from ..pipeline.backfill_process import backfill_status

        info = backfill_status(settings, track)
        return BackfillJobInfo(**info) if info else BackfillJobInfo(track=track)

    @app.get("/admin/backfill/overview", response_model=BackfillOverview)
    def backfill_overview(
        countries: str = Query(default=""),
        exclude_industries: str = Query(default=""),
        exclude_listed: bool = Query(default=False),
        db: Session = Depends(get_db),
        _admin: UserRow = Depends(require_admin),
    ) -> BackfillOverview:
        """상시 잔여 깔때기 — 조건 변경 시 즉시 재계산(시작 전 미리보기 겸용).

        FE 계약: **필터 변경·수동 새로고침 시에만 호출**(초 단위 폴링 금지 — 카운트가
        대형 조인 풀스캔이라 폴링은 status 쪽으로). 진행 중 잔여는 status.remaining 사용.
        """
        from ..pipeline.fill import count_resolve_targets, count_targets
        from ..storage.db import get_sessionmaker
        from ..storage.review import count_reviews

        c, e, x = _backfill_filters(countries, exclude_industries, exclude_listed)
        sm = get_sessionmaker(get_settings())
        return BackfillOverview(
            resolve_pending=count_resolve_targets(sm, c, exclude_industries=e, exclude_listed=x),
            fill_pending=count_targets(sm, c, exclude_industries=e, exclude_listed=x),
            queue_pending=count_reviews(db, status="pending", countries=c),
        )

    @app.post("/admin/backfill/start", response_model=BackfillStatusResponse, status_code=202)
    def backfill_start(
        body: BackfillStartRequest,
        admin: UserRow = Depends(require_admin),
    ) -> BackfillStatusResponse:
        """백필 시작(딸깍) — C·A 두 트랙을 같은 조건으로 함께 가동한다.

        어느 한 트랙이라도 이미 활성이면 409(먼저 중지 후 재시작). C 만 성공하고
        A 가 경쟁으로 막히면 C 를 되돌려 부분 가동을 남기지 않는다.
        """
        from ..pipeline.backfill_process import request_stop, start_backfill
        from ..pipeline.fill import count_resolve_targets, count_targets
        from ..storage.backfill_job import BackfillBusy, active_backfill_job
        from ..storage.db import get_sessionmaker

        settings = get_settings()
        sm = get_sessionmaker(settings)
        with sm() as s:
            active = [t for t in ("A", "C") if active_backfill_job(s, t) is not None]
        if active:
            raise HTTPException(
                status_code=409, detail=f"활성 백필 존재(트랙 {', '.join(active)})"
            )
        c, e, x = _backfill_filters(
            body.countries, body.exclude_industries, body.exclude_listed
        )
        kwargs = {
            "countries": body.countries.strip(),
            "exclude_industries": body.exclude_industries.strip(),
            "exclude_listed": bool(body.exclude_listed),
            "triggered_by": admin.username,
        }
        try:
            resolve_job = start_backfill(
                settings, track="C",
                initial_target=count_resolve_targets(
                    sm, c, exclude_industries=e, exclude_listed=x
                ),
                **kwargs,
            )
        except BackfillBusy as exc:
            raise HTTPException(status_code=409, detail="트랙 C 활성 작업 존재") from exc
        try:
            start_backfill(
                settings, track="A",
                initial_target=count_targets(sm, c, exclude_industries=e, exclude_listed=x),
                **kwargs,
            )
        except Exception as exc:
            # BackfillBusy 뿐 아니라 **어떤 실패든** C 를 되돌린다 — 아니면 500 과 함께
            # C 만 고아 running 으로 남는다(2026-08-18 Codex 리뷰 HIGH-1).
            request_stop(settings, str(resolve_job["id"]))  # 부분 가동 방지 — C 되돌림.
            if isinstance(exc, BackfillBusy):
                raise HTTPException(
                    status_code=409,
                    detail="트랙 A 활성 작업 존재 — 기존 가동을 비동기로 되감는 중이니"
                    " 잠시 후 재시도",
                ) from exc
            raise
        return BackfillStatusResponse(
            resolve=_track_info(settings, "C"), fill=_track_info(settings, "A")
        )

    @app.get("/admin/backfill/status", response_model=BackfillStatusResponse)
    def backfill_status_route(
        _admin: UserRow = Depends(require_admin),
    ) -> BackfillStatusResponse:
        """통합 진행 카드 — 트랙별 최신 작업(이력 없으면 idle)."""
        settings = get_settings()
        return BackfillStatusResponse(
            resolve=_track_info(settings, "C"), fill=_track_info(settings, "A")
        )

    @app.post("/admin/backfill/stop", response_model=BackfillStatusResponse)
    def backfill_stop(
        _admin: UserRow = Depends(require_admin),
    ) -> BackfillStatusResponse:
        """백필 중지 — 활성 작업 전부에 취소 요청(supervisor 가 Job 트리 종료·마감).

        활성이 하나도 없으면 404. 응답은 요청 직후 상태(마감은 수 초 내 비동기 완료).
        """
        from ..pipeline.backfill_process import request_stop
        from ..storage.backfill_job import active_backfill_job
        from ..storage.db import get_sessionmaker

        settings = get_settings()
        sm = get_sessionmaker(settings)
        with sm() as s:
            targets = [
                row.id for t in ("A", "C")
                if (row := active_backfill_job(s, t)) is not None
            ]
        if not targets:
            raise HTTPException(status_code=404, detail="활성 백필이 없습니다")
        for job_id in targets:
            request_stop(settings, job_id)
        return BackfillStatusResponse(
            resolve=_track_info(settings, "C"), fill=_track_info(settings, "A")
        )


def _target_info(row: CrawlTargetRow) -> CrawlTargetInfo:
    """크롤 타깃 행 → DTO(시각 ISO8601)."""
    return CrawlTargetInfo(
        countries=row.countries,
        industries=row.industries,
        listed=row.listed,
        persist=row.persist,
        updated_by=row.updated_by,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


def _stats_item(db: Session, user: UserRow) -> UserStatsItem:
    """단일 계정의 최신 통계 행(역할/활성 변경 응답용)."""
    for row in user_stats(db):
        if row["id"] == user.id:
            return UserStatsItem(**row)
    # 통계 목록에 없으면(이론상 도달불가) 기본값으로 구성.
    return UserStatsItem(
        id=user.id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )
