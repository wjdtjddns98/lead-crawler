"""cli.py 에서 도메인 계층으로 옮긴 로직(2026-08-26 구조 감사 #1)의 최소 회귀 체크."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from leadcrawler.schema import Base, CompanyRow
from leadcrawler.sources.nps import pick_latest_dataset
from leadcrawler.storage.review import count_dead_sites, enqueue_all_active, purge_dead_sites


def test_pick_latest_dataset_uses_summary_date_not_path() -> None:
    paths = {
        "/15083277/v1/uddi:99999999-aaaa": {"get": {"summary": "국민연금 사업장 가입 202501"}},
        "/15083277/v1/uddi:00000000-bbbb": {"get": {"summary": "국민연금 사업장 가입 20250630"}},
        "/15083277/v1/uddi:cccc": {"get": {"summary": "다른 데이터셋 20991231"}},  # 키워드 없음
    }
    assert pick_latest_dataset(paths) == (
        "/15083277/v1/uddi:00000000-bbbb",
        "국민연금 사업장 가입 20250630",
    )
    assert pick_latest_dataset({"/x": {"get": {"summary": "사업장(날짜 없음)"}}}) is None


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_enqueue_then_purge_dead_sites() -> None:
    s = _session()
    s.add_all(
        [
            CompanyRow(id="alive", canonical_key="dom:a.com", name="A", is_active=True, site_alive=True),
            CompanyRow(id="dead", canonical_key="dom:d.com", name="D", is_active=True, site_alive=False),
            CompanyRow(id="off", canonical_key="dom:o.com", name="O", is_active=False, site_alive=False),
        ]
    )
    s.commit()
    assert enqueue_all_active(s) == (2, 0)  # active 2곳, 이메일 보유 0
    s.commit()
    assert count_dead_sites(s) == (1, 1)  # 드라이런 카운트만 — DML 없음
    assert s.get(CompanyRow, "dead").is_active is True
    purge_dead_sites(s)
    s.commit()
    assert s.get(CompanyRow, "dead").is_active is False
    assert s.get(CompanyRow, "alive").is_active is True
    assert count_dead_sites(s) == (0, 0)
