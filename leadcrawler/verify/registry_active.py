"""등록처 active 신호 체커 — :class:`SupportsRegistryActive` 실구현(주입형).

발견 단계에서 부여된 ``registry``/``registry_id`` 로 공식 등록처에 현재 상태를 조회해
실존성 판정의 **가장 강한 신호**(active=0.9 우선)를 제공한다. 지원 등록처:

- ``companies_house``: ``/company/{번호}`` 의 ``company_status`` (active → True,
  dissolved/liquidation 등 → False). HTTP Basic(키:공백).
- ``opencorporates``: ``/companies/{관할}/{번호}`` 의 ``inactive`` (False → True,
  True → False). api_token 쿼리.

**계약(중요)**: 룩업 실패·미지원 등록처·키 없음·불확실은 반드시 ``None`` 을 반환한다.
``False`` 는 등록처가 **명시적으로 폐업**으로 보고한 경우만 — False 는 실존 기업을 reject
(저장 차단)하므로 오류를 False 로 흘리면 안 된다(제약 ②의 안전한 방향).

dry_run 에서는 파이프라인이 이 체커를 주입하지 않는다(실존 판정이 도메인 유무로 결정적).
테스트는 :class:`SupportsFetch` 가짜 페처를 주입해 네트워크 없이 분기를 검증한다.
"""

from __future__ import annotations

import base64
from typing import Any

from ..config import Settings
from ..logging import get_logger
from ..sources.http import Fetcher, SupportsFetch

log = get_logger("verify.registry_active")

_CH_URL = "https://api.company-information.service.gov.uk/company/{number}"
_OC_URL = "https://api.opencorporates.com/v0.4/companies/{jurisdiction}/{number}"
# Companies House 가 명시적으로 폐업/소멸로 보는 상태(이외는 active 계열 또는 불확실).
_CH_DEFUNCT = {
    "dissolved", "liquidation", "receivership", "administration",
    "converted-closed", "insolvency-proceedings", "removed",
}


class RegistryActiveChecker:
    """등록처별 active 상태 조회기(키게이트·graceful None)."""

    def __init__(self, settings: Settings, *, fetcher: SupportsFetch | None = None) -> None:
        self._settings = settings
        self._fetcher = fetcher

    def is_active(self, registry: str | None, registry_id: str | None) -> bool | None:
        """등록처가 active/폐업을 보고하면 True/False, 판정 불가면 None."""
        if not registry or not registry_id:
            return None
        reg = registry.strip().lower()
        try:
            if reg == "companies_house":
                return self._companies_house(registry_id.strip())
            if reg == "opencorporates":
                return self._opencorporates(registry_id.strip())
        except Exception as exc:  # 네트워크·형식·키 오류 → 불확실(None), 절대 False 금지.
            log.info("registry_active.error", registry=reg, err_type=type(exc).__name__)
            return None
        return None  # 미지원 등록처(lei/edgar/dart/거래소 등) → 불확실.

    def _client(self) -> SupportsFetch:
        if self._fetcher is None:
            self._fetcher = Fetcher(
                user_agent=self._settings.discovery_user_agent,
                min_interval=self._settings.http_request_delay,
                timeout=self._settings.http_timeout,
            )
        return self._fetcher

    def _companies_house(self, number: str) -> bool | None:
        key = self._settings.companies_house_api_key
        if not key:
            return None
        token = base64.b64encode(f"{key}:".encode()).decode()
        payload = self._client().get_json(
            _CH_URL.format(number=number), headers={"Authorization": f"Basic {token}"}
        )
        status = payload.get("company_status") if isinstance(payload, dict) else None
        if not status:
            return None  # 상태 미보고 → 불확실.
        status = str(status).strip().lower()
        if status in _CH_DEFUNCT:
            return False  # 명시적 폐업.
        if status == "active":
            return True
        return None  # 그 외(예: open/registered 외 모호) → 불확실.

    def _opencorporates(self, registry_id: str) -> bool | None:
        key = self._settings.opencorporates_api_key
        if not key or "/" not in registry_id:
            return None
        jurisdiction, _, number = registry_id.partition("/")
        if not jurisdiction or not number:
            return None
        payload = self._client().get_json(
            _OC_URL.format(jurisdiction=jurisdiction, number=number),
            params={"api_token": key},
        )
        company = _oc_company(payload)
        if company is None:
            return None
        inactive = company.get("inactive")
        if inactive is True:
            return False  # 명시적 폐업.
        if inactive is False:
            return True
        return None  # null/누락(상태 미추적 관할) → 불확실.


def build_registry_checker(
    settings: Settings, *, fetcher: SupportsFetch | None = None
) -> RegistryActiveChecker | None:
    """관련 키가 하나라도 있으면 체커를, 없으면 None 을 반환한다(미주입=미사용)."""
    if settings.companies_house_api_key or settings.opencorporates_api_key:
        return RegistryActiveChecker(settings, fetcher=fetcher)
    return None


def _oc_company(payload: Any) -> dict[str, Any] | None:
    """OpenCorporates 단건 응답에서 company dict 를 안전 추출한다(형식불일치 시 None)."""
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if not isinstance(results, dict):
        return None
    company = results.get("company")
    return company if isinstance(company, dict) else None
