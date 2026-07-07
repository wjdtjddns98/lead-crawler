"""DART(금융감독원 전자공시) 발견 소스 — 한국 공시대상 법인(상장+비상장).

dry_run: 네트워크 없이 결정적 더미. 라이브: OpenDART 2-패스
1. ``corpCode.xml``(ZIP) 로 전체 등록법인 목록(상장+비상장, ~10만+),
2. 각 ``company.json`` 으로 홈페이지(hm_url)·업종(induty_code)·시장(corp_cls) 취득 후
   업종 접두 매칭(KSIC)으로 거른다.
호출량은 ``discovery_max_per_source``(런당)와 ``dart_daily_call_budget``(일일·키별, KST
리셋)로 상한한다 — OpenDART 일일 쿼터(2만/키)를 태우면 남은 응답이 전부 020 이라 커서만
헛돌므로, 키 최대 3개(``dart_api_key``/``_2``/``_3``)를 로테이션해 소진 키는 즉시 다음
키로 갈아끼우고(같은 항목부터 재시도) 전 키 소진 시에만 중단/스킵한다.
키가 하나도 없으면 비활성(no-op).
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
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
from .industry import industry_from_ksic, is_broad_industry, ksic_prefixes, matches_prefix

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
# 2000→5000 상향(물량): 스캔당 요청 = corpCode 목록 1 + 기업개황 1×후보. 5000 후보 = 5001 요청
# 으로 OpenDART 일일 쿼터 20,000 의 25%(하루 ~4회 풀스캔 여유). 소요 = 5000 × http_request_delay
# (기본 0.12s) ≈ 600s(10분)/풀스캔. scan_limit=min(cap*10, ABS) 이라 딥백필로 cap 을 올릴 때 상한.
# KR 12업종 세그먼트 × 5000 = 6만 > 일일 쿼터 2만이라 런당 상한만으론 부족 — 일일 예산
# (dart_daily_call_budget)이 라운드 전체를 묶는다(2026-07-07 쿼터 소진 실사고).
_SCAN_LIMIT_ABS = 5000

# 런 전체를 즉시 중단해야 하는 OpenDART status — 키 문제(010/011/012)·쿼터 소진(020)·
# 시스템 점검(800)·키 만료(901). 이후 응답도 전부 같은 코드라 계속 돌면 커서만 헛전진한다.
_FATAL_STATUS = frozenset({"010", "011", "012", "020", "800", "901"})

# 일일 호출 카운터의 커서 스토어 키 — (source='dart_quota', cursor_key=날짜:키지문, position=사용량).
# 전용 테이블 대신 discovery_cursor 재사용(마이그레이션 0). OpenDART 쿼터는 KST 자정·키별 리셋.
_QUOTA_SOURCE = "dart_quota"
_KST = timezone(timedelta(hours=9))


def _kst_today() -> str:
    """KST 기준 오늘 날짜 키(YYYY-MM-DD) — OpenDART 일일 쿼터 리셋 주기와 정렬."""
    return datetime.now(_KST).strftime("%Y-%m-%d")


def _quota_key(api_key: str) -> str:
    """일일 예산 카운터 키 = KST 날짜 + 키 지문(sha256 8자리) — 키별 분리 집계.

    OpenDART 쿼터는 키 단위라, 한도 소진 시 예비키 교체(2026-07-02 운영 관행)가 가능하다.
    날짜로만 세면 교체한 새 키의 남은 쿼터를 우리 카운터가 막아버린다. 지문만 저장하므로
    키 원문은 DB 에 남지 않는다."""
    digest = hashlib.sha256(api_key.encode()).hexdigest()[:8]
    return f"{_kst_today()}:{digest}"


class DartSource:
    """DART 기반 한국 공시대상 법인(상장+비상장) 발견 소스."""

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
        if not self._api_keys():
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

    def _api_keys(self) -> list[str]:
        """설정된 DART 키 목록(1~3번, 빈값 제외) — 소진 시 순서대로 로테이션."""
        s = self._settings
        keys = (s.dart_api_key, s.dart_api_key_2, s.dart_api_key_3)
        return [k.strip() for k in keys if k.strip()]

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """실 OpenDART 발견(2-패스 + 업종/캡 필터 + 키 로테이션).

        키가 쿼터/치명 status 로 죽으면 다음 키로 갈아끼워 **같은 항목부터** 이어 긁고,
        전 키가 소진되면 그 자리에서 중단한다(커서는 미처리 항목 앞에 정지)."""
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        prefixes = ksic_prefixes(segment.industry)
        # 미매핑 **구체** 업종은 수집하지 않는다(등록처 3소스 공통 가드): KSIC 필터 불가 →
        # 전량 통과 → 비-broad 세그먼트 라벨로 오라벨(전체크롤 GB 자동차 70% 과 동일 결함).
        if prefixes is None and not is_broad_industry(segment.industry):
            log.info("dart.skip.unmapped_industry", industry=segment.industry)
            return []

        # 키별 일일 예산 상태 — used0(런 시작 시점 DB값, 키당 1회 조회)+spent(런 내 사용).
        # 예산 소진/치명 status 키는 dead 로 빼고 다음 키로 로테이션(2026-07-07 실사고 +
        # 예비키 운영 관행). 전 키 소진 시에만 세그먼트를 접는다(커서 헛전진 방지).
        keys = self._api_keys()
        budget = self._settings.dart_daily_call_budget
        used0 = {
            k: (self._cursor_store.get(_QUOTA_SOURCE, _quota_key(k)) if self._cursor_store else 0)
            for k in keys
        }
        spent: dict[str, int] = {}
        dead: set[str] = set()

        def _pick_key() -> str | None:
            """살아있고 예산 남은 첫 키(설정 순서 = 1번 우선)."""
            for k in keys:
                if k not in dead and (not budget or used0[k] + spent.get(k, 0) < budget):
                    return k
            return None

        key = _pick_key()
        if key is None:
            log.info("dart.skip.daily_budget", budget=budget, keys=len(keys))
            return []

        corps: list[tuple[str, str]] | None = None
        while corps is None:
            raw = b""
            spent[key] = spent.get(key, 0) + 1
            try:
                raw = fetcher.get_bytes(_CORP_CODE_URL, params={"crtfc_key": key})
                corps = _parse_corp_codes(raw)
            except Exception as exc:  # 깨진 ZIP/응답이면 전체 크래시 대신 빈 결과.
                # 쿼터 고갈이면 ZIP 자리에 에러 XML 이 온다 — status 로 즉시 식별 + 키 로테이션.
                api_status = _sniff_status(raw)
                log.info("dart.corpcode.error", err=str(exc), api_status=api_status)
                if api_status in _FATAL_STATUS:
                    dead.add(key)
                    key = _pick_key()
                    if key is not None:
                        log.info("dart.rotate_key", stage="corpcode", api_status=api_status)
                        continue
                self._flush_quota(spent)
                return []
        # 업종 필터가 없으면 상한만큼만, 있으면 더 넓게 스캔(상한*10·절대상한 cap)하며 매칭 수집.
        scan_limit = cap if prefixes is None else min(cap * 10, _SCAN_LIMIT_ABS)
        if budget:  # 전 키 합산 잔여예산 안으로 — 라운드 후반 세그먼트가 빈 스캔으로 헛돌지 않게.
            remaining = sum(
                max(budget - used0[k] - spent.get(k, 0), 0) for k in keys if k not in dead
            )
            scan_limit = min(scan_limit, remaining)
        # 런 간 커서(딥백필): 지난 런이 멈춘 위치부터 이어 스캔한다(매 런 같은 머리 재방문 방지).
        # 상류 목록(corpCode.xml) 순서가 런 간 대체로 안정하다고 가정 — 흔들려도 오염은 없고
        # (제약① dedup) 일부 항목이 다음 소진 사이클(0 리셋)까지 늦어질 뿐(self-heal).
        offset = cursor_offset(self._cursor_store, self.name, segment, len(corps))
        entries = corps[offset : offset + scan_limit]
        scanned = 0  # 처리 완료(성공·개별실패 포함) 항목 수 — 커서 전진량.

        out: list[DiscoveredCompany] = []
        while scanned < len(entries):
            # 현재 키의 예산이 런 중 바닥나면 선제 로테이션(020 맞기 전에).
            if budget and used0[key] + spent.get(key, 0) >= budget:
                dead.add(key)
                key = _pick_key()  # type: ignore[assignment]
                if key is None:
                    break  # 전 키 소진 — 커서는 미처리 항목 앞에 정지.
            corp_code, corp_name = entries[scanned]
            spent[key] = spent.get(key, 0) + 1
            try:
                info = fetcher.get_json(
                    _COMPANY_URL, params={"crtfc_key": key, "corp_code": corp_code}
                )
            except Exception as exc:  # 개별 실패는 건너뛴다(배치 보호).
                log.info("dart.company.error", corp_code=corp_code, err=str(exc))
                scanned += 1
                continue
            status = info.get("status") if isinstance(info, dict) else None
            if status in _FATAL_STATUS:
                # 쿼터/키 치명 오류 — 이후 응답도 전부 같아 계속 돌면 커서만 헛전진한다.
                # 키를 죽이고 다음 키로 **같은 항목** 재시도, 남은 키 없으면 즉시 중단.
                dead.add(key)
                nxt = _pick_key()
                log.info(
                    "dart.fatal_status",
                    api_status=status, segment=segment.label, rotated=nxt is not None,
                )
                if nxt is None:
                    break  # scanned 미증가 → 다음 런이 이 항목부터 재시도.
                key = nxt
                continue
            scanned += 1
            if not isinstance(info, dict) or status != "000":
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
        advance_cursor(self._cursor_store, self.name, segment, offset + scanned, len(corps))
        self._flush_quota(spent)
        log.info("dart.live", segment=segment.label, n=len(out), offset=offset, scanned=scanned)
        return out

    def _flush_quota(self, spent: dict[str, int]) -> None:
        """이번 런의 키별 사용 호출량을 누적 기록한다(커서 스토어 재사용, best-effort).

        ponytail: read-modify-write 라 병렬 세그먼트에선 과소집계 가능 — 예산 기본값이
        실쿼터(2만) 대비 여유(1.5만)라 허용. 정확 집계가 필요해지면 SQL increment 로.
        """
        if self._cursor_store is None:
            return
        for key, calls in spent.items():
            if calls <= 0:
                continue
            used = self._cursor_store.get(_QUOTA_SOURCE, _quota_key(key))
            self._cursor_store.advance(_QUOTA_SOURCE, _quota_key(key), used + calls)


def _parse_corp_codes(zip_bytes: bytes) -> list[tuple[str, str]]:
    """corpCode.xml(ZIP) 에서 전체 등록법인 (corp_code, corp_name) 추출(상장+비상장).

    과거엔 stock_code 보유분(상장 ~2.7천)만 남겨 스펙("전 기업 상장+비상장")을 위반했다 —
    비상장 법인(~10만+)이 후보에조차 못 들던 천장의 근원. 상장여부는 다운스트림이
    company.json 의 corp_cls 로 세분화한다(_LISTED_CLS)."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        xml_name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
        if xml_name is None:
            return []
        root = ElementTree.fromstring(zf.read(xml_name))
    out: list[tuple[str, str]] = []
    for node in root.iter("list"):
        corp_code = (node.findtext("corp_code") or "").strip()
        corp_name = (node.findtext("corp_name") or "").strip()
        if corp_code:
            out.append((corp_code, corp_name))
    return out


def _sniff_status(raw: bytes) -> str | None:
    """비-ZIP 응답(에러 XML)에서 OpenDART status 코드를 뽑는다(진단용, 없으면 None).

    쿼터 고갈이면 corpCode.xml 자리에 ``<status>020</status>`` XML 이 와 BadZipFile 로만
    보였다(2026-07-07 실사고 — 원인 규명에 라이브 재현이 필요했음). 로그에 코드를 남긴다."""
    m = re.search(rb"<status>(\d+)</status>", raw[:500])
    return m.group(1).decode() if m else None
