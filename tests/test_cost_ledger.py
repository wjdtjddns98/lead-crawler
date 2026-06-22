"""cost_ledger 테스트 — 단가·집계·예산 가드 + 유료 호출부 배선(네트워크 없음)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from leadcrawler.config import Settings
from leadcrawler.cost_ledger import DEFAULT_PRICING_KRW, CostLedger, month_key_of
from leadcrawler.enrich.enricher import Enricher
from leadcrawler.models import ValidationStatus
from leadcrawler.sources.base import DiscoveredCompany
from leadcrawler.storage.db import init_db
from leadcrawler.verify import email_validator as ev_mod
from leadcrawler.verify.email_validator import EmailValidator

UTC = timezone.utc


def _at(y: int, m: int, d: int = 1):
    """고정 시각 콜러블(now 주입용)."""
    fixed = datetime(y, m, d, tzinfo=UTC)
    return lambda: fixed


# --- 단가/집계(인메모리) -----------------------------------------------

def test_unit_cost_registered_and_unknown() -> None:
    led = CostLedger(Settings())
    assert led.unit_cost("vision") == DEFAULT_PRICING_KRW["vision"]
    assert led.unit_cost("nope") == 0  # 미등록 provider 는 0(과금 미추적)


def test_in_memory_record_totals_and_remaining() -> None:
    led = CostLedger(Settings(monthly_budget_krw=1000), now=_at(2026, 6))
    led.record("zerobounce", 3)  # 10*3=30
    led.record("neverbounce")  # 10
    assert led.month_total_krw() == 40
    assert led.remaining_krw() == 960
    assert not led.is_over_budget()


def test_pricing_override() -> None:
    led = CostLedger(Settings(monthly_budget_krw=1000), pricing={"vision": 100}, now=_at(2026, 6))
    led.record("vision")
    assert led.month_total_krw() == 100


def test_pricing_override_from_config() -> None:
    # config(env) 단가 보정이 기본 추정치를 덮어쓴다.
    s = Settings(monthly_budget_krw=1000, cost_pricing_krw={"hunter": 80})
    led = CostLedger(s, now=_at(2026, 6))
    led.record("hunter")
    assert led.month_total_krw() == 80


def test_explicit_pricing_overrides_config() -> None:
    # 우선순위: 기본 < config < 명시 인자.
    s = Settings(cost_pricing_krw={"hunter": 80})
    led = CostLedger(s, pricing={"hunter": 5}, now=_at(2026, 6))
    led.record("hunter")
    assert led.month_total_krw() == 5


def test_breakdown_sorted_desc() -> None:
    led = CostLedger(Settings(), now=_at(2026, 6))
    led.record("hunter")  # 50
    led.record("vision", 2)  # 60
    assert led.breakdown() == {"vision": 60, "hunter": 50}


def test_month_isolation() -> None:
    times = iter([datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)])
    led = CostLedger(Settings(), now=lambda: next(times))
    led.record("hunter")  # 5월
    led.record("hunter")  # 6월
    assert led.month_total_krw("2026-05") == 50
    assert led.month_total_krw("2026-06") == 50


def test_is_over_budget_at_exact_limit() -> None:
    led = CostLedger(Settings(monthly_budget_krw=50), now=_at(2026, 6))
    led.record("hunter")  # 50 == 예산 → 초과로 간주(이상)
    assert led.is_over_budget()
    assert led.remaining_krw() == 0


def test_record_negative_units_clamped() -> None:
    led = CostLedger(Settings(), now=_at(2026, 6))
    ev = led.record("hunter", -5)
    assert ev.units == 0 and ev.cost_krw == 0


# --- DB 영속 ----------------------------------------------------------

def test_persist_records_and_aggregates(tmp_path) -> None:
    s = Settings(database_url=f"sqlite:///{tmp_path}/cost.db", monthly_budget_krw=100)
    init_db(s)
    led = CostLedger(s, persist=True, now=_at(2026, 6, 22))
    led.record("vision", 2)  # 60
    led.record("hunter")  # 50
    assert led.month_total_krw("2026-06") == 110
    assert led.is_over_budget("2026-06")  # 110 >= 100
    assert led.breakdown("2026-06") == {"vision": 60, "hunter": 50}
    # 다른 월은 0(격리).
    assert led.month_total_krw("2026-05") == 0


def test_persist_survives_new_ledger_instance(tmp_path) -> None:
    s = Settings(database_url=f"sqlite:///{tmp_path}/cost2.db", monthly_budget_krw=1000)
    init_db(s)
    CostLedger(s, persist=True, now=_at(2026, 6)).record("apollo")  # 50
    # 새 인스턴스가 DB 에서 누계를 다시 읽는다(다중 런 합산).
    assert CostLedger(s, persist=True, now=_at(2026, 6)).month_total_krw("2026-06") == 50


def test_persist_cache_increments_consistently(tmp_path) -> None:
    # 캐시 증분이 DB 누계와 정합(여러 record 후 합계 일치).
    s = Settings(database_url=f"sqlite:///{tmp_path}/cost3.db", monthly_budget_krw=10_000)
    init_db(s)
    led = CostLedger(s, persist=True, now=_at(2026, 6))
    for _ in range(3):
        led.record("hunter")  # 50*3
    assert led.month_total_krw("2026-06") == 150
    # 새 인스턴스(캐시 없음)도 DB 에서 같은 누계.
    assert CostLedger(s, persist=True, now=_at(2026, 6)).month_total_krw("2026-06") == 150


def test_persist_cache_refresh_picks_up_external_writes(tmp_path) -> None:
    # 캐시는 현재 프로세스 정합 — 다른 인스턴스(프로세스 모사) 쓰기는 refresh 후 반영.
    s = Settings(database_url=f"sqlite:///{tmp_path}/cost4.db", monthly_budget_krw=10_000)
    init_db(s)
    a = CostLedger(s, persist=True, now=_at(2026, 6))
    a.record("hunter")  # 50, a 캐시=50
    CostLedger(s, persist=True, now=_at(2026, 6)).record("hunter")  # DB 누계=100
    assert a.month_total_krw("2026-06") == 50  # a 캐시는 외부 쓰기 미반영.
    a.refresh()
    assert a.month_total_krw("2026-06") == 100  # 재시드 후 DB 진실원천 반영.


def test_report_structure(tmp_path) -> None:
    s = Settings(database_url=f"sqlite:///{tmp_path}/cost5.db", monthly_budget_krw=200)
    init_db(s)
    led = CostLedger(s, persist=True, now=_at(2026, 6, 22))
    led.record("vision", 2)  # 60
    led.record("hunter")  # 50
    r = led.report("2026-06")
    assert r["month_key"] == "2026-06" and r["total_krw"] == 110
    assert r["budget_krw"] == 200 and r["remaining_krw"] == 90
    assert r["pct"] == 55.0 and r["over_budget"] is False
    assert r["breakdown"] == {"vision": 60, "hunter": 50}


def test_month_key_of() -> None:
    assert month_key_of(datetime(2026, 1, 9, tzinfo=UTC)) == "2026-01"


# --- 유료 호출부 배선: 더블 ------------------------------------------

class _FetchEmpty:
    """SupportsFetch 더블 — 빈 HTML 반환(정적 추출 0건, 네트워크 없음)."""

    def get_text(self, url, *, params=None, headers=None):
        return ""

    def get_bytes(self, url, *, params=None, headers=None):
        return b""


class _FakeFinder:
    """SupportsEmailFinder 더블 — 호출 카운트, 이메일 0건 반환."""

    source = "https://example.test"

    def __init__(self, name: str = "hunter") -> None:
        self.name = name
        self.calls = 0

    def find_emails(self, domain: str, *, limit: int = 5):
        self.calls += 1
        return []


class _FetchHtml:
    """SupportsFetch 더블 — 고정 HTML 반환 + 이미지 바이트(네트워크 없음)."""

    def __init__(self, html: str) -> None:
        self.html = html

    def get_text(self, url, *, params=None, headers=None):
        return self.html

    def get_bytes(self, url, *, params=None, headers=None):
        return b"img-bytes"


class _FakeVision:
    """SupportsVision 더블 — 호출 카운트, 텍스트 0(이메일 없음)."""

    def __init__(self) -> None:
        self.calls = 0

    def extract_text(self, image: bytes, *, media_type: str = "image/png") -> str:
        self.calls += 1
        return ""


def _dc(domain: str = "acme.com") -> DiscoveredCompany:
    return DiscoveredCompany(canonical_key=f"dom:{domain}", name="Acme", domain=domain)


# --- Enricher 예산 게이트 + 과금 기록 ---------------------------------

def test_enricher_records_email_api_cost() -> None:
    s = Settings(dry_run=False, enrich_email_api=True, monthly_budget_krw=10_000)
    led = CostLedger(s, now=_at(2026, 6))
    finder = _FakeFinder()
    Enricher(s, fetcher=_FetchEmpty(), email_finders=[finder], cost_ledger=led).enrich(_dc())
    assert finder.calls == 1  # 정적 0건 → 유료 호출 진입.
    assert led.month_total_krw() == 50  # hunter 1회 과금.


def test_enricher_blocks_paid_when_over_budget() -> None:
    # 예산 0 → 어떤 유료 호출도 차단(0 >= 0).
    s = Settings(dry_run=False, enrich_email_api=True, monthly_budget_krw=0)
    led = CostLedger(s, now=_at(2026, 6))
    finder = _FakeFinder()
    Enricher(s, fetcher=_FetchEmpty(), email_finders=[finder], cost_ledger=led).enrich(_dc())
    assert finder.calls == 0  # 예산 초과 → 진입 차단.
    assert led.month_total_krw() == 0


def test_enricher_enforce_off_allows_paid_over_budget() -> None:
    # enforce off 면 초과여도 호출은 진행(기록만, 차단 안 함).
    s = Settings(
        dry_run=False, enrich_email_api=True, monthly_budget_krw=0, cost_budget_enforce=False
    )
    led = CostLedger(s, now=_at(2026, 6))
    finder = _FakeFinder()
    Enricher(s, fetcher=_FetchEmpty(), email_finders=[finder], cost_ledger=led).enrich(_dc())
    assert finder.calls == 1


def test_enricher_no_ledger_is_noop() -> None:
    # 원장 없으면(예: dry_run/비영속 미주입) 게이트·기록 모두 no-op.
    s = Settings(dry_run=False, enrich_email_api=True)
    finder = _FakeFinder()
    Enricher(s, fetcher=_FetchEmpty(), email_finders=[finder]).enrich(_dc())
    assert finder.calls == 1  # 차단 안 됨(원장 없음).


def test_enricher_email_api_stops_mid_loop_at_budget() -> None:
    # 예산이 1건만 허용 — 첫 finder 가 예산을 채우면 두번째 finder 는 루프 내에서 차단.
    s = Settings(dry_run=False, enrich_email_api=True, monthly_budget_krw=50)
    led = CostLedger(s, now=_at(2026, 6))
    hunter, apollo = _FakeFinder("hunter"), _FakeFinder("apollo")
    Enricher(
        s, fetcher=_FetchEmpty(), email_finders=[hunter, apollo], cost_ledger=led
    ).enrich(_dc())
    assert hunter.calls == 1  # 첫 호출(50) → 예산 소진.
    assert apollo.calls == 0  # 루프 내 재확인으로 두번째는 차단(초과 방지).
    assert led.month_total_krw() == 50


def test_enricher_vision_stops_mid_loop_at_budget() -> None:
    # vision_max_images=3 이라도 예산이 1장만 허용하면 이미지 루프 내에서 차단.
    html = (
        '<img src="https://acme.com/a.png">'
        '<img src="https://acme.com/b.png">'
        '<img src="https://acme.com/c.png">'
    )
    s = Settings(
        dry_run=False,
        enrich_vision=True,
        anthropic_api_key="k",
        vision_max_images=3,
        monthly_budget_krw=30,
    )
    led = CostLedger(s, now=_at(2026, 6))
    vision = _FakeVision()
    Enricher(s, fetcher=_FetchHtml(html), vision=vision, cost_ledger=led).enrich(_dc())
    assert vision.calls == 1  # 1장(30) 후 예산 소진 → 나머지 이미지 차단.
    assert led.month_total_krw() == 30


def test_persist_failure_does_not_crash_pipeline(tmp_path) -> None:
    # persist=True 인데 테이블 미생성(DB 장애 모사) → record 가 예외 없이 degrade.
    s = Settings(database_url=f"sqlite:///{tmp_path}/missing.db", monthly_budget_krw=1000)
    led = CostLedger(s, persist=True, now=_at(2026, 6))  # init_db 미호출 → cost_ledger 테이블 없음
    ev = led.record("hunter")  # 예외 전파 없이 이벤트 반환(추적만 degrade).
    assert ev.provider == "hunter" and ev.cost_krw == 50


# --- EmailValidator 딜리버러빌리티 예산 게이트 ------------------------

class _FakeChecker:
    name = "zerobounce"

    def __init__(self) -> None:
        self.calls = 0

    def check(self, email: str) -> str:
        self.calls += 1
        from leadcrawler.verify.deliverability import UNKNOWN

        return UNKNOWN


def _patch_mx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ev_mod, "_resolve_mx", lambda d, s: (True, ["mx.test"]))


def test_validator_records_deliverability_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mx(monkeypatch)
    s = Settings(dry_run=False, email_deliverability_check=True, monthly_budget_krw=10_000)
    led = CostLedger(s, now=_at(2026, 6))
    checker = _FakeChecker()
    v = EmailValidator(s, deliverability_checker=checker, cost_ledger=led)
    v.validate("ir@acme.com", "acme.com")
    assert checker.calls == 1 and led.month_total_krw() == 10  # zerobounce 1회.


def test_validator_blocks_deliverability_when_over_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mx(monkeypatch)
    s = Settings(dry_run=False, email_deliverability_check=True, monthly_budget_krw=0)
    led = CostLedger(s, now=_at(2026, 6))
    checker = _FakeChecker()
    v = EmailValidator(s, deliverability_checker=checker, cost_ledger=led)
    result = v.validate("ir@acme.com", "acme.com")
    assert checker.calls == 0  # 예산 초과 → 차단.
    assert led.month_total_krw() == 0
    # 차단돼도 1차 판정(VALID)은 그대로.
    assert result.status is ValidationStatus.VALID
