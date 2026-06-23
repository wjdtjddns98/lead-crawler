"""크롤 타깃 영속화 — 웹앱 관리자가 설정한 '다음 크롤 타깃'을 단일행으로 보관.

스케줄러가 매일 :func:`get_crawl_target` 으로 읽어 세그먼트를 만든다(없으면 .env 폴백).
국가·업종은 쉼표구분 CSV, listed 는 unknown/listed/unlisted 중 하나.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..schema import CrawlTargetRow

_ROW_ID = "current"  # 단일행 PK.
VALID_LISTED = frozenset({"unknown", "listed", "unlisted"})


def get_crawl_target(session: Session) -> CrawlTargetRow | None:
    """현재 크롤 타깃 행을 반환한다(없으면 None — 스케줄러가 .env 로 폴백)."""
    return session.get(CrawlTargetRow, _ROW_ID)


def set_crawl_target(
    session: Session,
    *,
    countries: str,
    industries: str,
    listed: str = "unknown",
    persist: bool = True,
    updated_by: str | None = None,
) -> CrawlTargetRow:
    """크롤 타깃을 멱등 upsert 한다(단일행). listed 검증 실패는 ValueError.

    countries/industries 는 쉼표구분 CSV 문자열로 저장한다(빈 문자열 허용 — 국가 빈값은
    스케줄러에서 '지원 전체국'으로 해석).
    """
    if listed not in VALID_LISTED:
        raise ValueError(f"허용되지 않은 상장구분: {listed}")
    now = datetime.now(timezone.utc)
    row = session.get(CrawlTargetRow, _ROW_ID)
    if row is None:
        row = CrawlTargetRow(id=_ROW_ID)
        session.add(row)
    row.countries = countries.strip()
    row.industries = industries.strip()
    row.listed = listed
    row.persist = persist
    row.updated_by = updated_by
    row.updated_at = now
    session.flush()
    return row
