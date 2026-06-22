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
from ..schema import ReviewQueueRow
from ..storage.db import get_sessionmaker
from ..storage.export import ExcelExporter
from ..storage.repository import load_leads
from ..storage.review import (
    CONFIRMED,
    REJECTED,
    count_reviews,
    get_review,
    query_reviews,
    set_review_status,
)
from .schemas import ActionRequest, QueueResponse, ReviewItem, ReviewStatus


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

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/queue", response_model=QueueResponse)
    def list_queue(
        status: ReviewStatus | None = Query(default=None, description="상태 필터"),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
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

    @app.get("/queue/{review_id}", response_model=ReviewItem)
    def get_queue_item(review_id: str, db: Session = Depends(get_db)) -> ReviewItem:
        """단건 검증 항목."""
        item = get_review(db, review_id)
        if item is None:
            raise HTTPException(status_code=404, detail="검증 항목을 찾을 수 없습니다")
        return ReviewItem(**item)

    @app.post("/queue/{review_id}/confirm", response_model=ReviewItem)
    def confirm(
        review_id: str, body: ActionRequest | None = None, db: Session = Depends(get_db)
    ) -> ReviewItem:
        """후보를 확정한다(발송 대상 확정)."""
        return _set_status(db, review_id, CONFIRMED, body)

    @app.post("/queue/{review_id}/reject", response_model=ReviewItem)
    def reject(
        review_id: str, body: ActionRequest | None = None, db: Session = Depends(get_db)
    ) -> ReviewItem:
        """후보를 거부한다(발송 제외)."""
        return _set_status(db, review_id, REJECTED, body)

    @app.get("/export")
    def export(db: Session = Depends(get_db)) -> FileResponse:
        """확정(confirmed) 리드를 고정 12컬럼 엑셀로 내려받는다."""
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
    db: Session, review_id: str, status: str, body: ActionRequest | None
) -> ReviewItem:
    """상태 변경 공통 — 없으면 404."""
    assignee = body.assignee if body else None
    item = set_review_status(db, review_id, status, assignee=assignee)
    if item is None:
        raise HTTPException(status_code=404, detail="검증 항목을 찾을 수 없습니다")
    return ReviewItem(**item)
