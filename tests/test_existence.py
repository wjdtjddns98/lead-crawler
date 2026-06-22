"""실존성 검증 테스트 — 다중 신호 등급화(주입형 프로브, 네트워크 없음)."""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.verify.existence import ExistenceVerifier


class _Site:
    def __init__(self, ok: bool) -> None:
        self.ok = ok

    def head_ok(self, domain: str) -> bool:
        return self.ok


class _Dns:
    def __init__(self, ok: bool) -> None:
        self.ok = ok

    def resolves(self, domain: str) -> bool:
        return self.ok


class _Reg:
    def __init__(self, val: bool | None) -> None:
        self.val = val

    def is_active(self, registry, registry_id):
        return self.val


def _verify(site: bool, dns: bool, *, reg: bool | None = None, domain: str = "acme.com"):
    v = ExistenceVerifier(
        Settings(dry_run=False),
        site_probe=_Site(site),
        dns_probe=_Dns(dns),
        registry_checker=_Reg(reg) if reg is not None else None,
    )
    return v.verify(domain, registry="edgar", registry_id="0001")


# --- dry_run -----------------------------------------------------------

def test_dry_run_active_with_domain() -> None:
    r = ExistenceVerifier(Settings(dry_run=True)).verify("acme.com")
    assert r.is_active and r.site_alive and r.confidence == 0.9


def test_dry_run_inactive_without_domain() -> None:
    r = ExistenceVerifier(Settings(dry_run=True)).verify(None)
    assert not r.is_active and r.confidence == 0.0


# --- 라이브 등급화(주입 프로브) ---------------------------------------

def test_both_signals_high_confidence() -> None:
    r = _verify(site=True, dns=True)
    assert r.is_active and r.site_alive and r.confidence == 0.85


def test_http_only_mid_confidence() -> None:
    r = _verify(site=True, dns=False)
    assert r.is_active and r.site_alive and r.confidence == 0.7


def test_dns_only_low_confidence() -> None:
    r = _verify(site=False, dns=True)
    assert r.is_active and not r.site_alive and r.confidence == 0.5


def test_no_signal_inactive() -> None:
    r = _verify(site=False, dns=False)
    assert not r.is_active and r.confidence == 0.0


def test_no_domain_inactive() -> None:
    r = _verify(site=True, dns=True, domain="")
    assert not r.is_active and r.confidence == 0.0  # 도메인 없으면 프로브 미시도.


# --- 등록처 active 신호 우선 ------------------------------------------

def test_registry_active_overrides_dead_site() -> None:
    # 등록처가 active 면 사이트·DNS 가 죽어도 실존으로 본다.
    r = _verify(site=False, dns=False, reg=True)
    assert r.is_active and r.confidence == 0.9


def test_registry_defunct_overrides_live_site() -> None:
    # 등록처가 defunct 면 사이트가 살아도 실존 아님(제약 ②).
    r = _verify(site=True, dns=True, reg=False)
    assert not r.is_active and r.site_alive and r.confidence == 0.85
