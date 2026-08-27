"""backfill-market(DART corp_cls 소급 기입) — 대상 선별·갱신·보류 보존·limit 검증.

네트워크·과금 없이 스텁 get_info 로 backfill_dart_markets 의 선별 조건
(dart ∩ (unknown ∪ listed-무market))과 corp_cls 매핑·멱등 규칙을 검증한다.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from leadcrawler.pipeline.column_backfill import backfill_dart_markets
from leadcrawler.schema import Base, DiscoveredCompanyRow


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _add(
    s: Session,
    key: str,
    *,
    registry: str | None = "dart",
    registry_id: str | None = None,
    listed: str = "unknown",
    market: str | None = None,
) -> None:
    s.add(
        DiscoveredCompanyRow(
            canonical_key=key, name=key, registry=registry,
            registry_id=registry_id or key, listed=listed, market=market,
        )
    )


def _row(s: Session, key: str) -> DiscoveredCompanyRow:
    return s.get(DiscoveredCompanyRow, key)


def test_backfill_market_updates_targets_and_keeps_failures() -> None:
    s = _session()
    _add(s, "kosdaq-co")  # unknown → corp_cls K = listed/KOSDAQ
    _add(s, "etc-co")  # unknown → corp_cls E = unlisted/market None
    _add(s, "fail-co")  # 응답 실패 → 원래값 유지(보류)
    _add(s, "kospi-co", listed="listed", market=None)  # 상장인데 market 미상 → 채움
    _add(s, "done-co", listed="listed", market="KOSPI")  # 이미 완성 → 검토 제외
    _add(s, "gleif-co", registry="gleif")  # 타 등록처 → 검토 제외
    s.flush()

    cls_by_code = {"kosdaq-co": "K", "etc-co": "E", "kospi-co": "Y"}

    def get_info(corp_code: str) -> dict | None:
        cls = cls_by_code.get(corp_code)
        return {"status": "000", "corp_cls": cls} if cls else None

    examined, updated = backfill_dart_markets(s, get_info)
    assert (examined, updated) == (4, 3)  # done-co·gleif-co 제외, fail-co 보류.
    assert (_row(s, "kosdaq-co").listed, _row(s, "kosdaq-co").market) == ("listed", "KOSDAQ")
    assert (_row(s, "etc-co").listed, _row(s, "etc-co").market) == ("unlisted", None)
    assert (_row(s, "kospi-co").listed, _row(s, "kospi-co").market) == ("listed", "KOSPI")
    assert (_row(s, "fail-co").listed, _row(s, "fail-co").market) == ("unknown", None)

    # 멱등: 재실행해도 이미 완성된 행은 재검토 대상에서 빠지고 갱신 0.
    examined2, updated2 = backfill_dart_markets(s, get_info)
    assert (examined2, updated2) == (1, 0)  # fail-co 만 재시도.


def test_backfill_market_limit() -> None:
    s = _session()
    for i in range(3):
        _add(s, f"c{i}")
    s.flush()
    examined, updated = backfill_dart_markets(
        s, lambda code: {"status": "000", "corp_cls": "Y"}, limit=2
    )
    assert (examined, updated) == (2, 2)
