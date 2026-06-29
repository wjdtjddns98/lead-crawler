"""라이브(dry_run=False) 병렬 추출 e2e — cost 정합·결정성·취소·도메인해석(네트워크 없음).

기존 test_pipeline_parallel 은 dry_run=True 경로만 본다. 이 테스트는 dry_run=False 일 때만
타는 분기(cost_ledger 과금·registry_checker/DomainResolver 주입)를 **워커 4개 동시**로
돌려, 네트워크를 건드리는 클래스를 가짜로 주입해 검증한다:
- 라이브 병렬 산출이 순차(workers=1)와 동일(결정성).
- 병렬 워커들의 유료 호출이 cost_ledger 에 유실·중복 없이 정확히 1:1 기록(과금 정합).
- 배치 중간 취소는 이미 큐된 분을 보존, 즉시 취소는 빈 결과(협조적 중단).
- resolve_domains=True 면 도메인 미보유 발견이 DomainResolver 로 보강돼 enrich 까지 도달.
"""

from __future__ import annotations

import threading

import leadcrawler.pipeline.run as run_mod
from leadcrawler.config import Settings
from leadcrawler.models import (
    Contact,
    ContactType,
    EmailRole,
    EmailValidation,
    ExtractMethod,
    ValidationStatus,
)
from leadcrawler.pipeline import run_pipeline
from leadcrawler.sources.base import DiscoveredCompany, Segment
from leadcrawler.verify.existence import ExistenceResult


def _many(n: int):
    def _disc(segment, settings, cost_ledger=None, *, sources=None, seen_domains=None):  # noqa: ARG001
        return [
            DiscoveredCompany(canonical_key=f"dom:c{i}.com", name=f"C{i}", domain=f"c{i}.com")
            for i in range(n)
        ]

    return _disc


def _many_no_domain(n: int):
    # 도메인 미보유 발견(GLEIF 등) — 라이브 DomainResolver 분기(run.py 277)를 타게 한다.
    def _disc(segment, settings, cost_ledger=None, *, sources=None, seen_domains=None):  # noqa: ARG001
        return [
            DiscoveredCompany(canonical_key=f"lei:c{i}", name=f"C{i}", domain=None)
            for i in range(n)
        ]

    return _disc


class _FakeEnricher:
    """네트워크 없이 결정적 이메일 1건 반환 + 유료 호출 1건을 cost_ledger 에 기록."""

    def __init__(self, settings, *, cost_ledger=None, **_kw) -> None:  # noqa: ANN001, ARG002
        self._led = cost_ledger
        self.last_home_html = None  # _build_lead 가 existence 로 넘기는 신호(라이브 경로).

    def enrich(self, dc: DiscoveredCompany) -> list[Contact]:
        if self._led is not None:
            self._led.record("hunter")  # 유료 호출 시뮬 — 병렬 과금 정합 검증 지점.
        return [
            Contact(
                type=ContactType.EMAIL, value=f"ir@{dc.domain}", role=EmailRole.IR,
                extract_method=ExtractMethod.STATIC,
                source_url=f"https://{dc.domain}", confidence=0.9,
            )
        ]

    def close(self) -> None:
        pass


class _FakeExistence:
    def __init__(self, settings, *, registry_checker=None, **_kw) -> None:  # noqa: ANN001, ARG002
        pass

    def verify(self, domain, *, registry=None, registry_id=None, home_html=None):  # noqa: ANN001, ARG002
        return ExistenceResult(is_active=True, site_alive=True, confidence=0.9)

    def close(self) -> None:
        pass


class _FakeValidator:
    def __init__(self, settings, *, cost_ledger=None, **_kw) -> None:  # noqa: ANN001, ARG002
        self.settings = settings  # _build_lead 가 validate_all_candidates 를 읽는다(E).

    def validate(self, email, company_domain=None, *, deep=True):  # noqa: ANN001, ARG002
        return EmailValidation(status=ValidationStatus.VALID, mx=True)

    def close(self) -> None:
        pass


class _FakeDomainResolver:
    """도메인 미보유 기업의 공식 도메인을 결정적으로 해석(네트워크 없음)."""

    def __init__(self, settings, *, cost_ledger=None, **_kw) -> None:  # noqa: ANN001, ARG002
        pass

    def resolve(self, dc: DiscoveredCompany) -> str:
        return f"{dc.canonical_key.split(':')[-1]}.com"  # lei:c0 -> c0.com


