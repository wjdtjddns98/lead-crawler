"""DART corp 캐시 영속 — company.json 응답을 corp 당 1회만 조회하기 위한 저장소.

:class:`DbDartCorpCache` 는 발견 소스(병렬 청크 워커 포함)가 쓰는 어댑터로,
호출마다 자체 단명 세션을 열어 커밋한다(세션 공유 없음 — 스레드 안전,
:class:`~leadcrawler.storage.discovery_cursor.DbCursorStore` 와 동일 규약).

캐시는 최적화일 뿐 정확성 불변: 읽기 실패는 빈 결과 폴백(미스 취급 → API 재조회),
쓰기 실패는 로그 후 무시한다(크롤 본체를 죽이지 않는다).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..logging import get_logger
from ..schema import DartCorpCacheRow

log = get_logger("storage.dart_cache")

# 업서트 배치 크기 — 세그먼트 finalize 가 최대 scan_limit(5000)건을 한 번에 넘길 수
# 있어, 단일 트랜잭션 폭주 대신 잘라 커밋한다(부분 실패도 앞 배치는 보존).
_BATCH = 500


class CachedCorp:
    """캐시 히트 1건 — 소스가 emit 에 바로 쓸 수 있는 형태(순수 값 객체)."""

    __slots__ = ("corp_code", "corp_name", "status", "info")

    def __init__(
        self, corp_code: str, corp_name: str, status: str, info: dict[str, Any] | None
    ) -> None:
        self.corp_code = corp_code
        self.corp_name = corp_name
        self.status = status
        self.info = info


def _row_to_cached(row: DartCorpCacheRow) -> CachedCorp:
    info: dict[str, Any] | None = None
    if row.info:
        try:
            parsed = json.loads(row.info)
            info = parsed if isinstance(parsed, dict) else None
        except ValueError:  # 깨진 JSON → 미스 취급(재조회로 자가치유).
            log.info("dart_cache.corrupt", corp_code=row.corp_code)
            return CachedCorp(row.corp_code, row.corp_name, "", None)
    return CachedCorp(row.corp_code, row.corp_name, row.status, info)


class DbDartCorpCache:
    """DART corp 캐시 DB 어댑터 — get_many(벌크 조회)·put_many(벌크 업서트)."""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def get_many(self, corp_codes: list[str]) -> dict[str, CachedCorp]:
        """corp_code 벌크 조회 — 있는 것만 dict 로(없는 코드 = 미스). 실패 시 빈 dict."""
        if not corp_codes:
            return {}
        try:
            out: dict[str, CachedCorp] = {}
            with self._factory() as session:
                for i in range(0, len(corp_codes), _BATCH):
                    chunk = corp_codes[i : i + _BATCH]
                    rows = session.scalars(
                        select(DartCorpCacheRow).where(DartCorpCacheRow.corp_code.in_(chunk))
                    ).all()
                    for row in rows:
                        cached = _row_to_cached(row)
                        if cached.status:  # 깨진 JSON(status='')은 미스로 남긴다.
                            out[row.corp_code] = cached
            return out
        except Exception as exc:
            log.info("dart_cache.get.error", n=len(corp_codes), err=str(exc))
            return {}

    def put_many(self, entries: list[CachedCorp]) -> None:
        """벌크 업서트(멱등) — 같은 corp_code 는 최신 응답으로 덮는다. best-effort."""
        if not entries:
            return
        now = datetime.now(timezone.utc)
        try:
            for i in range(0, len(entries), _BATCH):
                chunk = entries[i : i + _BATCH]
                with self._factory() as session:
                    for e in chunk:
                        info_json = json.dumps(e.info, ensure_ascii=False) if e.info else None
                        induty = None
                        if e.info:
                            v = e.info.get("induty_code")
                            induty = str(v)[:16] if v else None
                        row = session.get(DartCorpCacheRow, e.corp_code)
                        if row is None:
                            row = DartCorpCacheRow(corp_code=e.corp_code)
                            session.add(row)
                        row.corp_name = e.corp_name[:512]
                        row.status = e.status[:8]
                        row.induty_code = induty
                        row.info = info_json
                        row.fetched_at = now
                    session.commit()
        except Exception as exc:
            log.info("dart_cache.put.error", n=len(entries), err=str(exc))
