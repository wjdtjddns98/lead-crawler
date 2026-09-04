"""백필 트랙 실행 잠금 — PG advisory lock 으로 전 실행 경로 중복 방지(#352 PR③).

같은 트랙(A=fill-emails, C=backfill-resolve-domains)을 웹 supervisor 자식·bat 러너·
야간 CLI 가 동시에 돌리면 같은 LIMIT 구간을 중복 처리해 외부 호출·과금이 이중으로
나간다(대상 쿼리에 row claim 이 없음). 트랙별 고정 키의 세션 수준 advisory lock 을
**프로세스 수명 동안 전용 커넥션으로 보유**해 이를 막는다 — 프로세스가 어떻게 죽든
커넥션 종료와 함께 잠금도 풀린다(스테일 락 없음).

SQLite(테스트·로컬)는 advisory lock 이 없어 항상 획득 성공(no-op) — 중복 방지는
운영(PG) 전용 방어선이고, 단위 테스트는 단일 프로세스라 무의미하다.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..logging import get_logger

log = get_logger("storage.track_lock")

# 트랙별 고정 키(64bit 정수 공간에서 임의 고정값 — 프로젝트 내 다른 advisory lock 없음).
_TRACK_LOCK_KEYS = {"A": 852_001, "C": 852_003, "S": 852_005}


class TrackLock:
    """트랙 실행 잠금 핸들 — ``acquire_track_lock`` 로만 생성. close 로 해제."""

    def __init__(self, conn: Connection | None, key: int | None = None) -> None:
        self._conn = conn
        self._key = key

    def close(self) -> None:
        """잠금 해제 후 커넥션 반환. 멱등.

        ``conn.close()`` 는 풀 반환일 뿐이라 세션 수준 advisory lock 이 풀린 커넥션에
        **그대로 남는다**(rollback 에도 생존) — 반드시 명시 unlock 을 먼저 보낸다.
        프로세스 종료 경로는 DBAPI 커넥션 자체가 죽으므로 unlock 없이도 풀린다.
        """
        if self._conn is None:
            return
        try:
            if self._key is not None:
                self._conn.execute(text("select pg_advisory_unlock(:k)"), {"k": self._key})
        finally:
            self._conn.close()
            self._conn = None


def acquire_track_lock(engine: Engine, track: str) -> TrackLock | None:
    """트랙 실행 잠금을 시도한다 — 성공 시 핸들, 이미 점유면 None.

    PG: 전용 커넥션에서 ``pg_try_advisory_lock``(세션 수준, 논블로킹). 반환된 핸들이
    커넥션을 보유하는 동안 잠금이 유지되고, ``close()``/프로세스 종료로 풀린다.
    비-PG(SQLite): 항상 성공(no-op 핸들).
    """
    if track not in _TRACK_LOCK_KEYS:
        raise ValueError(f"허용되지 않은 track: {track}")
    if engine.dialect.name != "postgresql":
        return TrackLock(None)
    key = _TRACK_LOCK_KEYS[track]
    conn = engine.connect()
    try:
        got = conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": key}).scalar()
    except Exception:
        conn.close()  # 잠금 질의 실패 시 커넥션 누수 방지(close 경로와 대칭).
        raise
    if not got:
        conn.close()
        log.info("track_lock.busy", track=track)
        return None
    # autobegin 트랜잭션을 닫는다 — 안 닫으면 커넥션이 "idle in transaction" 으로 수 시간
    # 방치돼 idle_in_transaction_session_timeout 에 잘려 잠금이 조용히 풀린다(중복 실행
    # 재발, 2026-08-18 Codex HIGH-1 실증). 세션 수준 advisory lock 은 commit 후에도 유지.
    conn.commit()
    return TrackLock(conn, key)
