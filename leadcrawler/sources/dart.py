"""DART(금융감독원 전자공시) 발견 소스 — 한국 상장/공시기업.

dry_run: 네트워크 없이 결정적 더미. 라이브: OpenDART 2-패스
1. ``corpCode.xml``(ZIP) 로 전체 고유번호 목록 → ``stock_code`` 보유분(상장) 필터,
2. 각 ``company.json`` 으로 홈페이지(hm_url)·업종(induty_code)·시장(corp_cls) 취득 후
   업종 접두 매칭(KSIC)으로 거른다.
호출량은 ``discovery_max_per_source`` 로 상한한다(예산·레이트리밋 보호).
``dart_api_key`` 가 없으면 비활성(no-op).
"""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

from ..config import Settings
from ..dedup import normalize_domain
from ..logging import get_logger
from .base import (
    DiscoveredCompany,
    Segment,
    SupportsCursorStore,
    advance_cursor,
    build_company,
    cursor_offset,
    is_country,
    opt_str,
)
from .http import Fetcher, HostRateLimiters, SupportsFetch
from .industry import industry_from_ksic, ksic_prefixes, matches_prefix

log = get_logger("sources.dart")

_KR = {"kr", "kor", "korea", "south korea", "대한민국", "한국"}
_CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
_COMPANY_URL = "https://opendart.fss.or.kr/api/company.json"
# 상장 시장 구분(corp_cls): 유가증권(Y)/코스닥(K)/코넥스(N)=상장, 기타법인(E)=비상장.
# 주의: corpCode.xml 은 상장폐지 기업도 stock_code 를 유지한다(2026-07-02 실측 —
# corp_cls='E' + stock_code 잔존, 예: 한빛네트). 즉 E 는 '한때 상장·현재 비상장' 포함
# → unlisted 로 세분화(과거처럼 unknown 으로 뭉개지 않는다).
_LISTED_CLS = {"Y": "listed", "K": "listed", "N": "listed", "E": "unlisted"}
# 시장(보드) 세분화 — E(비상장)는 시장이 없으므로 None.
_MARKET_CLS = {"Y": "KOSPI", "K": "KOSDAQ", "N": "KONEX"}
# 업종 필터 시 cap*10 까지 넓게 스캔하되, 후보당 company.json 1 콜이 발생하므로(무료 쿼터
# 보호) 절대 상한을 둔다 — 예산가드는 Serper 유료검색에만 걸려 무료 등록처는 무방비라서다.
_SCAN_LIMIT_ABS = 2000


