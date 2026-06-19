"""SEC EDGAR 발견 소스 — 미국 상장(공시)기업.

dry_run 에서는 네트워크 없이 결정적 더미를 반환한다. 라이브 발견(키/UA 연동)은
M2 에서 구현한다 — ``edgar_user_agent`` 가 없으면 비활성(no-op)으로 동작한다.
"""

from __future__ import annotations

from ..config import Settings
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company, is_country

log = get_logger("sources.edgar")

# EDGAR 적용 국가 별칭(소문자).
_US = {"us", "usa", "united states", "미국"}


class EdgarSource:
    """SEC EDGAR 기반 미국 상장기업 발견 소스."""

    name = "edgar"

    def __init__(self, settings: Settings, *, count: int = 2) -> None:
        self._settings = settings
        self._count = count

    def applies_to(self, segment: Segment) -> bool:
        """미국 세그먼트에 적용된다.

        EDGAR 는 본래 상장/공시기업 대상이나, M1 에서는 ``segment.listed`` 필터링을
        하지 않는다(상장여부 게이팅은 M2 라이브 발견과 함께 도입).
        """
        return is_country(segment, _US)

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트에 해당하는 미국 기업 목록을 반환한다."""
        if self._settings.dry_run:
            return self._dry(segment)
        if not self._settings.edgar_user_agent:
            log.info("edgar.skip.no_user_agent")
            return []
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(registry_id 기반 canonical_key)."""
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{segment.industry} EDGAR Corp {i}",
                domain=f"us-edgar{i}.com",
                registry="sec",
                registry_id=f"CIK{i:07d}",
            )
            for i in range(self._count)
        ]

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """실 EDGAR 발견 — M2 구현 예정."""
        raise NotImplementedError("EDGAR 라이브 발견은 M2 — edgar_user_agent 연동 예정")
