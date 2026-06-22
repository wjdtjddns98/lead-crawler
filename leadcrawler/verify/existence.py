"""실존성 검증 — 제약 ②(현 시점 실존 기업만).

신호를 **다중화**해 단일 HTTP HEAD 의존을 줄인다:
- 도메인 DNS 생존(A/MX 레코드 존재) — 사이트가 잠깐 죽어도 도메인 생존을 포착,
- 홈페이지 HTTP 200 — 실제 서비스 생존,
- (opt-in seam) 등록처 active 신호 — EDGAR/DART/GLEIF 등 공식 등록처가 active/최근
  공시로 보고하면 가장 강한 신호로 우선한다(주입형, 미주입이면 미사용).

판정은 위 신호를 **등급화 confidence** 로 합성한다(둘 다=높음, 하나=중간, 없음=비실존).
모든 프로브는 주입 가능(테스트는 네트워크 없이 가짜 프로브로 분기 검증). dry_run 에서는
네트워크 없이 도메인 유무로 결정적 판정한다.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from ..config import Settings, get_settings
from ..logging import get_logger

log = get_logger("verify.existence")


class ExistenceResult(BaseModel):
    """실존성 판정 결과."""

    is_active: bool
    site_alive: bool
    confidence: float


class SupportsSiteProbe(Protocol):
    """홈페이지 HTTP 생존 프로버(테스트 더블이 구현)."""

    def head_ok(self, domain: str) -> bool:
        """``https?://domain`` HEAD 가 200~399 면 True(실패 시 False)."""
        ...


class SupportsDnsProbe(Protocol):
    """도메인 DNS 생존 프로버(테스트 더블이 구현)."""

    def resolves(self, domain: str) -> bool:
        """도메인이 A 또는 MX 레코드로 해석되면 True(실패 시 False)."""
        ...


class SupportsRegistryActive(Protocol):
    """등록처 active 신호 체커(주입형 placeholder — 미주입이면 미사용)."""

    def is_active(self, registry: str | None, registry_id: str | None) -> bool | None:
        """등록처가 active/defunct 를 보고하면 True/False, 판정 불가면 None."""
        ...


class HttpSiteProbe:
    """httpx 기반 실 HTTP HEAD 프로버(graceful — 오류 시 False)."""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def head_ok(self, domain: str) -> bool:
        import httpx

        for scheme in ("https", "http"):
            try:
                resp = httpx.head(
                    f"{scheme}://{domain}", timeout=self._timeout, follow_redirects=True
                )
                if resp.status_code < 400:
                    return True
            except Exception:
                continue
        return False


class DnsProbe:
    """dnspython 기반 실 DNS 프로버 — A 또는 MX 존재 여부(graceful)."""

    def resolves(self, domain: str) -> bool:
        import dns.resolver

        for rtype in ("A", "MX"):
            try:
                if dns.resolver.resolve(domain, rtype):
                    return True
            except Exception:
                continue
        return False


class ExistenceVerifier:
    """도메인/등록처 신호로 기업 실존 여부를 판정한다."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        site_probe: SupportsSiteProbe | None = None,
        dns_probe: SupportsDnsProbe | None = None,
        registry_checker: SupportsRegistryActive | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._site_probe = site_probe
        self._dns_probe = dns_probe
        self._registry_checker = registry_checker

    def verify(
        self,
        domain: str | None,
        *,
        registry: str | None = None,
        registry_id: str | None = None,
    ) -> ExistenceResult:
        """도메인 생존(DNS+HTTP) + 등록처 신호로 실존성을 등급화 산정한다."""
        if self.settings.dry_run:
            alive = bool(domain)
            return ExistenceResult(
                is_active=alive, site_alive=alive, confidence=0.9 if alive else 0.0
            )

        site_alive = self._site().head_ok(domain) if domain else False
        dns_alive = self._dns().resolves(domain) if domain else False

        # 등록처 active 신호(주입 시) — 가장 강한 신호로 우선한다.
        registry_active = (
            self._registry_checker.is_active(registry, registry_id)
            if self._registry_checker is not None
            else None
        )
        if registry_active is True:
            return ExistenceResult(is_active=True, site_alive=site_alive, confidence=0.9)
        if registry_active is False:
            # 등록처가 defunct 로 보고 — 사이트가 살아있어도 실존 아님으로 본다(제약 ②).
            return ExistenceResult(is_active=False, site_alive=site_alive, confidence=0.85)

        # 등록처 신호 없음 → DNS/HTTP 생존 신호를 등급화 합성.
        if site_alive and dns_alive:
            confidence = 0.85  # 양 신호 일치 — 강한 실존.
        elif site_alive:
            confidence = 0.7  # HTTP 만 — 서비스 생존(DNS 조회 실패/누락).
        elif dns_alive:
            confidence = 0.5  # DNS 만 — 도메인 생존(사이트 일시 다운 가능, 사람 검토).
        else:
            confidence = 0.0
        return ExistenceResult(
            is_active=site_alive or dns_alive, site_alive=site_alive, confidence=confidence
        )

    def _site(self) -> SupportsSiteProbe:
        if self._site_probe is None:
            self._site_probe = HttpSiteProbe(timeout=self.settings.http_timeout)
        return self._site_probe

    def _dns(self) -> SupportsDnsProbe:
        if self._dns_probe is None:
            self._dns_probe = DnsProbe()
        return self._dns_probe
