"""이메일 딜리버러빌리티 API 검증 — 제3자 DB로 수신가능 여부를 보정(유료·opt-in).

형식·MX·SMTP 프로브 다음 단계로, ZeroBounce/NeverBounce 같은 딜리버러빌리티 API 에
이메일을 질의해 ``DELIVERABLE``/``UNDELIVERABLE``/``UNKNOWN`` 3분류 판정을 얻는다.
SMTP 프로브(:mod:`verify.email_validator`)와 동일한 주입형 프로토콜 패턴을 따른다.

- 각 제공자는 **키가 있을 때만** 활성(키 없으면 생성되지 않음).
- **호출당 과금/크레딧** 소모라 ``email_deliverability_check`` 기본 off + 호출마다 구조화
  로그(``verify.deliverability.call``)를 남겨 향후 cost_ledger 연결점으로 쓴다.
- 키오류/API오류/네트워크는 ``UNKNOWN`` 폴백(상태 무변경, 파이프라인 무중단).

테스트는 :class:`SupportsDeliverability` 가짜 구현 또는 가짜 페처를 주입해 네트워크 없이 검증한다.
"""

from __future__ import annotations

from typing import Protocol

from ..config import Settings
from ..logging import get_logger
from ..sources.http import SupportsFetch

log = get_logger("verify.deliverability")

# 판정 3분류(SMTP_* 와 동일 의미 — 검증기가 status 로 매핑).
DELIVERABLE = "deliverable"  # 수신 확정
UNDELIVERABLE = "undeliverable"  # 메일박스 없음/무효
UNKNOWN = "unknown"  # catch-all·판정불가·오류


class SupportsDeliverability(Protocol):
    """이메일 → 딜리버러빌리티 판정 인터페이스(테스트 더블이 구현)."""

    name: str  # 제공자 식별자(로그·provider 기록용)

    def check(self, email: str) -> str:
        """``DELIVERABLE``/``UNDELIVERABLE``/``UNKNOWN`` 중 하나(실패 시 UNKNOWN)."""
        ...


# ZeroBounce status → verdict (공식 docs 기준). 미수록 값(catch-all/unknown 등)은 UNKNOWN.
_ZB_MAP = {
    "valid": DELIVERABLE,
    "invalid": UNDELIVERABLE,
    "spamtrap": UNDELIVERABLE,
    "abuse": UNDELIVERABLE,
    "do_not_mail": UNDELIVERABLE,
}
# NeverBounce result → verdict. 미수록 값(catchall/unknown)은 UNKNOWN.
_NB_MAP = {
    "valid": DELIVERABLE,
    "invalid": UNDELIVERABLE,
    "disposable": UNDELIVERABLE,
}


class ZeroBounceChecker:
    """ZeroBounce v2 validate(GET) — ``status`` 를 3분류로 매핑한다.

    valid→DELIVERABLE, invalid/spamtrap/abuse/do_not_mail→UNDELIVERABLE,
    catch-all/unknown 등→UNKNOWN(과신 방지). 오류 시 UNKNOWN(graceful).
    """

    name = "zerobounce"
    _URL = "https://api.zerobounce.net/v2/validate"

    def __init__(self, api_key: str, *, fetcher: SupportsFetch) -> None:
        self._api_key = api_key
        self._fetcher = fetcher

    def check(self, email: str) -> str:
        try:
            data = self._fetcher.get_json(
                self._URL,
                params={"api_key": self._api_key, "email": email, "ip_address": ""},
            )
        except Exception as exc:  # 키오류·API오류·네트워크 → 판정불가.
            log.info("verify.deliverability.error", provider=self.name, err=str(exc))
            return UNKNOWN
        status = str((data or {}).get("status", "")).strip().lower()
        verdict = _ZB_MAP.get(status, UNKNOWN)
        log.info("verify.deliverability.call", provider=self.name, status=status, verdict=verdict)
        return verdict


class NeverBounceChecker:
    """NeverBounce v4 single check(POST, 쿼리스트링) — ``result`` 를 3분류로 매핑한다.

    valid→DELIVERABLE, invalid/disposable→UNDELIVERABLE, catchall/unknown→UNKNOWN.
    오류·``status!=success`` 시 UNKNOWN(graceful). 공식 docs 상 반드시 백엔드 호출.
    """

    name = "neverbounce"
    _URL = "https://api.neverbounce.com/v4/single/check"

    def __init__(self, api_key: str, *, fetcher: SupportsFetch) -> None:
        self._api_key = api_key
        self._fetcher = fetcher

    def check(self, email: str) -> str:
        try:
            # NeverBounce 는 POST + 파라미터를 쿼리스트링으로 받는다(본문 없음).
            data = self._fetcher.post_json(
                self._URL,
                params={"key": self._api_key, "email": email},
            )
        except Exception as exc:  # 키오류·API오류·네트워크 → 판정불가.
            log.info("verify.deliverability.error", provider=self.name, err=str(exc))
            return UNKNOWN
        body = data or {}
        if str(body.get("status", "")).strip().lower() != "success":
            return UNKNOWN  # auth_failure 등 → 판정불가(상태 무변경).
        result = str(body.get("result", "")).strip().lower()
        verdict = _NB_MAP.get(result, UNKNOWN)
        log.info("verify.deliverability.call", provider=self.name, result=result, verdict=verdict)
        return verdict


def build_deliverability_checker(
    settings: Settings, *, fetcher: SupportsFetch
) -> SupportsDeliverability | None:
    """키 있는 제공자 1개를 반환한다(ZeroBounce 우선, 없으면 NeverBounce, 둘 다 없으면 None).

    **단일 제공자·런타임 폴백 없음(의도)**: 딜리버러빌리티는 이메일당 과금이라, ZeroBounce 가
    UNKNOWN/오류여도 NeverBounce 로 재질의하지 않는다(이메일당 과금 2배 방지, 월 예산 보호).
    두 키가 모두 있으면 ZeroBounce 만 쓴다. 제공자를 바꾸려면 다른 키만 비우면 된다.
    """
    if settings.zerobounce_api_key:
        return ZeroBounceChecker(settings.zerobounce_api_key, fetcher=fetcher)
    if settings.neverbounce_api_key:
        return NeverBounceChecker(settings.neverbounce_api_key, fetcher=fetcher)
    return None
