"""발견 소스 공통 인터페이스 + dry_run 더미 소스.

실 소스(EDGAR/DART/거래소/CH/디렉터리/검색API)는 이 Protocol 을 구현한다.
dry_run 에서는 :class:`DummySource` 가 네트워크 없이 결정적 후보를 만든다.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from typing import Protocol

from pydantic import BaseModel, Field

from ..dedup import canonical_key


class Segment(BaseModel):
    """수집 단위: 국가 × 업종 × 상장여부."""

    country: str
    industry: str
    listed: str = "unknown"

    @property
    def label(self) -> str:
        return f"{self.country}/{self.industry}/{self.listed}"


class DiscoveredCompany(BaseModel):
    """발견 단계 산출 — 식별 정보 + canonical_key."""

    canonical_key: str
    name: str
    country: str = ""
    industry: str = ""
    listed: str = "unknown"
    domain: str | None = None
    registry: str | None = None
    registry_id: str | None = None
    source: str = ""
    segment: str | None = None


class DiscoverySource(Protocol):
    """벌크 발견 소스 인터페이스."""

    name: str

    def applies_to(self, segment: Segment) -> bool:
        """이 소스가 해당 세그먼트(국가·상장여부)에 적용 가능한지."""
        ...

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트에 해당하는 기업 목록을 반환한다."""
        ...


def is_country(segment: Segment, names: AbstractSet[str]) -> bool:
    """세그먼트 국가가 별칭 집합(소문자) 중 하나인지 판정한다(set/frozenset 모두 허용)."""
    return segment.country.strip().lower() in names


def build_company(
    *,
    source: str,
    segment: Segment,
    name: str,
    domain: str | None = None,
    registry: str | None = None,
    registry_id: str | None = None,
) -> DiscoveredCompany:
    """식별 정보로 ``canonical_key`` 를 산정해 :class:`DiscoveredCompany` 를 만든다."""
    key = canonical_key(
        registry=registry,
        registry_id=registry_id,
        domain=domain,
        name=name,
        country=segment.country,
    )
    return DiscoveredCompany(
        canonical_key=key,
        name=name,
        country=segment.country,
        industry=segment.industry,
        listed=segment.listed,
        domain=domain,
        registry=registry,
        registry_id=registry_id,
        source=source,
        segment=segment.label,
    )


class DummySource(BaseModel):
    """dry_run 용 결정적 더미 소스(네트워크 없음)."""

    name: str = "dummy"
    count: int = Field(default=3)

    def applies_to(self, segment: Segment) -> bool:  # noqa: ARG002 — 모든 세그먼트 적용
        """더미 소스는 모든 세그먼트에 적용된다."""
        return True

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트당 ``count`` 개의 결정적 더미 기업을 만든다."""
        out: list[DiscoveredCompany] = []
        for i in range(self.count):
            # 등록도메인(eTLD+1)이 i 마다 달라지도록 구성(서브도메인 축약 회피).
            domain = f"{segment.country.lower()}-firm{i}.com"
            out.append(
                DiscoveredCompany(
                    canonical_key=canonical_key(domain=domain),
                    name=f"{segment.industry} 더미기업 {i}",
                    country=segment.country,
                    industry=segment.industry,
                    domain=domain,
                    source=self.name,
                    segment=segment.label,
                )
            )
        return out
