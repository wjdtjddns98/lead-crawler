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
    hp = "https://x.example"
    _add(s, "k1", "한빛 반도체", UNCLASSIFIED, homepage=hp)  # 스텁 키워드 → 반도체·디스플레이
    _add(s, "k2", "Opaque Holdings", UNCLASSIFIED, homepage=hp)  # 무키워드 → abstain(미분류 유지)
    _add(s, "k3", "부산 화장품", "기타 제조", homepage=hp)  # catch-all 도 재분류 대상
    _add(s, "k4", "비활성 게임", UNCLASSIFIED, active=False)  # 비활성 → 검토 제외
    _add(s, "k5", "이미 확정", "은행")  # 확신 라벨 → 검토 제외
    _add(s, "k6", "서울 게임즈", UNCLASSIFIED)  # 홈페이지 없음 → 게이트 스킵(블라인드 분류 금지)
    s.flush()

    examined, updated = backfill_industries(
        s, StubClassifier(), fetch_html=lambda url: "<p>corporate site</p>"
    )

    assert (examined, updated) == (4, 2)
    assert _industry(s, "k1") == "반도체·디스플레이"
    assert _industry(s, "k2") == UNCLASSIFIED
    assert _industry(s, "k3") == "화장품·뷰티"
    assert _industry(s, "k4") == UNCLASSIFIED  # 비활성은 손대지 않음
    assert _industry(s, "k5") == "은행"
    # 이름에 키워드('게임')가 있어도 홈페이지 본문이 없으면 분류하지 않는다(오라벨 원천 차단).
    assert _industry(s, "k6") == UNCLASSIFIED


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
    hp = "https://x.example"
    _add(s, "a1", "한빛 반도체", UNCLASSIFIED, homepage=hp)
    _add(s, "a2", "서울 게임즈", UNCLASSIFIED, homepage=hp)
    s.flush()

    examined, updated = backfill_industries(
        s, StubClassifier(), fetch_html=lambda url: "<p>corporate site</p>", limit=1
    )

    assert (examined, updated) == (1, 1)
    assert _industry(s, "a1") == "반도체·디스플레이"
    assert _industry(s, "a2") == UNCLASSIFIED  # limit 밖 — 미검토


class _Resp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code, self.text = status, text


def _get_from(table: dict[str, object]):
    """URL→응답 표. 값이 Exception 이면 접속 실패로 raise."""
    calls: list[str] = []

    def get(url: str):
        calls.append(url)
        v = table.get(url, _Resp(404))
        if isinstance(v, Exception):
            raise v
        return v

    return get, calls


def test_fetch_industry_html_http_fallback_only_on_connect_error():
    """https 접속 실패(ConnectError)한 호스트만 http:// 로 재시도한다."""
    from leadcrawler.cli import fetch_industry_html

    import httpx

    get, calls = _get_from({
        "https://a.kr": httpx.ConnectError("x"), "https://www.a.kr": httpx.ConnectError("x"),
        "http://a.kr": _Resp(200, "<p>alive over http</p>"),
    })
    assert fetch_industry_html("https://a.kr", get=get, render=lambda u: None) == \
        "<p>alive over http</p>"
    # 404 응답·읽기 타임아웃을 받은 호스트는 http 폴백 안 함(접속은 됐으므로).
    get, calls = _get_from({"https://b.kr": _Resp(404), "https://www.b.kr": httpx.ReadTimeout("x")})
    assert fetch_industry_html("https://b.kr", get=get, render=lambda u: None) is None
    assert calls == ["https://b.kr", "https://www.b.kr"]


def test_fetch_industry_html_headless_on_403_but_not_406():
    """403(봇차단)만 헤드리스로 1회 재시도, 챌린지 페이지는 본문 취급 안 함. 406 은 제외."""
    from leadcrawler.cli import fetch_industry_html

    get, _ = _get_from({"https://c.com": _Resp(403, "blocked")})
    assert fetch_industry_html("https://c.com", get=get, render=lambda u: "<h1>Real</h1>") == \
        "<h1>Real</h1>"
    assert fetch_industry_html(
        "https://c.com", get=get, render=lambda u: "<title>Just a moment...</title>"
    ) is None
    get, _ = _get_from({"https://d.kr": _Resp(406), "https://www.d.kr": _Resp(406)})
    rendered: list[str] = []
    assert fetch_industry_html(
        "https://d.kr", get=get, render=lambda u: rendered.append(u) or "<p>x</p>"
    ) is None
    assert rendered == []
