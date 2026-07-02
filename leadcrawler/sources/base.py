"""발견 소스 공통 인터페이스 + dry_run 더미 소스.

실 소스(EDGAR/DART/거래소/CH/디렉터리/검색API)는 이 Protocol 을 구현한다.
dry_run 에서는 :class:`DummySource` 가 네트워크 없이 결정적 후보를 만든다.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from typing import Protocol

from pydantic import BaseModel, Field

from ..dedup import canonical_key
from ..logging import get_logger
from .industry import resolve_industry_label

log = get_logger("sources.cursor")


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


class SupportsCursorStore(Protocol):
    """등록처 발견 커서 저장소 — 런 간 스캔 위치 영속(구현: storage.discovery_cursor).

    커서는 최적화일 뿐 정확성 불변: 잃어도 다음 런이 같은 구간을 재스캔하고
    dedup(제약 ①)이 걸러낸다. 구현은 실패를 삼키고 get 은 0 폴백해야 한다.
    """

    def get(self, source: str, key: str) -> int:
        """마지막으로 영속된 다음 스캔 위치(없으면 0)."""
        ...

    def advance(self, source: str, key: str, position: int) -> None:
        """다음 런이 시작할 위치를 영속한다."""
        ...


def cursor_offset(
    store: SupportsCursorStore | None, source: str, segment: Segment, total: int
) -> int:
    """영속 커서에서 이번 런의 시작 offset 을 읽는다(store 없음·범위 밖이면 0).

    모집단 목록이 런 사이에 줄어 offset 이 끝을 넘으면 0 으로 되감는다(재검증 재개).
    """
    if store is None or total <= 0:
        return 0
    offset = store.get(source, segment.label)
    return offset if 0 <= offset < total else 0


def advance_cursor(
    store: SupportsCursorStore | None,
    source: str,
    segment: Segment,
    position: int,
    total: int,
) -> None:
    """다음 런 시작 위치를 영속한다. 모집단 끝 도달 시 0 리셋 + exhausted 로그."""
    if store is None or total <= 0:
        return
    if position >= total:
        log.info("cursor.exhausted", source=source, segment=segment.label, total=total)
        position = 0
    store.advance(source, segment.label, position)


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
    industry_code_label: str | None = None,
) -> DiscoveredCompany:
    """식별 정보로 ``canonical_key`` 를 산정해 :class:`DiscoveredCompany` 를 만든다.

    ``industry`` 는 :func:`resolve_industry_label` 로 정한다: 구체 업종 검색이면 세그먼트
    업종 그대로, broad('전체' 등)면 등록처 코드에서 복원한 ``industry_code_label``(명확
    단일매치, 등록처 소스만 전달)을 쓰고 없으면 '미분류'(파이프라인이 이후 LLM 배치 시도).
    ``segment`` 라벨(provenance)은 원래 세그먼트 업종을 유지한다 — 구분 컬럼과 별개.
    """
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
        industry=resolve_industry_label(segment.industry, code_label=industry_code_label),
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
                    industry=resolve_industry_label(segment.industry),
                    domain=domain,
                    source=self.name,
                    segment=segment.label,
                )
            )
        return out
