"""중복후보 워크벤치(C4) 라우트 — 사람이 경계 중복쌍을 동일확정/분리한다.

near_dup(C1)/LLM(C2) 사다리가 못 가른 경계 쌍만 ``dedup_candidate`` 에서 읽어 직원이
'동일 확정(merge)' 또는 '분리(둘 다 유지)'를 결정한다. 동일확정은 골든레코드
survivorship 으로 ``duplicate_of`` 를 기록(가역·감사). 목록·결정은 로그인 직원,
재적재(refresh)는 관리자 전용. refresh 는 결정적 연산이라 ``dry_run`` 과 무관하게
네트워크·과금 없이 동작한다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import get_settings
from ..dedup_resolve.report import build_report, load_company_records
from ..logging import get_logger
from ..schema import UserRow
from ..storage import dedup_candidate as wb
from ..storage.db import get_sessionmaker
from .schemas import (
    DedupCandidateItem,
    DedupCandidateList,
    DedupRefreshResult,
    DedupRefreshStatus,
    DedupSummary,
)

log = get_logger("api.dedup")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_refresh(session: Session) -> dict:
    """경계 중복쌍 재산정 + 워크벤치 멱등 적재(결정적·무과금). 호출자가 세션 수명 관리."""
    records = load_company_records(session)
    report = build_report(records)
    stats = wb.populate_candidates(session, report)
    return {
        "total_candidates": report.total_candidates,
        "total_records": report.total_records,
        **stats,
    }


class _RefreshJob:
    """관리자 /dedup/refresh 의 백그라운드 실행 상태(단일 프로세스 인메모리).

    대용량 원장 비교는 수 초 걸려 요청 스레드를 막으므로 별도 스레드에서 돌리고, 상태는
    폴링(/dedup/refresh/status)으로 노출한다. running 중 재시작 요청은 거부(동시 중복 차단).
    워커는 요청 세션을 쓰지 못하므로(응답 후 닫힘) 자체 세션을 연다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = "idle"  # idle | running | done | error
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._error: str | None = None
        self._result: dict | None = None

    def start(self) -> bool:
        """실행을 시작한다. 이미 running 이면 False(동시 중복 차단)."""
        with self._lock:
            if self._status == "running":
                return False
            self._status = "running"
            self._started_at = _now_iso()
            self._finished_at = None
            self._error = None
            self._result = None
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        # 세션 생성도 try 안 — 실패(DB 접속 불가 등) 시에도 반드시 _finish 로 상태를 풀어
        # status 가 "running" 에 영구 고착(이후 모든 요청 409)되는 것을 막는다.
        session = None
        try:
            session = get_sessionmaker(get_settings())()
            result = _run_refresh(session)
            session.commit()
            self._finish(status="done", result=result)
        except Exception as exc:  # 실패해도 워커만 죽고 앱은 유지 — 상태에 사유 기록.
            if session is not None:
                session.rollback()
            log.warning("dedup.refresh.error", err=str(exc))
            self._finish(status="error", error=str(exc))
        finally:
            if session is not None:
                session.close()

    def _finish(self, *, status: str, result: dict | None = None, error: str | None = None) -> None:
        with self._lock:
            self._status = status
            self._result = result
            self._error = error
            self._finished_at = _now_iso()

    def snapshot(self) -> DedupRefreshStatus:
        with self._lock:
            return DedupRefreshStatus(
                status=self._status,
                started_at=self._started_at,
                finished_at=self._finished_at,
                error=self._error,
                result=DedupRefreshResult(**self._result) if self._result else None,
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

    refresh_job = _RefreshJob()

    @app.post("/dedup/refresh", response_model=DedupRefreshStatus, status_code=202)
    def refresh_candidates(
        _admin: UserRow = Depends(require_admin),
    ) -> DedupRefreshStatus:
        """near_dup 사다리로 경계 중복쌍을 재산정해 워크벤치에 멱등 적재한다(관리자, 비동기).

        결정적·무과금(LLM 미사용). 이미 결정(merged/separated)된 쌍은 보존된다. 발견 원장
        전건을 읽어 블로킹 비교하므로 대용량에선 수 초 걸려 **백그라운드로 실행**한다 — 202 와
        함께 status="running" 을 즉시 반환하고, 완료는 GET /dedup/refresh/status 로 폴링한다.
        이미 실행 중이면 409(동시 중복 차단). 단 409 가드·상태는 **프로세스 내 인메모리**라
        uvicorn --workers N(다중 프로세스)에선 워커별로 분리된다 — 운영은 단일 워커 전제.
        """
        if not refresh_job.start():
            raise HTTPException(status_code=409, detail="재적재가 이미 실행 중입니다")
        return refresh_job.snapshot()

    @app.get("/dedup/refresh/status", response_model=DedupRefreshStatus)
    def refresh_status(
        _admin: UserRow = Depends(require_admin),
    ) -> DedupRefreshStatus:
        """재적재 백그라운드 작업 상태(idle|running|done|error)를 반환한다(폴링, 관리자)."""
        return refresh_job.snapshot()
