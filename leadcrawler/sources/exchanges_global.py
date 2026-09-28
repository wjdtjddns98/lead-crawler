"""거래소 상장목록 발견 소스(Tier B) — 유럽 DE/Euronext(FR·IT·NL·BE·PT·IE·NO)/ES/CH(PR-b)
+ 신흥 BR(B3)/TR(KAP)/SA(Tadawul)/RU(MOEX)(PR-c).

``exchanges.py``(동남아 전용)와 분리한 신규 파일(2026-09-23, PR-b) — 국가별 상장기업
전수를 공개 정적 HTTP(+일부 curl_cffi)로 받는다. 베이스(:class:`ExchangeSource`,
``exchanges.py``)의 상장 세그먼트 게이트·``_seg``(업종 무도장)·``_memo``(국가별 1회)·
``cursor_store`` 재수집 주기를 그대로 물려받는다.

라이브 엔드포인트 상태(2026-09-23 이 PC 실측, 요청 거래소당 ≤5회):
- Xetra(독일): 랜딩 페이지에서 ``Listed-companies.xlsx`` blob URL 을 정규식으로 찾아
  GET(200, 71KB) → openpyxl 로 Prime/General/Scale 3시트 파싱(Basic Board 제외, PO 기본값).
  blob 해시는 재발행 시 바뀔 수 있어 랜딩 파싱이 정식, 실패 시 모듈 상수 URL 로 폴백.
- Euronext(FR/IT/NL/BE/PT/IE/NO): ``live.euronext.com/en/pd_es/data/stocks`` GET JSON
  (200, CloudFront, 챌린지 없음). 국가→MIC 매핑으로 1콜에 해당국 전 시장을 받는다.
  ``iDisplayLength`` 상한·IT ``EXGM`` 허용 여부는 미확인(스모크로 확인).
- BME(스페인): ``apiweb.bolsasymercados.es/Market/v1/EQ/ListedCompanies`` GET JSON(200).
  SIBE + BMEGrowth 두 패스. SICAV/HedgeFunds/ETF 세그먼트 행 제외.
  ``shareName`` 은 약칭이라 ticker 로 쓰지 않는다(ticker=None, Yahoo 는 companyKey 폴백
  이 무의미하면 미스 — 웹사이트 필드가 있는 행만 직접 도메인).
- SIX(스위스): ``six-group.com/sheldon/equity_issuers/v1/equity_issuers.json`` GET
  (200, content-type text/plain 이지만 본문은 JSON → get_text + json.loads).
  ``country``/``lastListingDate`` 값 형식은 미확인 — 관대한 필터로 시작(스모크로 조정).

- B3: 목록 API 는 ``dateListing`` 이 있는 행도 status 값이 전부 ``"A"`` 라 필터 실질 효력은
  ``dateListing != 31/12/9999`` 쪽 — 표본 4페이지(400행) 중 180행(45%)이 통과, 상세 콜로 확정.
  상세 ``hasQuotation`` 은 "S"(페트로브라스, 실거래)/"N"(등록만 된 SPE 법인)로 실측 분기 확인.
  pageSize 는 100 확인(1000·4000 은 빈 배열) — 스펙의 "120" 대신 100 채택.
- KAP: 렌더 HTML 테이블(``<tr class="border-b hover:bg-light-danger">`` 758행)을 직접 파싱한다
  (flight JSON 대신 — 그 안의 ``mkkMemberOid`` 는 상세 URL 의 짧은 숫자 id 와 무관해 별도 상관관계가
  필요한데, 렌더 테이블 앵커가 이미 그 숫자 id 를 href 에 담고 있어 더 짧고 견고함, 758/758 매치
  실측). 이 페이지 자체가 BIST 상장사 전용이라 ``kapMemberType`` 필터가 불필요(전부 IGS 동치).
- Tadawul: Akamai 가 TLS 지문을 본다 — impersonate=True(curl_cffi chrome) 로 272건 확인.
  ``marketType`` 파라미터를 빈 값으로 보내면 응답에 marketType 필드 자체가 없어 Nomu 별도
  여부 미확인(메인마켓으로 추정, PO §8 참조).
- MOEX: ISS 컬럼 실측 일치(``securities``/``emitters`` 엔드포인트 모두 스펙과 동일 스키마).

dry_run: 네트워크 없는 결정적 더미(베이스 ``_dry`` 상속).
"""

from __future__ import annotations

import base64
import html
import io
import json
import re
from datetime import datetime, timezone
from typing import Any

import openpyxl

from ..dedup import normalize_domain
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company, english_display, opt_str
from .countries import resolve_country, supported_countries
from .exchanges import ExchangeSource

