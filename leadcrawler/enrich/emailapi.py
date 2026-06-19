"""Email Finder API — 도메인으로 제3자 이메일 DB를 질의해 보강(유료·escalation).

정적·헤드리스·OCR 로도 이메일이 0건일 때, 회사 도메인을 Hunter/Apollo 같은 이메일
탐색 API 에 질의해 IR/일반 이메일을 보강한다. escalation 의 paid-외부 티어로 Vision 직전.

- 각 제공자는 **키가 있을 때만** 활성(키 없으면 생성되지 않음).
- **호출당 과금/크레딧** 소모라 ``enrich_email_api`` 기본 off + ``email_api_max_results``
  로 제공자당 후보를 제한하고, 호출마다 구조화 로그(``enrich.email_api.call``)를 남겨
  향후 cost_ledger 연결점으로 쓴다.
- 반환 이메일은 사이트 추출과 **동일한 role 필터**(:func:`emails_from_text` → IR 우선,
  HR·언론·개인 배제)를 거친다. 미설치/키오류/API오류는 빈 리스트 폴백(파이프라인 무중단).

테스트는 :class:`SupportsEmailFinder` 가짜 구현 또는 가짜 페처를 주입해 네트워크 없이 검증한다.
"""

from __future__ import annotations

from typing import Protocol

from ..logging import get_logger
from ..sources.http import SupportsFetch

log = get_logger("enrich.email_api")


class SupportsEmailFinder(Protocol):
    """도메인 → 이메일 후보 탐색 인터페이스(테스트 더블이 구현)."""

    name: str  # 제공자 식별자(로그·디버깅용)
    source: str  # Contact.source_url 에 기록할 출처 URL

    def find_emails(self, domain: str, *, limit: int = 5) -> list[str]:
        """도메인에서 이메일 문자열 후보를 반환한다(실패 시 빈 리스트)."""
        ...


class HunterFinder:
    """Hunter.io Domain Search — 도메인의 일반(generic) 이메일을 찾는다.

    ``type=generic`` 으로 개인 이메일(personal)을 애초에 제외 요청해 과금·노이즈를 줄인다
    (ir@·info@·contact@ 등은 generic 으로 반환된다). 오류 시 빈 리스트(graceful).
    """

    name = "hunter"
    source = "https://hunter.io"
    _URL = "https://api.hunter.io/v2/domain-search"

    def __init__(self, api_key: str, *, fetcher: SupportsFetch) -> None:
        self._api_key = api_key
        self._fetcher = fetcher

    def find_emails(self, domain: str, *, limit: int = 5) -> list[str]:
        try:
            data = self._fetcher.get_json(
                self._URL,
                params={
                    "domain": domain,
                    "api_key": self._api_key,
                    "limit": limit,
                    "type": "generic",  # 개인 이메일 제외(정책상 generic/IR 만 채택).
                },
            )
        except Exception as exc:  # 키오류·API오류·네트워크 → 빈 결과.
            log.info("enrich.email_api.error", provider=self.name, err=str(exc))
            return []
        emails = (data or {}).get("data", {}).get("emails", []) or []
        out = [str(e["value"]).strip() for e in emails if isinstance(e, dict) and e.get("value")]
        log.info("enrich.email_api.call", provider=self.name, n=len(out))
        return out[:limit]


class ApolloFinder:
    """Apollo People Search — 도메인 소속 인물의 이메일을 찾는다(잠금 placeholder 제외).

    Apollo 는 인물 중심이라 반환 이메일 다수가 개인(personal)이며, 이는 상위 role 필터에서
    배제된다(정책: IR/일반만 채택). 잠금 미해제(``email_not_unlocked``) 자리표시자는
    여기서 먼저 거른다. 오류 시 빈 리스트(graceful).
    """

    name = "apollo"
    source = "https://apollo.io"
    _URL = "https://api.apollo.io/v1/mixed_people/search"

    def __init__(self, api_key: str, *, fetcher: SupportsFetch) -> None:
        self._api_key = api_key
        self._fetcher = fetcher

    def find_emails(self, domain: str, *, limit: int = 5) -> list[str]:
        try:
            data = self._fetcher.post_json(
                self._URL,
                json={"q_organization_domains": domain, "page": 1, "per_page": limit},
                headers={"X-Api-Key": self._api_key, "Cache-Control": "no-cache"},
            )
        except Exception as exc:  # 키오류·API오류·네트워크 → 빈 결과.
            log.info("enrich.email_api.error", provider=self.name, err=str(exc))
            return []
        people = (data or {}).get("people", []) or []
        out: list[str] = []
        for p in people:
            email = (p.get("email") or "").strip() if isinstance(p, dict) else ""
            if email and "not_unlocked" not in email:  # 미해제 자리표시자 제외.
                out.append(email)
        log.info("enrich.email_api.call", provider=self.name, n=len(out))
        return out[:limit]
