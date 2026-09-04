"""네이버 지역검색(local.json) KR 발견 소스 — 업종-우선(순도 100%) 소재.

등록처(DART) 스캔은 업종을 알기 위해 corp 당 1콜을 태우고 순도가 낮다(~1.6%,
2026-07-10 실측). 지역검색은 반대로 **업종 키워드로 직접** 살아있는 업체를 돌려줘
(홈페이지 link·주소·전화 동봉) 저장 전환율이 높다. KR 지역 팬아웃(segment.region)과
결합하면 "수원 화학" 식으로 시/도별 롱테일을 캔다.

API 계약(2026-07 기준): display 상한 5·start 1 고정(페이지네이션 없음) — 쿼리당
최대 5업체. 그래서 쿼리 다변화(업종 라벨 '·' 분해 + 지역 프리픽스)가 물량 축이다.
무료 25,000쿼리/일(웹검색·도메인해석과 공유), 무과금(원장 기록 없음).

dry_run 은 네트워크 없이 결정적 더미를 반환한다(다른 소스와 동일 계약).
"""

from __future__ import annotations

from ..config import Settings
from ..cost_ledger import SupportsCostLedger
from ..dedup import normalize_domain
from ..logging import get_logger
from .base import DiscoveredCompany, DiscoverySource, Segment, build_company, is_country
from .domain_resolver import _is_noise_domain
from .http import Fetcher, HostRateLimiters, SupportsFetch
from .industry import is_broad_industry
from .search import _BLOCKLIST
from .search_provider import clean_search_text

log = get_logger("sources.naver_local")

_LOCAL_URL = "https://openapi.naver.com/v1/search/local.json"
_DISPLAY = 5  # local.json display 상한(하드 캡 — 올려도 5까지만 온다).
_KR = {"kr", "kor", "korea", "south korea", "대한민국", "한국"}


def _industry_terms(industry: str) -> list[str]:
    """택소노미 라벨을 지역검색 키워드로 — '화학·석유화학' → ['화학', '석유화학'].

    라벨 자체가 한국어라 그대로 검색어가 된다(글로벌 SERP 용 영어 동의어와 별개).
    broad('전체' 등)는 검색어가 못 되므로 빈 목록 — "전체" 를 그대로 검색하면
    업종무관 업체가 '미분류'로 유입돼 이 소스의 존재 이유(업종-우선 순도)가 무너진다.
    """
    if not industry.strip() or is_broad_industry(industry):
        return []
    return [t.strip() for t in industry.split("·") if t.strip()]