log = get_logger("sources.exchanges_global")


# --- Deutsche Börse(Xetra) ------------------------------------------------

_DE_BASE = "https://www.cashmarket.deutsche-boerse.com"
_DE_LANDING_URL = f"{_DE_BASE}/cash-en/Data-Tech/statistics/listed-companies"
# 실측 blob(2026-09-23) — 랜딩 파싱 실패 시 폴백. 해시는 재발행 시 바뀔 수 있다.
_DE_FALLBACK_XLSX = (
    f"{_DE_BASE}/resource/blob/67858/3db127ac20c83f2955d06a43eddafede/"
    "data/Listed-companies.xlsx"
)
_DE_BLOB_RE = re.compile(r'href="([^"]*Listed-companies\.xlsx)"', re.I)
# Basic Board(비규제 시장)는 PO 기본값으로 제외.
_DE_SHEETS = ("Prime Standard", "General Standard", "Scale")


def _de_blob_url(landing_html: str) -> str | None:
    """랜딩 페이지 HTML 에서 xlsx blob 절대 URL 을 찾는다(못 찾으면 None → 폴백 URL)."""
    m = _DE_BLOB_RE.search(landing_html or "")
    if not m:
        return None
    href = html.unescape(m.group(1))
    return href if href.startswith("http") else _DE_BASE + href


def _parse_de_workbook(wb: Any) -> list[dict[str, Any]]:
    """DE 상장목록 워크북에서 (ISIN, 심볼, 회사명, 시트명) 행을 뽑는다(Country=Germany 만).

    헤더 행은 "ISIN" 셀 위치로 탐색한다(Scale 시트는 안내 행이 앞에 있어 행 번호 고정 불가).
    """
    out: list[dict[str, Any]] = []
    for sheet_name in _DE_SHEETS:
        if sheet_name not in wb.sheetnames:
            continue
        col_map: dict[str, int] | None = None
        for row in wb[sheet_name].iter_rows(values_only=True):
            if col_map is None:
                if any(isinstance(c, str) and c.strip() == "ISIN" for c in row):
                    col_map = {c.strip(): i for i, c in enumerate(row) if isinstance(c, str)}
                continue
            if not row or all(c is None for c in row):
                continue
            isin_i = col_map.get("ISIN")
            if isin_i is None or isin_i >= len(row) or not row[isin_i]:
                continue
            country_i = col_map.get("Country")
            country = row[country_i] if country_i is not None and country_i < len(row) else None
            if country != "Germany":
                continue

            def _cell(key: str) -> Any:
                i = col_map.get(key)  # noqa: B023 — col_map 은 시트 루프마다 재바인딩.
                return row[i] if i is not None and i < len(row) else None  # noqa: B023

            out.append({
                "isin": row[isin_i],
                "ticker": _cell("Trading Symbol"),
                "name": _cell("Company"),
                "sheet": sheet_name,
            })
    return out


class DeutscheBoerseSource(ExchangeSource):
    """독일 증권거래소(Xetra) 상장목록 — 랜딩 blob 파싱 + openpyxl(모듈 docstring 참조)."""

    name = "xetra"
    registry = "xetra"
    countries = frozenset({
        "de", "deu", "germany", "deutschland", "federal republic of germany", "독일",
    })

    def _live(self, segment: Segment) -> list[Any]:
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        listed_seg = self._seg(segment)
        try:
            landing = fetcher.get_text(_DE_LANDING_URL)
        except Exception as exc:  # noqa: BLE001 — 랜딩 실패해도 폴백 URL 로 계속.
            log.info("xetra.landing.error", err_type=type(exc).__name__, err=str(exc))
            landing = ""
        blob_url = _de_blob_url(landing)
        if blob_url is None:
            log.info("xetra.blob.fallback", url=_DE_FALLBACK_XLSX)
            blob_url = _DE_FALLBACK_XLSX
        try:
            data = fetcher.get_bytes(blob_url)
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 — 네트워크/형식 이상 → graceful 빈 결과.
            log.info("xetra.error", url=blob_url, err_type=type(exc).__name__, err=str(exc))
            return []

        out = []
        seen: set[str] = set()
        for row in _parse_de_workbook(wb):
            isin = str(row["isin"]).strip()
            name = str(row["name"] or "").strip()
            if not isin or not name or isin in seen:
                continue
            seen.add(isin)
            ticker = str(row["ticker"]).strip() if row["ticker"] else None
            out.append(build_company(
                source=self.name, segment=listed_seg, name=name, domain=None,
                registry=self.registry, registry_id=isin, ticker=ticker,
                market=row["sheet"], listed_verified=True,
            ))
            if len(out) >= cap:
                break
        log.info("xetra.live", segment=segment.label, n=len(out))
        return out