class DartSource:
    """DART 기반 한국 상장기업 발견 소스."""

    name = "dart"

    def __init__(
        self,
        settings: Settings,
        *,
        count: int = 2,
        fetcher: SupportsFetch | None = None,
        rate_limiters: HostRateLimiters | None = None,
        cursor_store: SupportsCursorStore | None = None,
    ) -> None:
        self._settings = settings
        self._count = count
        self._fetcher = fetcher
        self._rate_limiters = rate_limiters
        self._cursor_store = cursor_store

    def applies_to(self, segment: Segment) -> bool:
        """한국 세그먼트에 적용된다(상장여부 게이팅은 라이브에서 corp_cls 로)."""
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

    def _client(self) -> SupportsFetch:
        # 소스 인스턴스당 1개만 생성·재사용(discover 호출마다 클라이언트 누수 방지).
        if self._fetcher is None:
            self._fetcher = Fetcher(
                min_interval=self._settings.http_request_delay,
                timeout=self._settings.http_timeout,
                rate_limiters=self._rate_limiters,
            )
        return self._fetcher

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """실 OpenDART 발견(2-패스 + 업종/캡 필터)."""
        fetcher = self._client()
        key = self._settings.dart_api_key
        cap = self._settings.discovery_max_per_source
        prefixes = ksic_prefixes(segment.industry)

        try:
            listed = _parse_corp_codes(
                fetcher.get_bytes(_CORP_CODE_URL, params={"crtfc_key": key})
            )
        except Exception as exc:  # 깨진 ZIP/응답이면 전체 크래시 대신 빈 결과.
            log.info("dart.corpcode.error", err=str(exc))
            return []
        # 업종 필터가 없으면 상한만큼만, 있으면 더 넓게 스캔(상한*10·절대상한 cap)하며 매칭 수집.
        scan_limit = cap if prefixes is None else min(cap * 10, _SCAN_LIMIT_ABS)
        # 런 간 커서(딥백필): 지난 런이 멈춘 위치부터 이어 스캔한다(매 런 같은 머리 재방문 방지).
        # 상류 목록(corpCode.xml) 순서가 런 간 대체로 안정하다고 가정 — 흔들려도 오염은 없고
        # (제약① dedup) 일부 항목이 다음 소진 사이클(0 리셋)까지 늦어질 뿐(self-heal).
        offset = cursor_offset(self._cursor_store, self.name, segment, len(listed))
        scanned = 0

        out: list[DiscoveredCompany] = []
        for corp_code, corp_name in listed[offset : offset + scan_limit]:
            scanned += 1
            try:
                info = fetcher.get_json(
                    _COMPANY_URL, params={"crtfc_key": key, "corp_code": corp_code}
                )
            except Exception as exc:  # 개별 실패는 건너뛴다(배치 보호).
                log.info("dart.company.error", corp_code=corp_code, err=str(exc))
                continue
            if not isinstance(info, dict) or info.get("status") != "000":
                continue
            if not matches_prefix(info.get("induty_code"), prefixes):
                continue
            out.append(
                build_company(
                    source=self.name,
                    segment=Segment(
                        country=segment.country,
                        industry=segment.industry,
                        listed=_LISTED_CLS.get(info.get("corp_cls", ""), "unknown"),
                    ),
                    name=info.get("corp_name") or corp_name,
                    domain=normalize_domain(info.get("hm_url")),
                    registry="dart",
                    registry_id=corp_code,
                    # broad 검색 시 구분을 대분류로 복원(KSIC induty_code). 구체 검색이면
                    # resolve 가 세그먼트 업종을 그대로 써 이 값은 무시된다.
                    industry_code_label=industry_from_ksic(info.get("induty_code")),
                    # 풍부필드 — 같은 응답이 이미 주는 값(추가 호출 0). region 은
                    # build_company 가 adres 에서 시/도로 파생한다.
                    address=opt_str(info.get("adres")),
                    reg_no=opt_str(info.get("bizr_no")),
                    ticker=opt_str(info.get("stock_code")),
                    phone=opt_str(info.get("phn_no")),
                    ir_url=opt_str(info.get("ir_url")),
                    name_eng=opt_str(info.get("corp_name_eng")),
                    market=_MARKET_CLS.get(info.get("corp_cls", "")),
                )
            )
            if len(out) >= cap:
                break
        advance_cursor(self._cursor_store, self.name, segment, offset + scanned, len(listed))
        log.info("dart.live", segment=segment.label, n=len(out), offset=offset, scanned=scanned)
        return out


def _parse_corp_codes(zip_bytes: bytes) -> list[tuple[str, str]]:
    """corpCode.xml(ZIP) 에서 상장사(stock_code 보유) (corp_code, corp_name) 추출."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        xml_name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
        if xml_name is None:
            return []
        root = ElementTree.fromstring(zf.read(xml_name))
    out: list[tuple[str, str]] = []
    for node in root.iter("list"):
        stock = (node.findtext("stock_code") or "").strip()
        if not stock:  # 상장사만(비상장은 stock_code 공백).
            continue
        corp_code = (node.findtext("corp_code") or "").strip()
        corp_name = (node.findtext("corp_name") or "").strip()
        if corp_code:
            out.append((corp_code, corp_name))
    return out
