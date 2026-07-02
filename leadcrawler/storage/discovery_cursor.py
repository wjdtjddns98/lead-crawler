"""등록처 발견 커서 영속 — 런 간 스캔 위치(offset/start_index) 저장.

``get_cursor``/``set_cursor`` 는 호출자가 넘긴 세션 안에서 동작하고 flush 만 한다
(commit 은 호출자 책임 — 다른 storage 모듈과 동일 규약). :class:`DbCursorStore` 는
발견 소스(병렬 발견 워커 스레드 포함)가 쓰는 어댑터로, 호출마다 자체 단명 세션을
열어 커밋한다 — 세션 공유가 없어 스레드 안전하다.

커서는 최적화일 뿐 정확성 불변(잃어도 다음 런이 재스캔, dedup 이 걸러냄)이므로
읽기 실패는 0 폴백, 쓰기 실패는 로그 후 무시한다(크롤 본체를 죽이지 않는다).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from ..logging import get_logger
from ..schema import DiscoveryCursorRow

log = get_logger("storage.discovery_cursor")


def get_cursor(session: Session, source: str, key: str) -> int:
    """(source, key) 커서 위치를 반환한다(없으면 0)."""
    row = session.get(DiscoveryCursorRow, (source, key))
    return int(row.position) if row is not None else 0


def set_cursor(session: Session, source: str, key: str, position: int) -> None:
    """(source, key) 커서를 멱등 upsert 한다(updated_at 자동 갱신, flush 만)."""
    row = session.get(DiscoveryCursorRow, (source, key))
    if row is None:
        row = DiscoveryCursorRow(source=source, cursor_key=key, position=position)
        session.add(row)
    else:
        row.position = position
    row.updated_at = datetime.now(timezone.utc)
    session.flush()


class DbCursorStore:
    """:class:`SupportsCursorStore` DB 구현 — 호출마다 자체 세션(스레드 안전)."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def get(self, source: str, key: str) -> int:
        try:
            with self._factory() as session:
                return get_cursor(session, source, key)
        except Exception as exc:  # 커서 유실은 재스캔으로 흡수 — 크롤 본체 보호.
            log.info("cursor.get.error", source=source, key=key, err=str(exc))
            return 0

    def advance(self, source: str, key: str, position: int) -> None:
        try:
            with self._factory() as session:
                set_cursor(session, source, key, position)
                session.commit()
        except Exception as exc:  # 위와 동일 — best-effort 영속.
            log.info("cursor.advance.error", source=source, key=key, err=str(exc))