# --- Euronext(FR/IT/NL/BE/PT/IE/NO) ---------------------------------------

# 국가→MIC(Market Identifier Code) 매핑(2026-09-23 설계). IT EXGM 허용 여부·MIC 목록의
# 실제 유효성은 라이브 스모크로 확인(0행이면 로그만 남기고 계속 — graceful).
_EURONEXT_MIC_BY_ISO2: dict[str, tuple[str, ...]] = {
    "FR": ("XPAR", "ALXP"),
    "IT": ("MTAA",),
    "NL": ("XAMS",),
    "BE": ("XBRU",),
    "PT": ("XLIS",),
    "IE": ("XMSM", "XESM"),
    "NO": ("XOSL", "MERK"),
}
_EURONEXT_MAX_PAGES = 30
_EURONEXT_PAGE_SIZE = 100
_EURONEXT_NAME_RE = re.compile(r">([^<]+)</a>")
_EURONEXT_TAG_RE = re.compile(r"<[^>]+>")
_EURONEXT_TITLE_RE = re.compile(r'title=[\'"]([^\'"]+)[\'"]')


def _euronext_countries() -> frozenset[str]:
    """``countries.py`` 에 이미 등록된 별칭을 Euronext 7개국 ISO2 로 필터해 재사용한다."""
    aliases: set[str] = set()
    for c in supported_countries():
        if c.iso2 in _EURONEXT_MIC_BY_ISO2:
            aliases.update(c.aliases)
    return frozenset(aliases)


def _parse_euronext_row(row: Any) -> dict[str, Any] | None:
    """``aaData`` 한 행(list, 셀 일부는 HTML 문자열)에서 회사 식별 정보를 뽑는다."""
    if not isinstance(row, list) or len(row) < 4:
        return None
    name_cell = str(row[1] or "")
    isin = str(row[2] or "").strip()
    symbol = str(row[3] or "").strip()
    if not isin:
        return None
    m = _EURONEXT_NAME_RE.search(name_cell)
    raw_name = m.group(1) if m else _EURONEXT_TAG_RE.sub("", name_cell)
    name = html.unescape(raw_name).strip()
    if not name:
        return None
    market = None
    if len(row) > 4:
        mt = _EURONEXT_TITLE_RE.search(str(row[4] or ""))
        market = html.unescape(mt.group(1)).strip() if mt else None
    return {"isin": isin, "name": name, "symbol": symbol or None, "market": market}


class EuronextSource(ExchangeSource):
    """Euronext(파리·밀라노·암스테르담·브뤼셀·리스본·더블린·오슬로) 상장목록.

    registry 는 단일 ``euronext``(ISIN 이 전역 유일이라 시장별 registry 분리 불필요,
    시장 구분은 ``ticker``/``market`` 필드로). 국가별 MIC 매핑 1콜로 해당국 전 시장 수집.
    """

    name = "euronext"
    registry = "euronext"
    countries = _euronext_countries()
    list_url = "https://live.euronext.com/en/pd_es/data/stocks"

    def _live(self, segment: Segment) -> list[Any]:
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        listed_seg = self._seg(segment)
        country = resolve_country(segment.country)
        mics = _EURONEXT_MIC_BY_ISO2.get(country.iso2) if country else None
        if not mics:
            return []
        mics_param = ",".join(mics)

        out = []
        seen: set[str] = set()
        start = 0
        page = 0
        total: int | None = None
        while len(out) < cap and page < _EURONEXT_MAX_PAGES and (
            total is None or start < total
        ):
            try:
                payload = fetcher.get_json(self.list_url, params={
                    "mics": mics_param,
                    "display_datapoints": "dp_stocks",
                    "display_filters": "df_stocks2",
                    "iDisplayStart": start,
                    "iDisplayLength": _EURONEXT_PAGE_SIZE,
                })
            except Exception as exc:  # noqa: BLE001 — 네트워크/형식 이상 → 부분 결과 보존.
                log.info(
                    "euronext.error", mics=mics_param,
                    err_type=type(exc).__name__, err=str(exc),
                )
                break
            if not isinstance(payload, dict):
                break
            total = payload.get("iTotalRecords") or 0
            rows = payload.get("aaData")
            if not isinstance(rows, list) or not rows:
                break
            before = len(seen)
            for raw in rows:
                parsed = _parse_euronext_row(raw)
                if not parsed or parsed["isin"] in seen:
                    continue
                seen.add(parsed["isin"])
                out.append(build_company(
                    source=self.name, segment=listed_seg, name=parsed["name"], domain=None,
                    registry=self.registry, registry_id=parsed["isin"],
                    ticker=parsed["symbol"], market=parsed["market"] or mics_param,
                    listed_verified=True,
                ))
                if len(out) >= cap:
                    break
            if len(seen) == before:  # 이번 페이지에서 신규 ISIN 0건 → 동일 페이지 반복(무한루프 방지).
                break
            start += len(rows)
            page += 1
        log.info("euronext.live", segment=segment.label, n=len(out))
        return out


