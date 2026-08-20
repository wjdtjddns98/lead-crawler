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
from ..dedup import normalize_name
from ..logging import get_logger
from .base import (
    DiscoveredCompany,
    DiscoverySource,
    Segment,
    SupportsCursorStore,
    build_company,
    is_country,
)
# DART corp_cls → listed/market 세분화 표 재사용(같은 corp 를 같은 규칙으로 해석).
from .dart import _LISTED_CLS, _MARKET_CLS
from .http import HostRateLimiters
from .industry import ksic_prefixes
from .taxonomy import UNCLASSIFIED

log = get_logger("sources.nps")

_KR = {"kr", "kor", "korea", "south korea", "대한민국", "한국"}


class SupportsNpsStore(Protocol):
    """스냅샷 조회 계약(구현: storage.nps.NpsStore) — 실패는 빈 결과로 삼켜야 한다."""

    def page_by_label(self, label: str, *, offset: int, limit: int) -> list: ...

    def mapped_labels(self) -> frozenset[str]: ...


class SupportsDartJoin(Protocol):
    """DART 캐시 조인 계약(구현: storage.dart_cache.DbDartCorpCache.find_matches).

    조회 실패는 None(빈 dict=진짜 미스와 구분) — 미스=비상장 추론의 전제.
    """

    def find_matches(self, pairs: list[tuple[str, str]]) -> dict | None: ...


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
        dart_cache: SupportsDartJoin | None = None,
    ) -> None:
        self._settings = settings
        self._count = count
        self._store = nps_store
        self._cursor_store = cursor_store
        # DART 캐시 조인(주입 시) — 발견 시점에 홈페이지·사업자번호·상장 필드를 무쿼터 부착.
        self._dart_cache = dart_cache
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
        """KR + 3층 통합매핑 보유 라벨 + (라이브면) 스토어 주입 시.

        라이브는 3층(ksic_industry_map) 라벨 매핑만 쓴다 — NPS 업종코드는 KSIC 10차가
        아니라 구체계(실측: 61=수상운송·21=제지·26=요업)라, 10차 접두표(ksic_prefixes)
        필터가 엉뚱한 사업장을 골라낸다(2026-07-14 실사고: 통신 세그먼트에 해운사 유입).
        접두 폴백 제거 — 매핑은 nps-map-industries 가 전 라벨을 커버한다.
        dry_run 은 네트워크 0 결정성 계약용 게이트로만 접두표를 유지한다(_dry 와 대칭).
        """
        if not is_country(segment, _KR):
            return False
        if self._settings.dry_run:
            return ksic_prefixes(segment.industry) is not None
        if self._store is None:
            return False
        return segment.industry in self._mapped_labels()

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        if self._settings.dry_run:
            return self._dry(segment)
        if self._store is None:
            return []
        cap = self._settings.discovery_max_per_source
        offset = 0
        if self._cursor_store is not None:
            offset = max(0, self._cursor_store.get(self.name, segment.label))
        # 3층 라벨 매핑 경로 단독(전 라벨 커버·코드단위 통합) — 10차 접두 폴백은
        # NPS 코드체계 불일치 오라벨(applies_to docstring)로 제거. 미매핑 라벨은 빈 결과.
        if segment.industry not in self._mapped_labels():
            return []
        rows = self._store.page_by_label(segment.industry, offset=offset, limit=cap)
        # DART 캐시 조인 — (사업자번호 앞6 + 정규화명) 정확 매치만(정밀도 우선). 히트는
        # DART 와 같은 reg: 키(registry_id=corp_code)로 emit 해 과거/미래 DART 발견분과
        # canonical_key 가 완전히 합쳐진다(제약① 강화) + 홈페이지·상장 필드 무쿼터 부착.
        # matches=None 은 캐시 조회 실패(미스 아님 — unlisted 추론 금지), {}는 진짜 미스.
        matches: dict | None = {}
        row_keys: dict[int, tuple[str, str]] = {}  # 행당 정규화 1회(재계산 방지).
        if self._dart_cache is not None and rows:
            for idx, row in enumerate(rows):
                if row.name:
                    row_keys[idx] = (
                        (row.bizno_prefix or "")[:6], normalize_name(row.name)[:255]
                    )
            matches = self._dart_cache.find_matches(
                [p for p in row_keys.values() if p[0] and p[1]]
            )
        lookup = matches if matches is not None else {}

        out: list[DiscoveredCompany] = []
        for idx, row in enumerate(rows):
            if not row.name:
                continue
            hit = lookup.get(row_keys.get(idx, ("", "")))
            info = getattr(hit, "info", None) if hit is not None else None
            if hit is not None and isinstance(info, dict):
                corp_cls = str(info.get("corp_cls") or "")
                out.append(
                    build_company(
                        source=self.name,
                        segment=Segment(
                            country=segment.country,
                            industry=segment.industry,
                            listed=_LISTED_CLS.get(corp_cls, segment.listed),
                        ),
                        name=row.name,
                        domain=str(info.get("hm_url") or "") or None,
                        registry="dart",
                        registry_id=hit.corp_code,
                        address=row.address,
                        reg_no=str(info.get("bizr_no") or "") or None,
                        ticker=str(info.get("stock_code") or "") or None,
                        phone=str(info.get("phn_no") or "") or None,
                        ir_url=str(info.get("ir_url") or "") or None,
                        name_eng=str(info.get("corp_name_eng") or "") or None,
                        market=_MARKET_CLS.get(corp_cls),
                        listed_verified=corp_cls in _LISTED_CLS,
                    )
                )
                continue
            # 조인 미스 = DART 캐시에 없는 법인 → 사실상 비상장(PO 결정 2026-07-13).
            # 유효한 키(사업자번호 6자리 숫자+정규화명)로 실제 조회하고도 미스일 때만 —
            # 키 불충분(빈/손상 사업자번호)·캐시 미주입·조회 실패(matches=None)면
            # 세그먼트 기본값 유지. 캐시 미완충기의 오표기(실상장인데 미스)는
            # nps-relink-dart 와 save_discovered 검증값 백필이 소급 교정한다.
            key = row_keys.get(idx)
            seg = segment
            if (
                matches is not None
                and key is not None
                and len(key[0]) == 6
                and key[0].isdigit()
                and key[1]
            ):
                seg = segment.model_copy(update={"listed": "unlisted"})
            out.append(
                build_company(
                    source=self.name,
                    segment=seg,
                    name=row.name,
                    address=row.address,
                )
            )
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
