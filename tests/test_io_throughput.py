"""라이브 처리량(I/O) 최적화 회귀 테스트 — 기업당 중복 네트워크 왕복 제거(결정적·오프라인).

IO-001: enrich 에스컬레이션 체인이 같은 기업 home 을 1회만 fetch(캐시 재사용).
IO-002: 같은 도메인 candidate N개 검증 시 MX(DNS) 조회가 도메인당 1회.
둘 다 카운팅 더블로 왕복 수를 단언한다(wall-clock 비의존, 회귀까지 포착).
"""

from __future__ import annotations

from collections import Counter

import leadcrawler.verify.email_validator as ev_mod
from leadcrawler.config import Settings
from leadcrawler.enrich.enricher import Enricher
from leadcrawler.sources.base import DiscoveredCompany
from leadcrawler.verify.email_validator import EmailValidator

_NO_EMAIL_HTML = "<html><body><p>회사 소개. 연락처 없음.</p><a href='/about'>about</a></body></html>"


class _CountingFetcher:
    """get_text/get_bytes 호출을 URL 별로 센다(네트워크 없음, 이메일 없는 HTML)."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()

    def get_text(self, url, *, params=None, headers=None):  # noqa: ANN001, ARG002
        self.calls[url] += 1
        return _NO_EMAIL_HTML

    def get_bytes(self, url, *, params=None, headers=None):  # noqa: ANN001, ARG002
        self.calls[url] += 1
        return b""

    def get_json(self, url, *, params=None, headers=None):  # noqa: ANN001, ARG002
        self.calls[url] += 1
        return {}

    def post_text(self, url, *, data=None, params=None, headers=None):  # noqa: ANN001, ARG002
        self.calls[url] += 1
        return _NO_EMAIL_HTML

    def post_json(self, url, *, json=None, params=None, headers=None):  # noqa: ANN001, ARG002
        self.calls[url] += 1
        return {}


class _FakeRenderer:
    def render(self, url):  # noqa: ANN001, ARG002
        return _NO_EMAIL_HTML


class _FakeOcr:
    def image_to_text(self, data):  # noqa: ANN001, ARG002
        return "no email here"


class _FakeVision:
    def extract_text(self, data, *, media_type):  # noqa: ANN001, ARG002
        return "no email here"


def _escalating_enricher(fetcher) -> Enricher:  # noqa: ANN001
    """정적→헤드리스→OCR→Vision 전 단계를 타도록 구성한 라이브 Enricher(주입 더블)."""
    settings = Settings(
        dry_run=False,
        enrich_headless=True,
        enrich_ocr=True,
        enrich_vision=True,
        anthropic_api_key="x",  # vision 단계 진입 게이트
        enrich_email_api=False,
        http_request_delay=0.0,
    )
    return Enricher(
        settings,
        fetcher=fetcher,
        renderer=_FakeRenderer(),
        ocr=_FakeOcr(),
        vision=_FakeVision(),
        email_finders=[],
    )


def test_home_fetched_once_across_escalation_chain() -> None:
    # IO-001: 정적·OCR·Vision 이 모두 타도 home GET 은 1회(캐시 공유).
    fetcher = _CountingFetcher()
    enr = _escalating_enricher(fetcher)
    dc = DiscoveredCompany(canonical_key="dom:acme.com", name="Acme", domain="acme.com")
    enr.enrich(dc)
    assert fetcher.calls["https://acme.com"] == 1  # 3→1


def test_last_home_html_is_none_in_dry_run() -> None:
    # last_home_html(실존검증 재사용 신호)은 dry_run 에선 None(네트워크 미수행).
    enr = Enricher(Settings(dry_run=True))
    enr.enrich(DiscoveredCompany(canonical_key="dom:acme.com", name="Acme", domain="acme.com"))
    assert enr.last_home_html is None


def test_last_home_html_is_none_without_domain() -> None:
    # 도메인 없는 기업 enrich 후에도 None(직전 기업 값 누수 0).
    fetcher = _CountingFetcher()
    enr = _escalating_enricher(fetcher)
    enr.enrich(DiscoveredCompany(canonical_key="dom:a.com", name="A", domain="a.com"))
    assert enr.last_home_html is not None  # 도메인 기업 → 채워짐
    enr.enrich(DiscoveredCompany(canonical_key="reg:x", name="NoDomain", domain=None))
    assert enr.last_home_html is None  # 도메인없음 → 진입 즉시 리셋, 누수 없음


def test_home_cache_resets_between_companies() -> None:
    # IO-001: 캐시는 enrich() 진입마다 초기화 — 기업 B 의 home 은 별개로 1회 fetch(누수 0).
    fetcher = _CountingFetcher()
    enr = _escalating_enricher(fetcher)
    enr.enrich(DiscoveredCompany(canonical_key="dom:a.com", name="A", domain="a.com"))
    enr.enrich(DiscoveredCompany(canonical_key="dom:b.com", name="B", domain="b.com"))
    assert fetcher.calls["https://a.com"] == 1
    assert fetcher.calls["https://b.com"] == 1


def test_mx_resolved_once_per_domain(monkeypatch) -> None:
    # IO-002: 같은 도메인 candidate 3개 검증 → MX(DNS) 조회 1회. 다른 도메인은 별도 1회.
    counter: Counter[str] = Counter()
    real = ev_mod._resolve_mx

    def _counting_resolve(domain, settings):  # noqa: ANN001
        counter[domain] += 1
        return real(domain, settings)

    monkeypatch.setattr(ev_mod, "_resolve_mx", _counting_resolve)
    val = EmailValidator(Settings(dry_run=True))
    for local in ("ir", "info", "contact"):
        val.validate(f"{local}@acme.com", "acme.com")
    assert counter["acme.com"] == 1  # 3 후보 → MX 1회
    val.validate("ir@other.com", "other.com")
    assert counter["other.com"] == 1  # 다른 도메인은 독립 1회


def test_mx_cached_but_smtp_probe_runs_per_candidate(monkeypatch) -> None:
    # IO-002: MX 는 도메인당 1회 캐시되지만 SMTP RCPT 프로브는 후보(메일박스)별로 매번 실행.
    # "MX 1회 + probe N회" 를 동시에 단언 — 캐시가 메일박스 단위 검증까지 삼키지 않음을 보증.
    mx_calls: Counter[str] = Counter()
    monkeypatch.setattr(
        ev_mod, "_resolve_mx",
        lambda d, s: (mx_calls.update([d]) or (True, ["mx1.acme.com"])),  # noqa: ARG005
    )
    probe_calls: list[str] = []

    class _CountingProber:
        def probe(self, email, mx_hosts):  # noqa: ANN001, ARG002
            probe_calls.append(email)
            return ev_mod.SMTP_UNKNOWN

    settings = Settings(dry_run=False, email_smtp_check=True, email_smtp_from="probe@leadcrawler.io")
    val = EmailValidator(settings, smtp_prober=_CountingProber())
    for local in ("ir", "info", "contact"):
        val.validate(f"{local}@acme.com", "acme.com")
    assert mx_calls["acme.com"] == 1  # MX 1회(캐시)
    assert probe_calls == ["ir@acme.com", "info@acme.com", "contact@acme.com"]  # 프로브 3회


def test_home_fetch_failure_is_cached_and_stages_skip() -> None:
    # IO-001(개정 2026-07-02): home fetch 실패도 캐시된다 — 죽은 도메인의 타임아웃을
    # 단계(_live/OCR/Vision)마다 중복 지불하던 낭비 제거. 일시 장애 재시도는 Fetcher 의
    # tenacity(3회 지수백오프)가 이미 담당하므로 단계 재fetch 는 순수 중복이었다.
    # graceful 보증은 유지: 이후 단계는 캐시된 실패를 즉시 예외로 받아 폴백(크래시 없음).
    calls: Counter[str] = Counter()

    class _RaisingHomeFetcher(_CountingFetcher):
        def get_text(self, url, *, params=None, headers=None):  # noqa: ANN001, ARG002
            calls[url] += 1
            raise RuntimeError("home down")

    fetcher = _RaisingHomeFetcher()
    enr = _escalating_enricher(fetcher)
    out = enr.enrich(DiscoveredCompany(canonical_key="dom:dead.com", name="Dead", domain="dead.com"))
    # naked+www 폴백 각 1회뿐(_live) — OCR/Vision 단계는 실패 캐시로 재fetch 0, 크래시 없음.
    assert calls["https://dead.com"] == 1
    assert calls["https://www.dead.com"] == 1
    assert isinstance(out, list)


def test_deep_false_skips_smtp_and_deliverability(monkeypatch) -> None:
    # E: deep=False 면 SMTP RCPT·딜리버러빌리티(유료)를 모두 건너뛰고 형식/MX 까지만.
    monkeypatch.setattr(ev_mod, "_resolve_mx", lambda d, s: (True, ["mx1.acme.com"]))  # noqa: ARG005
    probe_calls: list[str] = []
    deliv_calls: list[str] = []

    class _Prober:
        def probe(self, email, mx_hosts):  # noqa: ANN001, ARG002
            probe_calls.append(email)
            return ev_mod.SMTP_UNKNOWN

    class _Deliv:
        name = "stub-deliv"

        def check(self, email):  # noqa: ANN001
            deliv_calls.append(email)
            return ev_mod.DELIV_OK

    settings = Settings(
        dry_run=False, email_smtp_check=True, email_deliverability_check=True,
        email_smtp_from="probe@leadcrawler.io",
    )
    val = EmailValidator(settings, smtp_prober=_Prober(), deliverability_checker=_Deliv())
    shallow = val.validate("info@acme.com", "acme.com", deep=False)
    assert probe_calls == [] and deliv_calls == []  # deep=False → SMTP·딜리버러빌리티 미수행
    assert shallow.mx is True and shallow.domain_match is True  # 형식/MX 는 수행
    val.validate("ir@acme.com", "acme.com", deep=True)
    assert probe_calls == ["ir@acme.com"] and deliv_calls == ["ir@acme.com"]  # deep=True → 둘 다 수행


def test_mx_cache_preserves_validation_output(monkeypatch) -> None:
    # IO-002: 캐시 전후 validate 산출 동치(status/mx/domain_match) — 메모이즈가 결과를 안 바꾼다.
    monkeypatch.setattr(ev_mod, "_resolve_mx", lambda d, s: (True, ["mx1.acme.com"]))  # noqa: ARG005
    val = EmailValidator(Settings(dry_run=True))
    first = val.validate("ir@acme.com", "acme.com")
    second = val.validate("ir@acme.com", "acme.com")  # 캐시 히트
    assert (first.status, first.mx, first.domain_match) == (
        second.status, second.mx, second.domain_match
    )
    assert first.mx is True and first.domain_match is True