# --- BME(스페인) -----------------------------------------------------------

_BME_MAX_PAGES = 50
_BME_PAGE_SIZE = 100
# 비영업 상품 세그먼트/시스템 — 실존 기업이 아닌 투자기구·ETF 배제(제약②).
_BME_EXCLUDE_SEGMENTS = frozenset({"SICAV", "HedgeFunds"})


class BmeSource(ExchangeSource):
    """스페인 증권거래소(BME) 상장목록 — SIBE + BME Growth 두 패스(PO 기본값=중소 병행시장 포함)."""

    name = "bme"
    registry = "bme"
    countries = frozenset({"es", "esp", "spain", "españa", "스페인"})
    list_url = "https://apiweb.bolsasymercados.es/Market/v1/EQ/ListedCompanies"

    def _fetch_pass(
        self, fetcher: Any, extra_params: dict[str, str], market_label: str,
        cap: int, listed_seg: Segment, out: list[Any], seen: set[str],
    ) -> None:
        page = 0
        while len(out) < cap and page < _BME_MAX_PAGES:
            params: dict[str, Any] = {"page": page, "pageSize": _BME_PAGE_SIZE, **extra_params}
            try:
                payload = fetcher.get_json(self.list_url, params=params)
            except Exception as exc:  # noqa: BLE001 — 네트워크/형식 이상 → 부분 결과 보존.
                log.info(
                    "bme.error", pass_=market_label,
                    err_type=type(exc).__name__, err=str(exc),
                )
                return
            if not isinstance(payload, dict):
                return
            rows = payload.get("data")
            if not isinstance(rows, list) or not rows:
                return
            before = len(seen)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if (
                    row.get("mtfSegment") in _BME_EXCLUDE_SEGMENTS
                    or row.get("tradingSystem") == "ETF"
                ):
                    continue  # SICAV/HedgeFunds/ETF — 실존 영업기업 아님(제약②).
                key = str(row.get("companyKey") or "").strip()
                name = str(row.get("name") or "").strip()
                if not key or not name or key in seen:
                    continue
                seen.add(key)
                out.append(build_company(
                    source=self.name, segment=listed_seg, name=name,
                    domain=normalize_domain(row.get("website")),
                    registry=self.registry, registry_id=key, ticker=None,
                    market=market_label, listed_verified=True,
                ))
                if len(out) >= cap:
                    return
            if len(seen) == before:  # 이번 페이지에서 신규 companyKey 0건 → 동일 페이지 반복.
                return
            if not payload.get("hasMoreResults"):
                return
            page += 1

    def _live(self, segment: Segment) -> list[Any]:
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        listed_seg = self._seg(segment)
        out: list[Any] = []
        seen: set[str] = set()
        self._fetch_pass(fetcher, {"tradingSystem": "SIBE"}, "SIBE", cap, listed_seg, out, seen)
        self._fetch_pass(
            fetcher, {"mtfSegment": "BMEGrowth"}, "BME Growth", cap, listed_seg, out, seen,
        )
        log.info("bme.live", segment=segment.label, n=len(out))
        return out


# --- SIX(스위스) -----------------------------------------------------------

_SIX_LIST_URL = "https://www.six-group.com/sheldon/equity_issuers/v1/equity_issuers.json"
# 값 형식(country="CH" 대 "Switzerland")이 미확인이라 둘 다 관대히 인정(스모크로 확정).
_SIX_COUNTRY_OK = frozenset({"CH", "SWITZERLAND"})


def _six_today() -> int:
    return int(datetime.now(timezone.utc).strftime("%Y%m%d"))


def _six_row_active(row: Any, today: int) -> bool:
    """CH 1차 상장이고 상장폐지일이 아직(또는 영구, 99991231) 도래하지 않은 행인지."""
    if not isinstance(row, dict):
        return False
    if str(row.get("country") or "").strip().upper() not in _SIX_COUNTRY_OK:
        return False
    if row.get("primaryListing") is not True:
        return False
    try:
        last = int(row.get("lastListingDate"))
    except (TypeError, ValueError):
        return False
    return last >= today