class _CountingLedger:
    """record() 호출을 스레드안전하게 센다(병렬 워커 유료 호출 유실·중복 검출)."""

    def __init__(self, settings=None, *, persist=False) -> None:  # noqa: ANN001, ARG002
        self._lock = threading.Lock()
        self.records: list[str] = []
        _created_ledgers.append(self)

    def record(self, provider: str, units: int = 1) -> None:  # noqa: ARG002
        with self._lock:
            self.records.append(provider)

    def is_over_budget(self) -> bool:
        return False


_created_ledgers: list[_CountingLedger] = []  # 런마다 생성된 원장(테스트 진입 시 clear).


def _patch_live(monkeypatch, n: int, *, disc=None) -> None:
    _created_ledgers.clear()
    monkeypatch.setattr(run_mod, "discover_segment", disc if disc is not None else _many(n))
    monkeypatch.setattr(run_mod, "Enricher", _FakeEnricher)
    monkeypatch.setattr(run_mod, "ExistenceVerifier", _FakeExistence)
    monkeypatch.setattr(run_mod, "EmailValidator", _FakeValidator)
    monkeypatch.setattr(run_mod, "CostLedger", _CountingLedger)
    monkeypatch.setattr(run_mod, "DomainResolver", _FakeDomainResolver)
    monkeypatch.setattr(run_mod, "build_registry_checker", lambda settings: None)  # noqa: ARG005


def _shape(leads):
    return [(ld.company.canonical_key, ld.email.value if ld.email else None) for ld in leads]


def test_live_parallel_matches_sequential(monkeypatch) -> None:
    # 라이브(dry_run=False) 경로에서 workers=4 산출이 workers=1 과 동일(결정성).
    seg = [Segment(country="KR", industry="건설")]
    _patch_live(monkeypatch, 30)
    seq = run_pipeline(seg, settings=Settings(dry_run=False, enrich_workers=1))
    _patch_live(monkeypatch, 30)
    par = run_pipeline(seg, settings=Settings(dry_run=False, enrich_workers=4))
    assert len(seq) == 30
    assert _shape(seq) == _shape(par)


def test_live_parallel_cost_ledger_exact(monkeypatch) -> None:
    # 병렬 워커 20개 처리 → 유료 호출(hunter)이 cost_ledger 에 정확히 20건(유실·중복 0).
    _patch_live(monkeypatch, 20)
    run_pipeline(
        [Segment(country="KR", industry="건설")],
        settings=Settings(dry_run=False, enrich_workers=4),
    )
    assert len(_created_ledgers) == 1  # 런당 원장 1개
    assert _created_ledgers[0].records == ["hunter"] * 20  # 20 회사 × 1, 병렬 유실/중복 0


def test_live_parallel_cancel_preserves(monkeypatch) -> None:
    # 라이브 병렬 경로에서 배치 중간 취소 → 취소 전 발견·큐된 분은 보존(빈 결과 아님).
    # should_cancel 은 메인 루프에서만 호출되므로 호출 순서가 결정적(flaky 없음).
    _patch_live(monkeypatch, 50)
    calls = {"n": 0}

    def _cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 6  # 세그먼트 1 + 회사 5건 통과 후 취소(7번째 호출부터 True).

    leads = run_pipeline(
        [Segment(country="KR", industry="건설")],
        settings=Settings(dry_run=False, enrich_workers=4),
        should_cancel=_cancel,
    )
    # 취소 직전까지 pending 에 모인 5건이 세그먼트 경계 _flush 로 보존된다(전량 50 미만·>0).
    assert 0 < len(leads) < 50


def test_live_parallel_immediate_cancel_is_empty(monkeypatch) -> None:
    # 라이브 경로 루프 진입 직후 취소 → 빈 결과(크래시 없음).
    _patch_live(monkeypatch, 50)
    leads = run_pipeline(
        [Segment(country="KR", industry="건설")],
        settings=Settings(dry_run=False, enrich_workers=4),
        should_cancel=lambda: True,
    )
    assert leads == []


def test_live_parallel_resolves_missing_domains(monkeypatch) -> None:
    # resolve_domains=True 라이브 분기(run.py 277) — 도메인 미보유 발견이 DomainResolver 로
    # 도메인을 얻어 enrich 까지 도달(이메일 산출). 워커>1 에서도 동작.
    _patch_live(monkeypatch, 12, disc=_many_no_domain(12))
    leads = run_pipeline(
        [Segment(country="KR", industry="건설")],
        settings=Settings(dry_run=False, enrich_workers=4, resolve_domains=True),
    )
    assert len(leads) == 12
    # 해석된 도메인(c{i}.com)으로 enrich → 결정적 이메일이 채워졌다(분기 실제 실행 증거).
    assert all(ld.email is not None and ld.email.value.endswith(".com") for ld in leads)
