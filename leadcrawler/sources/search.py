"""검색엔진 발견 소스 — 국가/업종 무관 범용 후보 발굴(상장·비상장 모두).

상장 등록처(EDGAR/DART 등)로 잡히지 않는 비상장·중소기업을 검색 API(Google CSE,
Bing)로 발굴하는 보강용 소스. dry_run 에서는 네트워크 없이 결정적 더미를 반환하며,
라이브는 M2 에서 구현한다 — 검색 키가 하나도 없으면 비활성(no-op)으로 동작한다.
"""

from __future__ import annotations

from ..config import Settings
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company

log = get_logger("sources.search")


class SearchSource:
    """검색엔진 기반 범용 발견 소스(모든 세그먼트 적용)."""

    name = "search"

    def __init__(self, settings: Settings, *, count: int = 2) -> None:
        self._settings = settings
        self._count = count

    def applies_to(self, segment: Segment) -> bool:  # noqa: ARG002 — 전 세그먼트 적용
        """검색 발견은 모든 세그먼트에 적용된다."""
        return True

    def _has_key(self) -> bool:
        """Google CSE 또는 Bing 키가 하나라도 설정되어 있는지."""
        s = self._settings
        return bool((s.google_cse_key and s.google_cse_cx) or s.bing_api_key)

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트에 해당하는 후보 기업 목록을 반환한다."""
        if self._settings.dry_run:
            return self._dry(segment)
        if not self._has_key():
            log.info("search.skip.no_key")
            return []
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(도메인 기반 canonical_key)."""
        cc = (segment.country or "xx").strip().lower()
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{segment.industry} 서치기업 {i}",
                domain=f"{cc}-search{i}.com",
            )
            for i in range(self._count)
        ]

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """실 검색 발견 — M2 구현 예정."""
        raise NotImplementedError("검색 라이브 발견은 M2 — google_cse/bing 연동 예정")