class SixSource(ExchangeSource):
    """스위스 증권거래소(SIX) 상장목록 — JSON(content-type text/plain, get_text+json.loads)."""

    name = "six"
    registry = "six"
    countries = frozenset({"ch", "che", "switzerland", "schweiz", "suisse", "스위스"})
    list_url = _SIX_LIST_URL

    def _live(self, segment: Segment) -> list[Any]:
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        listed_seg = self._seg(segment)
        try:
            payload = json.loads(fetcher.get_text(self.list_url))
        except Exception as exc:  # noqa: BLE001 — 네트워크/형식 이상 → graceful 빈 결과.
            log.info("six.error", err_type=type(exc).__name__, err=str(exc))
            return []
        items = payload.get("itemList") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []

        out = []
        seen: set[str] = set()
        today = _six_today()
        for row in items:
            if not _six_row_active(row, today):
                continue
            valor = str(row.get("valorNumber") or "").strip()
            name = str(row.get("company") or "").strip()
            if not valor or not name or valor in seen:
                continue
            seen.add(valor)
            symbol = str(row.get("valorSymbol") or "").strip() or None
            out.append(build_company(
                source=self.name, segment=listed_seg, name=name, domain=None,
                registry=self.registry, registry_id=valor, ticker=symbol,
                market="SIX Swiss Exchange", listed_verified=True,
            ))
            if len(out) >= cap:
                break
        log.info("six.live", segment=segment.label, n=len(out))
        return out


# --- B3(브라질) -----------------------------------------------------------

_B3_LIST_URL = (
    "https://sistemaswebb3-listados.b3.com.br/listedCompaniesProxy/CompanyCall/"
    "GetInitialCompanies/{b64}"
)
_B3_DETAIL_URL = (
    "https://sistemaswebb3-listados.b3.com.br/listedCompaniesProxy/CompanyCall/"
    "GetDetail/{b64}"
)
_B3_PAGE_SIZE = 100  # 1000/4000 은 빈 배열(2026-09-23 실측) — 100 이 안전 상한.
_B3_MAX_PAGES = 40  # 실측 totalPages=36(전체 3,533건, pageSize=100).
_B3_NOT_LISTED_DATE = "31/12/9999"  # 미상장 성격(등록만·공모 대기 등) — 1차 필터로 상세콜 절감.


