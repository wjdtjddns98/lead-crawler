"""국민연금 가입 사업장 발견 소스 — 업종·규모 우선 KR 소재(로컬 스냅샷 소비).

CLI ``nps-import`` 가 적재한 월간 스냅샷(:mod:`~leadcrawler.storage.nps`)에서
세그먼트 업종(KSIC 접두)에 맞는 사업장을 **가입자수 내림차순(대형 우선)** 으로 emit
한다. 네트워크 0(전량 로컬 DB) — 발견 비용이 없어 쿼터·레이트와 무관하다.

강점: 연금 납부 중 = 살아있는 사업장(제약② 정합), 업종·규모를 발견 시점에 이미 안다.
약점: 홈페이지·전화 없음 → 다운스트림 도메인 해석(resolve_domains)이 수율 레버.
사업자등록번호는 원천 마스킹(앞 6자리) — canonical_key 는 name+지역(build_company).

커서: 세그먼트 라벨별 offset 을 cursor_store 에 영속(등록처와 동일 계약). 페이지가
cap 미만이면 소진 → 0 리셋(월간 스냅샷 교체 후 재검증 랩). 재발견은 dedup(제약①)이
걸러낸다.
"""

from __future__ import annotations

from typing import Protocol

from ..config import Settings
from ..logging import get_logger
from .base import (
    DiscoveredCompany,
    DiscoverySource,
    Segment,
    SupportsCursorStore,
    build_company,
    is_country,
)
from .http import HostRateLimiters
from .industry import ksic_prefixes
from .taxonomy import UNCLASSIFIED

log = get_logger("sources.nps")

_KR = {"kr", "kor", "korea", "south korea", "대한민국", "한국"}


class SupportsNpsStore(Protocol):
    """스냅샷 조회 계약(구현: storage.nps.NpsStore) — 실패는 빈 결과로 삼켜야 한다."""

    def page(self, prefixes: tuple[str, ...], *, offset: int, limit: int) -> list: ...

    def page_by_label(self, label: str, *, offset: int, limit: int) -> list: ...

    def mapped_labels(self) -> frozenset[str]: ...


class NpsSource(DiscoverySource):
    """국민연금 스냅샷 기반 KR 사업장 발견(대형 우선)."""

    name = "nps"

    def __init__(
        self,
        settings: Settings,
        *,
        count: int = 2,
        nps_store: SupportsNpsStore | None = None,
        cursor_store: SupportsCursorStore | None = None,
        rate_limiters: HostRateLimiters | None = None,
    ) -> None:
        self._settings = settings
        self._count = count
        self._store = nps_store
        self._cursor_store = cursor_store
        # 무네트워크 소스라 미사용 — build_sources 의 "전 소스 공유 limiter" 균일 계약용.
        self._rate_limiters = rate_limiters
        self._labels: frozenset[str] | None = None  # 3층 매핑 라벨 캐시(지연 1회 로드).

    def _mapped_labels(self) -> frozenset[str]:
        """3층 매핑 라벨 집합 — 인스턴스당 1회 로드(세그먼트마다 DB 히트 방지)."""
        if self._labels is None:
            self._labels = (
                self._store.mapped_labels() if self._store is not None else frozenset()
            )
        return self._labels

    def applies_to(self, segment: Segment) -> bool:
        """KR + (KSIC 접두 매핑 or 3층 통합매핑 보유 라벨) + (라이브면) 스토어 주입 시.

        3층(ksic_industry_map)이 채워지면 KSIC 접두표에 없는 30개 라벨(게임·은행 등)도
        열린다 — 매핑 없던 시절의 사각 63% 개방(적대 검토 HIGH-1 소비 경로).
        """
        if not is_country(segment, _KR):
            return False
        if self._settings.dry_run:
            return ksic_prefixes(segment.industry) is not None
        if self._store is None:
            return False
        if segment.industry in self._mapped_labels():
            return True
        return ksic_prefixes(segment.industry) is not None

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        if self._settings.dry_run:
            return self._dry(segment)
        if self._store is None:
            return []
        cap = self._settings.discovery_max_per_source
        offset = 0
        if self._cursor_store is not None:
            offset = max(0, self._cursor_store.get(self.name, segment.label))
        # 3층 라벨 매핑이 있으면 그 경로(42라벨 전체 커버·코드단위 통합), 없으면 접두 폴백.
        if segment.industry in self._mapped_labels():
            rows = self._store.page_by_label(segment.industry, offset=offset, limit=cap)
        else:
            prefixes = ksic_prefixes(segment.industry)
            if prefixes is None:
                return []
            rows = self._store.page(prefixes, offset=offset, limit=cap)
        out = [
            build_company(
                source=self.name,
                segment=segment,
                name=row.name,
                address=row.address,
            )
            for row in rows
            if row.name
        ]
        if self._cursor_store is not None:
            # 페이지가 cap 미만 = 매칭 소진 → 0 리셋(다음 스냅샷/랩 재검증 재개).
            nxt = 0 if len(rows) < cap else offset + len(rows)
            self._cursor_store.advance(self.name, segment.label, nxt)
        log.info("nps.live", segment=segment.label, n=len(out), offset=offset)
        return out

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크·DB 없는 결정적 더미.

        더미엔 도메인을 부여한다(다른 소스 dry 와 동일 컨벤션) — dry_run 실존 판정이
        '도메인 유무'로 결정적이라, 무도메인 더미는 persist 경로 테스트 계약을 깬다.
        (라이브는 도메인 없이 emit — resolve_domains 가 다운스트림 해석.)
        """
        if ksic_prefixes(segment.industry) is None:  # applies_to 와 대칭(방어).
            return []
        industry = segment.industry or UNCLASSIFIED
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{industry} 연금사업장 {i}",
                domain=f"kr-nps{i}.com",
                address=f"서울특별시 중구 더미로 {i}",
            )
            for i in range(self._count)
        ]
