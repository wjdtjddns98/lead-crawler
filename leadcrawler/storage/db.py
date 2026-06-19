"""DB 엔진·세션 관리.

운영/로컬 개발은 PostgreSQL(psycopg), 단위 테스트는 SQLite 를 쓴다(schema 가 PG 전용
타입을 피해 양립하도록 설계됨). 연결 문자열은 :class:`Settings.database_url` 에서 주입한다.
``dry_run`` 여부와 무관하게 DB 는 로컬 자원이므로 사용 가능하다 — 단, 파이프라인의
영속화는 명시적 ``persist`` 옵션으로만 일어난다.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ..config import Settings, get_settings
from ..schema import Base

# 캐시로 생성된 엔진 추적(dispose 용). lru_cache 는 내부값을 노출하지 않으므로 별도 보관.
_ENGINES: list[Engine] = []


def _enable_sqlite_fk(engine: Engine) -> Engine:
    """SQLite 의 FK 강제를 켠다(기본 OFF — 운영 PG 와 동일하게 참조무결성 검증)."""

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_conn: object, _rec: object) -> None:
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


@lru_cache
def _engine_for(url: str) -> Engine:
    """연결 문자열별로 캐시된 엔진을 만든다(SQLite/PG 분기)."""
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        # 메모리 DB 는 커넥션마다 별도 저장소라, 단일 커넥션을 공유해야 한다.
        if ":memory:" in url or url == "sqlite://":
            engine = _enable_sqlite_fk(
                create_engine(
                    url, connect_args=connect_args, poolclass=StaticPool, future=True
                )
            )
        else:
            engine = _enable_sqlite_fk(
                create_engine(url, connect_args=connect_args, future=True)
            )
    else:
        # PG: 좀비 커넥션·풀 고갈 완화(pre_ping + 풀 한계 + 주기적 재활용).
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            pool_recycle=1800,
            future=True,
        )
    _ENGINES.append(engine)
    return engine


def dispose_engines() -> None:
    """캐시된 엔진을 모두 dispose 하고 캐시를 비운다(테스트 teardown·재설정용)."""
    while _ENGINES:
        _ENGINES.pop().dispose()
    _engine_for.cache_clear()


def get_engine(settings: Settings | None = None) -> Engine:
    """설정의 ``database_url`` 로 엔진을 반환한다."""
    settings = settings or get_settings()
    return _engine_for(settings.database_url)


def get_sessionmaker(settings: Settings | None = None) -> sessionmaker[Session]:
    """세션 팩토리를 반환한다."""
    return sessionmaker(
        bind=get_engine(settings), expire_on_commit=False, future=True
    )


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """트랜잭션 경계를 관리하는 세션 컨텍스트(commit/rollback/close)."""
    session = get_sessionmaker(settings)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(settings: Settings | None = None) -> None:
    """스키마를 생성한다 — 개발/테스트 편의용. 운영 스키마는 Alembic 으로 관리한다."""
    Base.metadata.create_all(get_engine(settings))