def _b64_json(payload: dict[str, Any]) -> str:
    """B3 API 의 base64(JSON) 경로 파라미터 인코딩."""
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _b3_parse_list_page(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """B3 GetInitialCompanies 응답에서 (행 목록, totalPages) 를 뽑는다(형식 불일치는 빈 값)."""
    if not isinstance(payload, dict):
        return [], 0
    results = payload.get("results")
    total_pages = (payload.get("page") or {}).get("totalPages") or 0
    return (results if isinstance(results, list) else []), int(total_pages)


class B3Source(ExchangeSource):
    """브라질 B3 거래소 상장목록 소스(목록 페이지네이션 + 상세 N+1, hasQuotation 확정)."""

    name = "b3"
    registry = "b3"
    countries = frozenset({"br", "bra", "brazil", "brasil", "브라질"})

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        listed_seg = self._seg(segment)

        # 1차: 전 목록 페이지네이션(미상장 성격만 걸러 상세콜 후보를 줄인다).
        candidates: list[dict[str, Any]] = []
        page = 1
        total_pages = _B3_MAX_PAGES
        while page <= min(total_pages, _B3_MAX_PAGES):
            b64 = _b64_json({"language": "pt-br", "pageNumber": page, "pageSize": _B3_PAGE_SIZE})
            try:
                payload = fetcher.get_json(_B3_LIST_URL.format(b64=b64))
            except Exception as exc:  # 네트워크/형식 → 부분 결과로 다음 단계 진행.
                log.info("b3.list.error", page=page, err_type=type(exc).__name__, err=str(exc))
                break
            rows, total_pages_resp = _b3_parse_list_page(payload)
            if not rows:
                break
            if total_pages_resp:
                total_pages = total_pages_resp
            for row in rows:
                if (
                    row.get("dateListing") != _B3_NOT_LISTED_DATE
                    and row.get("status") == "A"
                    and row.get("codeCVM")
                ):
                    candidates.append(row)
            page += 1

        # 2차: 후보만 상세 조회 — hasQuotation=="S" 인 행만 상장 확정 채택(cap 안에서).
        out: list[DiscoveredCompany] = []
        seen: set[str] = set()
        for row in candidates:
            if len(out) >= cap:
                break
            code_cvm = str(row["codeCVM"])
            if code_cvm in seen:
                continue
            seen.add(code_cvm)
            try:
                info = json.loads(fetcher.get_text(_B3_DETAIL_URL.format(
                    b64=_b64_json({"codeCVM": code_cvm, "language": "pt-br"})
                )))
            except Exception as exc:  # 상세 실패 → 상장 미확정, 행 스킵(저장 안 함).
                log.info(
                    "b3.detail.error", codeCVM=code_cvm,
                    err_type=type(exc).__name__, err=str(exc),
                )
                continue
            if not isinstance(info, dict) or info.get("hasQuotation") != "S":
                continue
            out.append(
                build_company(
                    source=self.name, segment=listed_seg,
                    name=opt_str(info.get("companyName")) or opt_str(row.get("companyName"))
                    or code_cvm,
                    domain=normalize_domain(opt_str(info.get("website"))),
                    registry=self.registry, registry_id=code_cvm,
                    ticker=opt_str(info.get("code")), market=opt_str(info.get("market")),
                    listed_verified=True,  # 상세 hasQuotation 실측 확정.
                )
            )
        log.info("b3.live", segment=segment.label, n=len(out), candidates=len(candidates))
        return out


# --- KAP(튀르키예) ---------------------------------------------------------

_KAP_LIST_URL = "https://www.kap.org.tr/tr/bist-sirketler"
_KAP_DETAIL_URL = "https://www.kap.org.tr/tr/sirket-bilgileri/ozet/{slug}"
_KAP_ROW_SEP = '<tr class="border-b hover:bg-light-danger">'
# 렌더 테이블 행: <a href="/tr/sirket-bilgileri/ozet/<id>-<slug>"><div>코드</div></a></td>
# <td..><a..>회사명</a></td><td..>도시</td> — 실측 758/758 매치(2026-09-23).
_KAP_ID_RE = re.compile(r'/tr/sirket-bilgileri/ozet/([^"]+?)"><div>([^<]*)</div>')
_KAP_TITLE_RE = re.compile(r'<div>[^<]*</div></a></td>\s*<td[^>]*><a[^>]*>([^<]*)</a>')
# 상세 페이지의 Next.js flight 페이로드는 라벨과 값이 연속된 "children":"..." 쌍으로 실린다
# (예: ..."children":"İnternet Adresi"}],[...,"children":"www.x.com.tr"}]...).
_KAP_CHILDREN_RE = re.compile(r'\\"children\\":\\"([^\\]{0,300})\\"')
_KAP_WEBSITE_LABEL = "İnternet Adresi"
_KAP_MARKET_LABEL = "Sermaye Piyasası Aracının İşlem Gördüğü Pazar"


def _kap_parse_list(page_html: str) -> list[dict[str, str]]:
    """KAP bist-sirketler 렌더 HTML 테이블에서 상장사 행을 뽑는다(<tr> 블록 단위).

    Next.js RSC flight JSON 대신 서버렌더 테이블을 직접 읽는다 — flight 페이로드의
    ``mkkMemberOid`` 는 상세 URL 의 slug(짧은 숫자 id)와 무관해 별도 상관관계가 필요했는데,
    렌더 테이블 앵커가 이미 그 숫자 id 를 href 에 담고 있어 더 짧고 견고하다(설계와 다른 결정 —
    모듈 docstring 참조).
    """
    out: list[dict[str, str]] = []
    for block in (page_html or "").split(_KAP_ROW_SEP)[1:]:
        m_id = _KAP_ID_RE.search(block)
        if not m_id:
            continue
        slug, code = m_id.group(1).strip(), m_id.group(2).strip()
        num_id = slug.split("-", 1)[0]
        if not num_id.isdigit() or not code:
            continue
        m_title = _KAP_TITLE_RE.search(block)
        out.append({
            "id": num_id,
            "slug": slug,
            "code": code,
            "title": (m_title.group(1).strip() if m_title else "") or code,
        })
    return out


def _kap_label_value(detail_html: str, label: str) -> str | None:
    """flight 페이로드의 순차 ``children`` 값 쌍(라벨 다음 값)에서 라벨의 값을 찾는다."""
    values = _KAP_CHILDREN_RE.findall(detail_html or "")
    for i, v in enumerate(values):
        if v == label and i + 1 < len(values):
            return v and values[i + 1] or None
    return None


class KapSource(ExchangeSource):
    """튀르키예 KAP(공시플랫폼) BIST 상장사 목록 소스(목록+상세 N+1)."""

    name = "kap"
    registry = "kap"
    countries = frozenset({
        "tr", "tur", "turkey", "türkiye", "turkiye", "튀르키예", "터키",
    })

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        listed_seg = self._seg(segment)

        try:
            page_html = fetcher.get_text(_KAP_LIST_URL)
        except Exception as exc:  # 네트워크/형식 → graceful 빈 결과.
            log.info("kap.list.error", err_type=type(exc).__name__, err=str(exc))
            return []
        rows = _kap_parse_list(page_html)

        out: list[DiscoveredCompany] = []
        seen: set[str] = set()
        for row in rows:
            if len(out) >= cap:
                break
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            domain: str | None = None
            market: str | None = None
            try:
                detail_html = fetcher.get_text(_KAP_DETAIL_URL.format(slug=row["slug"]))
                domain = normalize_domain(_kap_label_value(detail_html, _KAP_WEBSITE_LABEL))
                market = _kap_label_value(detail_html, _KAP_MARKET_LABEL)
            except Exception as exc:  # 상세 실패 → 웹사이트 None 으로 목록 행 유지.
                log.info(
                    "kap.detail.error", id=row["id"],
                    err_type=type(exc).__name__, err=str(exc),
                )
            out.append(
                build_company(
                    source=self.name, segment=listed_seg, name=row["title"],
                    domain=domain, registry=self.registry, registry_id=row["id"],
                    ticker=row["code"].split(",")[0].strip() or None,
                    market=market or "BIST", listed_verified=True,
                )
            )
        log.info("kap.live", segment=segment.label, n=len(out), total=len(rows))
        return out


# --- Tadawul(사우디아라비아) -----------------------------------------------

_TADAWUL_LANDING_URL = (
    "https://www.saudiexchange.sa/wps/portal/saudiexchange/trading/"
    "participants-directory/issuer-directory"
)
# WebSphere 내비 토큰 포함 액션 경로 — base(<base href>, 매 요청 새로 파싱) 뒤에 그대로 붙는다.
_TADAWUL_ACTION_PATH = (
    "p0/IZ7_5A602H80OOMQC0604RU6VD10F2=CZ6_5A602H80OGSTA0QFSTBN9F10I5="
    "NJgetCompanyListByMarknetAndSectors=/"
)
_TADAWUL_BASE_RE = re.compile(r'<base href="([^"]+)"')


class TadawulSource(ExchangeSource):
    """사우디 타다울(Tadawul) 상장목록 소스 — Akamai TLS 지문 차단(impersonate 필수)."""

    name = "tadawul"
    registry = "tadawul"
    countries = frozenset({
        "sa", "sau", "saudi arabia", "ksa", "사우디아라비아", "사우디",
    })
    impersonate = True

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        listed_seg = self._seg(segment)
        try:
            landing = fetcher.get_text(_TADAWUL_LANDING_URL)
            m = _TADAWUL_BASE_RE.search(landing)
            if not m:
                log.info("tadawul.base.missing")
                return []
            body = fetcher.post_text(
                m.group(1) + _TADAWUL_ACTION_PATH,
                data={"marketType": "", "sector": "", "symbol": "", "letter": ""},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            payload = json.loads(body)
        except Exception as exc:  # 네트워크/파싱/impersonate 미설치 → graceful 빈 결과.
            log.info("tadawul.error", err_type=type(exc).__name__, err=str(exc))
            return []
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []

        out: list[DiscoveredCompany] = []
        seen: set[str] = set()
        for row in rows:
            if len(out) >= cap:
                break
            if not isinstance(row, dict):
                continue
            symbol = opt_str(row.get("symbol"))
            name = opt_str(row.get("lonaName")) or opt_str(row.get("shortName"))
            if not symbol or not name or symbol in seen:
                continue
            seen.add(symbol)
            out.append(
                build_company(
                    source=self.name, segment=listed_seg, name=name,
                    domain=None,  # 목록에 웹사이트 없음 → Yahoo `.SR` 해석에 위임.
                    registry=self.registry, registry_id=symbol, ticker=symbol,
                    market="Tadawul Main Market", listed_verified=True,
                )
            )
        log.info("tadawul.live", segment=segment.label, n=len(out))
        return out


# --- MOEX(러시아) -----------------------------------------------------------

_MOEX_LIST_URL = "https://iss.moex.com/iss/securities.json"
_MOEX_EMITTER_URL = "https://iss.moex.com/iss/emitters/{emitent_id}.json"
_MOEX_PAGE_SIZE = 100
_MOEX_MAX_PAGES = 50
_MOEX_WANTED_TYPES = frozenset({"common_share", "preferred_share"})


def _moex_section(payload: Any, block: str) -> tuple[list[str], list[list[Any]]]:
    """MOEX ISS 응답에서 (columns, data) 를 뽑는다(``block`` 예: 'securities'/'emitter')."""
    section = payload.get(block) if isinstance(payload, dict) else None
    if not isinstance(section, dict):
        return [], []
    cols = section.get("columns")
    data = section.get("data")
    return (cols if isinstance(cols, list) else []), (data if isinstance(data, list) else [])


def _moex_cell(value: Any) -> str | None:
    """MOEX ISS 셀 값을 문자열로(``emitent_id`` 등 정수 컬럼도 있다) — 빈값은 None."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


class MoexSource(ExchangeSource):
    """러시아 MOEX(모스크바거래소) 상장목록 소스 — 발행자(emitent_id) 기준 dedup."""

    name = "moex"
    registry = "moex"
    countries = frozenset({
        "ru", "rus", "russia", "russian federation", "россия", "러시아",
    })

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source
        listed_seg = self._seg(segment)

        # 1차: 종목 목록 페이지네이션 → 발행자 기준 대표 행 dedup(보통주 ticker 우선).
        issuers: dict[str, dict[str, Any]] = {}
        start = 0
        page = 0
        prev: list[Any] | None = None
        while page < _MOEX_MAX_PAGES and len(issuers) < cap:
            try:
                payload = fetcher.get_json(
                    _MOEX_LIST_URL,
                    params={
                        "engine": "stock", "market": "shares", "is_trading": 1,
                        "limit": _MOEX_PAGE_SIZE, "start": start, "iss.meta": "off",
                    },
                )
            except Exception as exc:  # 네트워크/형식 → 부분 결과로 다음 단계 진행.
                log.info("moex.list.error", start=start, err_type=type(exc).__name__, err=str(exc))
                break
            cols, data = _moex_section(payload, "securities")
            if not cols or not data or data == prev:  # start 무시로 같은 페이지 반복 → 중단.
                break
            prev = data
            for row in data:
                # zip: 컬럼보다 짧은/깨진 행도 IndexError 없이 결측으로(세그먼트 크래시 방지).
                rec = dict(zip(cols, row)) if isinstance(row, list) else {}
                sec_type = rec.get("type")
                if sec_type not in _MOEX_WANTED_TYPES:
                    continue
                emitent_id = _moex_cell(rec.get("emitent_id"))
                if not emitent_id:
                    continue
                secid = _moex_cell(rec.get("secid"))
                title = _moex_cell(rec.get("emitent_title"))
                existing = issuers.get(emitent_id)
                if existing is None:
                    issuers[emitent_id] = {"secid": secid, "title": title, "type": sec_type}
                elif sec_type == "common_share" and existing["type"] != "common_share":
                    issuers[emitent_id]["secid"] = secid  # 보통주가 대표 ticker.
            start += len(data)
            page += 1

        # 2차: 발행자당 1회 조회(웹사이트 URL·영문 음역).
        out: list[DiscoveredCompany] = []
        for emitent_id, info in issuers.items():
            if len(out) >= cap:
                break
            local_name = info.get("title") or emitent_id
            domain: str | None = None
            eng: str | None = None
            try:
                epayload = fetcher.get_json(
                    _MOEX_EMITTER_URL.format(emitent_id=emitent_id),
                    params={"iss.meta": "off"},
                )
                ecols, edata = _moex_section(epayload, "emitter")
                if ecols and edata:
                    eidx = {c: i for i, c in enumerate(ecols)}
                    erow = edata[0]
                    if "URL" in eidx:
                        domain = normalize_domain(_moex_cell(erow[eidx["URL"]]))
                    if "TRANSLITERATION_TITLE" in eidx:
                        eng = _moex_cell(erow[eidx["TRANSLITERATION_TITLE"]])
            except Exception as exc:  # 발행자 조회 실패 → 도메인 없이 목록 행 유지.
                log.info(
                    "moex.emitter.error", emitent_id=emitent_id,
                    err_type=type(exc).__name__, err=str(exc),
                )
            name, name_eng = english_display(local_name, eng, segment.country)
            out.append(
                build_company(
                    source=self.name, segment=listed_seg, name=name, name_eng=name_eng,
                    domain=domain, registry=self.registry, registry_id=emitent_id,
                    ticker=info.get("secid"), market="MOEX", listed_verified=True,
                )
            )
        log.info("moex.live", segment=segment.label, n=len(out), issuers=len(issuers))
        return out