class NaverLocalSource(DiscoverySource):
    """네이버 지역검색 기반 KR 업체 발견(업종 키워드 직접 검색)."""

    name = "naver_local"
    # 지역 세그먼트(KR 팬아웃)에서도 돈다 — registry 의 지역 게이트가 이 속성을 본다.
    # (등록처·집계원은 주소로 열거하지 않아 지역마다 재스캔이 낭비지만, 지역검색은
    # 지역 프리픽스가 곧 검색축이라 팬아웃의 1차 수혜자다.)
    region_aware = True

    def __init__(
        self,
        settings: Settings,
        *,
        count: int = 2,
        fetcher: SupportsFetch | None = None,
        cost_ledger: SupportsCostLedger | None = None,  # noqa: ARG002 — 무과금(시그니처 통일).
        rate_limiters: HostRateLimiters | None = None,
    ) -> None:
        self._settings = settings
        self._count = count
        self._fetcher = fetcher
        self._rate_limiters = rate_limiters

    def applies_to(self, segment: Segment) -> bool:
        """KR 세그먼트 + 키 보유 + 업종 검색어 + 상장스코프 미지정일 때만."""
        if not is_country(segment, _KR):
            return False
        if segment.listed != "unknown":
            # 지역검색은 상장여부를 판별할 수 없는 소스 — listed 스코프 크롤에서 돌면
            # 미검증 업체가 그 스코프값으로 최초 저장돼 오염된다(이중 리뷰 MED, 게이트
            # 확장으로 region×listed 조합이 처음 실행 가능해지며 열린 표면).
            return False
        s = self._settings
        if not s.dry_run and not (s.naver_client_id and s.naver_client_secret):
            return False
        return bool(_industry_terms(segment.industry))

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        if self._settings.dry_run:
            return self._dry(segment)
        return self._live(segment)

    def close(self) -> None:
        close = getattr(self._fetcher, "close", None)
        if callable(close):
            close()

    def _client(self) -> SupportsFetch:
        if self._fetcher is None:
            self._fetcher = Fetcher(
                user_agent=self._settings.discovery_user_agent,
                min_interval=self._settings.http_request_delay,
                timeout=self._settings.http_timeout,
                rate_limiters=self._rate_limiters,
            )
        return self._fetcher

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(지역 태그 포함 — 팬아웃이 dedup 에 안 접히게)."""
        tag = f"-{segment.region.encode('utf-8').hex()}" if segment.region else ""
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{segment.region or ''} {segment.industry} 지역업체 {i}".strip(),
                domain=f"kr{tag}-local{i}.com",
            )
            for i in range(self._count)
        ]

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """업종 키워드(라벨 '·' 분해)마다 지역검색 1콜 — 쿼리당 최대 5업체.

        지역 세그먼트면 지역을 프리픽스로("수원 화학"). 업체명 dedup 은 여기서 1차
        (쿼리 간 같은 업체), 전역 dedup(제약 ①)은 registry/파이프라인이 담당한다.
        """
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        prefix = f"{segment.region} " if segment.region else ""
        headers = {
            "X-Naver-Client-Id": self._settings.naver_client_id,
            "X-Naver-Client-Secret": self._settings.naver_client_secret,
        }
        out: list[DiscoveredCompany] = []
        seen_names: set[str] = set()
        calls = 0  # 쿼터 소비 계측 — 25k/일을 도메인 해석과 공유(로그로 산정 근거 제공).
        for term in _industry_terms(segment.industry):
            query = f"{prefix}{term}".strip()
            calls += 1
            try:
                payload = fetcher.get_json(
                    _LOCAL_URL,
                    params={"query": query, "display": _DISPLAY, "start": 1},
                    headers=headers,
                )
            except Exception as exc:  # 쿼리 1건 실패는 다음 키워드로(배치 보호).
                log.info("naver_local.error", query=query, err=str(exc))
                continue
            items = payload.get("items") if isinstance(payload, dict) else None
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                # <b> 태그 제거 + HTML 엔티티 복원("AT&amp;T"→"AT&T") — 엔티티를 남기면
                # name: canonical_key 가 오염돼 동일기업 dedup(제약①)이 어긋난다.
                name = clean_search_text(str(it.get("title") or ""))
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                # link 는 원시 URL('http://x.co.kr/', 'https://blog.naver.com/..') — 그대로
                # 저장하면 enrich/existence 가 URL 을 호스트명으로 취급해 전면 getaddrinfo
                # 실패한다(2026-08-18 실사고, 승격 0). 정규화 후 공유 플랫폼(포털/블로그/
                # SNS·노이즈)이면 도메인을 **폐기**한다 — normalize 는 등록가능 루트까지
                # 접으므로(blog.naver.com/A·/B → naver.com) 살려두면 서로 다른 업체가
                # dom:naver.com 한 행에 뭉개져 영구 유실되고(제약①) 포털 루트가 승격된다
                # (이중 리뷰 HIGH 합치). None 폴백이면 name: 티어 키로 업체가 분리 보존되고
                # resolve 백필이 진짜 도메인을 찾을 여지도 남는다.
                dom = normalize_domain(str(it.get("link") or "").strip() or None)
                if dom is not None and (dom in _BLOCKLIST or _is_noise_domain(dom)):
                    dom = None
                out.append(
                    build_company(
                        source=self.name,
                        segment=segment,
                        name=name,
                        domain=dom,
                        address=str(it.get("address") or "").strip() or None,
                        phone=str(it.get("telephone") or "").strip() or None,
                    )
                )
                if len(out) >= cap:
                    log.info("naver_local.live", segment=segment.label, n=len(out), calls=calls)
                    return out
        log.info("naver_local.live", segment=segment.label, n=len(out), calls=calls)
        return out
