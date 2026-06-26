"""중복후보 워크벤치(C4) 라우트 — 사람이 경계 중복쌍을 동일확정/분리한다.

near_dup(C1)/LLM(C2) 사다리가 못 가른 경계 쌍만 ``dedup_candidate`` 에서 읽어 직원이
'동일 확정(merge)' 또는 '분리(둘 다 유지)'를 결정한다. 동일확정은 골든레코드
survivorship 으로 ``duplicate_of`` 를 기록(가역·감사). 목록·결정은 로그인 직원,
재적재(refresh)는 관리자 전용. refresh 는 결정적 연산이라 ``dry_run`` 과 무관하게
네트워크·과금 없이 동작한다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.orm import Session

from ..dedup_resolve.report import build_report, load_company_records
from ..schema import UserRow
from ..storage import dedup_candidate as wb
from .schemas import (
    DedupCandidateItem,
    DedupCandidateList,
    DedupRefreshResult,
    DedupSummary,
)


def register_dedup(
    app: FastAPI,
    get_db: Callable[[], Iterator[Session]],
    require_user: Callable[..., UserRow],
    require_admin: Callable[..., UserRow],
) -> None:
    """중복후보 워크벤치 라우트를 등록한다."""

    @app.get("/dedup/candidates", response_model=DedupCandidateList)
    def list_dedup(
        status: str | None = Query(default="pending", description="상태 필터(빈 전체=all)"),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
        _user: UserRow = Depends(require_user),
    ) -> DedupCandidateList:
        """중복후보 목록(양쪽 회사정보 포함). ``status=all`` 이면 전체 상태."""
        st = None if status in ("all", "") else status
        try:
            data = wb.list_candidates(db, status=st, limit=limit, offset=offset)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return DedupCandidateList(**data)

    @app.get("/dedup/summary", response_model=DedupSummary)
    def dedup_summary(
        db: Session = Depends(get_db),
        _user: UserRow = Depends(require_user),
    ) -> DedupSummary:
        """상태별 후보 건수 요약."""
        return DedupSummary(**wb.summary(db))

    @app.post("/dedup/candidates/{candidate_id}/merge", response_model=DedupCandidateItem)
    def merge_candidate(
        candidate_id: str,
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> DedupCandidateItem:
        """동일 확정 — 두 행을 골든레코드로 머지(duplicate_of 기록). 처리자=로그인 직원."""
        try:
            item = wb.decide_merge(db, candidate_id, decided_by=user.username)
        except wb.DedupConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if item is None:
            raise HTTPException(status_code=404, detail="중복후보를 찾을 수 없습니다")
        return DedupCandidateItem(**item)

    @app.post("/dedup/candidates/{candidate_id}/separate", response_model=DedupCandidateItem)
    def separate_candidate(
        candidate_id: str,
        db: Session = Depends(get_db),
        user: UserRow = Depends(require_user),
    ) -> DedupCandidateItem:
        """분리(둘 다 유지) — 동일기업 아님으로 영속 표시. 처리자=로그인 직원."""
        try:
            item = wb.decide_separate(db, candidate_id, decided_by=user.username)
        except wb.DedupConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if item is None:
            raise HTTPException(status_code=404, detail="중복후보를 찾을 수 없습니다")
        return DedupCandidateItem(**item)

    @app.post("/dedup/refresh", response_model=DedupRefreshResult)
    def refresh_candidates(
        db: Session = Depends(get_db),
        _admin: UserRow = Depends(require_admin),
    ) -> DedupRefreshResult:
        """near_dup 사다리로 경계 중복쌍을 재산정해 워크벤치에 멱등 적재한다(관리자).

        결정적·무과금(LLM 미사용). 이미 결정(merged/separated)된 쌍은 보존된다. 발견
        원장 전건을 읽어 블로킹 비교하므로 대용량에선 수 초 걸릴 수 있다(동기 실행).
        """
        records = load_company_records(db)
        report = build_report(records)
        stats = wb.populate_candidates(db, report)
        return DedupRefreshResult(
            total_candidates=report.total_candidates,
            total_records=report.total_records,
            **stats,
        )
