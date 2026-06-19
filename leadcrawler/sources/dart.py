"""DART(금융감독원 전자공시) 발견 소스 — 한국 상장/공시기업.

dry_run 에서는 네트워크 없이 결정적 더미를 반환한다. 라이브 발견은 M2 에서
구현한다 — ``dart_api_key`` 가 없으면 비활성(no-op)으로 동작한다.
"""

from __future__ import annotations

from ..config import Settings
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company, is_country

log = get_logger("sources.dart")

# DART 적용 국가 별칭(소문자).
_KR = {"kr", "kor", "korea", "south korea", "대한민국", "한국"}


class DartSource:
    """DART 기반 한국 상장기업 발견 소스."""

    name = "dart"

    def __init__(self, settings: Settings, *, count: int = 2) -> None:
        self._settings = settings
        self._count = count

    def applies_to(self, segment: Segment) -> bool:
        """한국 세그먼트에 적용된다.

        DART 는 본래 상장/공시기업 대상이나, M1 에서는 ``segment.listed`` 필터링을
        하지 않는다(상장여부 게이팅은 M2 라이브 발견과 함께 도입).
        """
        return is_country(segment, _KR)

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트에 해당하는 한국 기업 목록을 반환한다."""
        if self._settings.dry_run:
            return self._dry(segment)
        if not self._settings.dart_api_key:
            log.info("dart.skip.no_key")
            return []
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(registry_id 기반 canonical_key)."""
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{segment.industry} 디에이알티 {i}",
                domain=f"kr-dart{i}.co.kr",
                registry="dart",
                registry_id=f"KR{i:08d}",
            )
            for i in range(self._count)
        ]

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """실 DART 발견 — M2 구현 예정."""
        raise NotImplementedError("DART 라이브 발견은 M2 — dart_api_key 연동 예정")
