"""backfill-industry(구분 소급 재분류) — 대상 선별·갱신·abstain 보존·limit 검증.

네트워크·과금 없이 StubClassifier(결정적 키워드 스캔)로 backfill_industries 의
선별 조건(AMBIGUOUS_LABELS ∩ is_active)과 갱신/보존 규칙을 검증한다.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from leadcrawler.cli import backfill_industries
from leadcrawler.enrich.industry_classify import StubClassifier
from leadcrawler.schema import Base, CompanyRow, DiscoveredCompanyRow
from leadcrawler.sources.taxonomy import UNCLASSIFIED


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _add(
    s: Session,
    key: str,
    name: str,
    industry: str,
    *,
    active: bool = True,
    homepage: str | None = None,
    domain: str | None = None,
) -> None:
    s.add(DiscoveredCompanyRow(canonical_key=key, name=name, domain=domain))
    s.add(
        CompanyRow(
            id=key, canonical_key=key, name=name, industry=industry,
            is_active=active, homepage=homepage,
        )
    )


def _industry(s: Session, key: str) -> str:
    return s.get(CompanyRow, key).industry


def test_backfill_updates_ambiguous_and_keeps_abstain() -> None:
    """미분류·'기타 제조'만 검토하고, 확신 라벨은 갱신·abstain 은 원래값 유지."""
    s = _session()
    _add(s, "k1", "한빛 반도체", UNCLASSIFIED)  # 스텁 키워드 → 반도체·디스플레이
    _add(s, "k2", "Opaque Holdings", UNCLASSIFIED)  # 무키워드 → abstain(미분류 유지)
    _add(s, "k3", "부산 화장품", "기타 제조")  # catch-all 도 재분류 대상
    _add(s, "k4", "비활성 게임", UNCLASSIFIED, active=False)  # 비활성 → 검토 제외
    _add(s, "k5", "이미 확정", "은행")  # 확신 라벨 → 검토 제외
    s.flush()

    examined, updated = backfill_industries(
        s, StubClassifier(), fetch_html=lambda url: None
    )

    assert (examined, updated) == (3, 2)
    assert _industry(s, "k1") == "반도체·디스플레이"
    assert _industry(s, "k2") == UNCLASSIFIED
    assert _industry(s, "k3") == "화장품·뷰티"
    assert _industry(s, "k4") == UNCLASSIFIED  # 비활성은 손대지 않음
    assert _industry(s, "k5") == "은행"


def test_backfill_uses_homepage_html_as_evidence() -> None:
    """홈페이지가 있으면 fetch_html 본문을 분류 근거로 쓴다(이름만으론 무키워드)."""
    s = _session()
    _add(s, "k1", "ACME Co", UNCLASSIFIED, homepage="https://acme.example")
    s.flush()

    fetched: list[str] = []

    def fake_fetch(url: str) -> str:
        fetched.append(url)
        return "<html>global logistics provider</html>"

    examined, updated = backfill_industries(s, StubClassifier(), fetch_html=fake_fetch)

    assert (examined, updated) == (1, 1)
    assert fetched == ["https://acme.example"]
    assert _industry(s, "k1") == "물류·운송"


def test_backfill_limit_caps_examined_rows() -> None:
    """--limit 은 검토 건수를 상한한다(소량 시험용) — id 순으로 앞에서 자름."""
    s = _session()
    _add(s, "a1", "한빛 반도체", UNCLASSIFIED)
    _add(s, "a2", "서울 게임즈", UNCLASSIFIED)
    s.flush()

    examined, updated = backfill_industries(
        s, StubClassifier(), fetch_html=lambda url: None, limit=1
    )

    assert (examined, updated) == (1, 1)
    assert _industry(s, "a1") == "반도체·디스플레이"
    assert _industry(s, "a2") == UNCLASSIFIED  # limit 밖 — 미검토
