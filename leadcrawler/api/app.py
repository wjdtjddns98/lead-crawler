"""검증 웹앱 FastAPI 앱 — 직원용 검증 워크벤치 백엔드.

검증 큐 조회 → 후보 확정/거부 → 확정분 엑셀 export 의 최소 라우터를 제공한다.
``fastapi`` 는 선택적 extra(``api``) 이므로 미설치 시 이 모듈은 import 되지 않고,
기본 테스트는 건너뛴다. DB 는 로컬 자원이라 ``dry_run`` 과 무관하게 사용한다.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from .. import __version__
from ..config import get_settings
from ..logging import get_logger
from ..schema import ReviewQueueRow, UserRow
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
    query_reviews,
    release_my_claims,
    set_review_status,
)
from .admin import register_admin
from .auth import make_require_admin, make_require_user, register_auth
from .schemas import ConfirmRequest, QueueResponse, ReviewItem, ReviewStatus

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

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/queue", response_model=QueueResponse)
    def list_queue(
        status: ReviewStatus | None = Query(default=None, description="상태 필터"),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> QueueResponse:
        """검증 큐 항목을 조회한다(상태 필터·페이지네이션)."""
        status_val = status.value if status is not None else None
        items = query_reviews(db, status=status_val, limit=limit, offset=offset)
        return QueueResponse(
            items=[ReviewItem(**it) for it in items],
            total=count_reviews(db, status=status_val),
            limit=limit,
            offset=offset,
        )

    @app.post("/queue/claim", response_model=list[ReviewItem])
    def claim_queue(
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> list[ReviewItem]:
        """내 작업분을 배치 크기까지 채워 반환한다(당겨가기 — 6명 동시 충돌 방지·자동 리필)."""
        s = get_settings()
        items = claim_work(
            db, user.id, target=s.review_claim_batch, ttl_minutes=s.review_claim_ttl_minutes
        )
        return [ReviewItem(**it) for it in items]

    @app.post("/queue/release")
    def release_queue(
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> dict[str, int]:
        """내가 점유한 미처리 항목을 풀로 반납한다(작업 종료)."""
        return {"released": release_my_claims(db, user.id)}

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
        db: Session = Depends(get_db),
        _admin: UserRow = Depends(require_admin),
    ) -> FileResponse:
        """확정(confirmed) 리드를 고정 12컬럼 엑셀로 내려받는다(관리자 전용)."""
        company_ids = list(
            db.scalars(
                select(ReviewQueueRow.company_id).where(ReviewQueueRow.status == CONFIRMED)
            ).all()
        )
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

    return app


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
            claim_ttl_minutes=get_settings().review_claim_ttl_minutes,
        )
    except ReviewConflict as exc:  # 타인이 활성 점유 중 → 409(동시성 백스톱).
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:  # 후보에 없는 selected → 400.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="검증 항목을 찾을 수 없습니다")
    return ReviewItem(**item)
