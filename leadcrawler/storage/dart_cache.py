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

from ..dedup import normalize_name
from ..logging import get_logger
from ..schema import DartCorpCacheRow


def _bizno_of(info: dict[str, Any] | None) -> str | None:
    """company.json 의 사업자등록번호(bizr_no)를 숫자만 10자리로 정규화(없으면 None)."""
    if not info:
        return None
    raw = str(info.get("bizr_no") or "")
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[:10] or None

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

    def find_matches(
        self, pairs: list[tuple[str, str]]
    ) -> dict[tuple[str, str], CachedCorp]:
        """(사업자번호 앞6자리, 정규화명) 쌍 벌크 룩업 — NPS×DART 무쿼터 enrich 조인.

        정밀도 우선: **둘 다** 일치해야 매치(동명 이사·유사 사업자번호 오결합 방지 —
        false-merge 는 제약① 방향으로 위험해 이름 단독 매치는 하지 않는다).
        반환 dict 키 = 입력 쌍. 실패는 빈 dict(조인은 최적화 — 없으면 기존 경로).
        """
        want = {(p, n) for p, n in pairs if p and n}
        if not want:
            return {}
        try:
            out: dict[tuple[str, str], CachedCorp] = {}
            norms = list({n for _, n in want})
            with self._factory() as session:
                for i in range(0, len(norms), _BATCH):
                    rows = session.scalars(
                        select(DartCorpCacheRow)
                        .where(DartCorpCacheRow.name_norm.in_(norms[i : i + _BATCH]))
                        .where(DartCorpCacheRow.status == "000")
                        # tie-break 고정(리뷰 M4) — 동일 (bizno6, name_norm) 이 여러 corp 면
                        # corp_code 최소값으로 결정적 선택(런/스냅샷 간 키 플립 방지).
                        .order_by(DartCorpCacheRow.corp_code.asc())
                    ).all()
                    for row in rows:
                        if not row.bizno or not row.name_norm:
                            continue
                        key = (row.bizno[:6], row.name_norm)
                        if key in want and key not in out:
                            out[key] = _row_to_cached(row)
            return out
        except Exception as exc:
            log.info("dart_cache.match.error", n=len(pairs), err=str(exc))
            return {}

    def backfill_join_cols(self) -> int:
        """기존 캐시 행의 bizno/name_norm 을 저장된 info 에서 재계산한다(API 콜 0).

        조인 컬럼 도입 이전에 적재된 행 대상 일회성 — dart-cache-fill CLI 가 호출.
        """
        updated = 0
        try:
            with self._factory() as session:
                rows = session.scalars(
                    select(DartCorpCacheRow)
                    .where(DartCorpCacheRow.name_norm.is_(None))
                    .where(DartCorpCacheRow.status == "000")
                ).all()
                for row in rows:
                    cached = _row_to_cached(row)
                    row.bizno = _bizno_of(cached.info)
                    name_for_norm = (
                        (cached.info.get("corp_name") if cached.info else None) or row.corp_name
                    )
                    row.name_norm = normalize_name(str(name_for_norm))[:255] or None
                    updated += 1
                session.commit()
        except Exception as exc:
            log.info("dart_cache.backfill.error", err=str(exc))
        return updated

    def put_many(self, entries: list[CachedCorp]) -> None:
        """벌크 업서트(멱등) — 같은 corp_code 는 최신 응답으로 덮는다. best-effort."""
        if not entries:
            return
        now = datetime.now(timezone.utc)
        for i in range(0, len(entries), _BATCH):
            chunk = entries[i : i + _BATCH]
            # 배치별 격리 — 병렬 워커 간 PK 경합(IntegrityError) 1건이 잔여 배치를
            # 못 죽이게 한다(캐시는 최적화 — 유실분은 다음 스캔이 재조회·자가치유).
            try:
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
                        row.bizno = _bizno_of(e.info)
                        name_for_norm = (
                            (e.info.get("corp_name") if e.info else None) or e.corp_name
                        )
                        row.name_norm = normalize_name(str(name_for_norm))[:255] or None
                        row.fetched_at = now
                    session.commit()
            except Exception as exc:
                log.info("dart_cache.put.error", n=len(chunk), err=str(exc))
