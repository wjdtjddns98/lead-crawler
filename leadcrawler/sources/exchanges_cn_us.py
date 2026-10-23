"""거래소 상장목록 발견 소스(Tier B) — CN(상하이 SSE·선전 SZSE) / US(Nasdaq 스크리너: NASDAQ·NYSE·AMEX).

2026-10-23 신설(exchanges_major 후속). 원장 실측 CN 상장 1,165(전수 ≈5,200)·US 2,727(EDGAR,
전수 ≈5,300) 로 큰 사각이 남아 있었다. 전부 공개 정적 HTTP.

라이브 엔드포인트(2026-10-23 이 PC 실측, 전부 200):
- SSE: ``query.sse.com.cn/sseQuery/commonQuery.do?sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L
  &STOCK_TYPE=…`` (Referer 필수) — STOCK_TYPE 1(메인보드 1,700)·8(커촹반 618).
  ``pageHelp.pageSize=5000`` 1페이지(total 이 더 크면 부분 결과로 표시). B주 단독(STOCK_TYPE 2, 41건)은
  제외 — 외국인 전용 B주만 상장한 법인은 A주 목록과 무관한 소수라 1차 범위 밖(ponytail).
  필드: A_STOCK_CODE·FULL_NAME·FULL_NAME_IN_ENGLISH·DELIST_DATE('-'=상장중, 실측 1,700/1,700).
  웹사이트 없음 → Yahoo ``.SS``(yahoo_symbol 이 6/9 시작 코드로 판정).
- SZSE: ``szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1110&TABKEY=tab1`` xlsx(Referer 필수)
  2,906행 — 公司全称·英文名称·A股代码·B股代码·所属行业·**公司网址 동봉**(도메인 해석 불필요).
- Nasdaq 스크리너: ``api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true
  &exchange={nasdaq|nyse|amex}`` (브라우저 UA 필수) — 전 미국 상장 6,999행(country 포함). ``country ==
  "United States"`` 5,271 만(빈 country 281행은 ADR·외국 법인 혼재라 제외, 로그로 건수만 남긴다).
  종목명은 **보통주 꼬리 우선** 판정: "Common Stock"·"Class A Ordinary Shares"·"Common Units"(MLP) 등
  꼬리가 있으면 보통주로 보고 꼬리를 떼어 법인명을 만든다(꼬리 뒤 "(DE)"·"New" 허용). 꼬리가 없는
  이름만 증권 문맥(워런트·유닛·우선주·권리·채권·예탁증서·%·due YYYY·ETF/펀드/신탁상품)으로 제외한다 —
  이름 어디든 "Senior"·"Unit"·"Preferred" 가 들어가면 떨어내던 구조는 Brookdale Senior Living·Unit Corp·
  Preferred Bank·MLP 전량을 오제외했다(2026-10-23 리뷰 HIGH). SPAC("… Acquisition Corp")은 제외.
  같은 법인의 복수 클래스(GOOG/GOOGL·BRK/A·BRK/B)는 법인명으로 묶고 심볼은 **결정적 규칙**(구분자 없는
  심볼 우선 → 짧은 것 → 사전순)으로 하나만 — 행 순서에 키가 흔들리지 않게(제약①). 스크리너 심볼의
  클래스 구분자 ``/``(BRK/B)는 ``.`` 로 정규화한다(yahoo_symbol 이 US 는 ``.``→``-``).
  ponytail: 티커 변경(FB→META)·SPAC 심볼 재사용 때는 키가 바뀐다 — 스크리너에 CIK 가 없어 근본 해결
  불가, EDGAR(reg:sec) 행과의 이중은 Yahoo 해석 후 도메인 동치 dedup(``dedup.skip.resolved``)이 흡수.

dry_run: 네트워크 없는 결정적 더미(베이스 ``_dry`` 상속).
"""

from __future__ import annotations

import io
import re
from typing import Any

import openpyxl

from ..dedup import normalize_domain
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, build_company, english_display, opt_str
from .exchanges import ExchangeSource
from .exchanges_major import _aliases

log = get_logger("sources.exchanges_cn_us")

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)


# --- SSE(상하이) --------------------------------------------------------------

_SSE_URL = (
    "http://query.sse.com.cn/sseQuery/commonQuery.do?sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L"
    "&STOCK_TYPE={stock_type}&REG_PROVINCE=&CSRC_CODE=&STOCK_CODE=&COMPANY_STATUS=2,4,5,7,8"
    "&type=inParams&isPagination=true&pageHelp.cacheSize=1&pageHelp.beginPage=1"
    "&pageHelp.pageSize=5000&pageHelp.pageNo=1"
)
_SSE_HEADERS = {"User-Agent": _BROWSER_UA, "Referer": "http://www.sse.com.cn/", "Accept": "*/*"}
_SSE_BOARDS = (("1", "SSE Main Board"), ("8", "SSE STAR Market"))


def _sse_rows(payload: Any) -> tuple[list[dict[str, Any]], bool]:
    """SSE 응답 ``pageHelp.data`` → (상장중 행 목록, 완전 수신 여부).

    ``total`` 이 받은 행 수보다 크면(페이지 상한 초과) 완전 수신 False — 호출부가 부분 결과로 표시.
    """
    page = payload.get("pageHelp") if isinstance(payload, dict) else None
    data = page.get("data") if isinstance(page, dict) else None
    if not isinstance(data, list):
        return [], False
    total = page.get("total")
    complete = not (isinstance(total, int) and total > len(data))
    out = []
    for row in data:
        if not isinstance(row, dict):
            continue
        code, name = opt_str(row.get("A_STOCK_CODE")), opt_str(row.get("FULL_NAME"))
        delist = opt_str(row.get("DELIST_DATE"))
        if code and name and delist in (None, "-"):
            out.append({
                "code": code, "name": name, "eng": opt_str(row.get("FULL_NAME_IN_ENGLISH")),
            })
    return out, complete


class SseSource(ExchangeSource):
    """상하이 증권거래소(SSE) 상장목록 JSON — 메인보드 + 커촹반(STAR)."""

    name = "sse"
    registry = "sse"
    countries = _aliases("CN")

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        fetcher = self._client()
        cap = self._settings.exchange_max_per_source
        listed_seg = self._seg(segment)
        out: list[DiscoveredCompany] = []
        seen: set[str] = set()
        for stock_type, market in _SSE_BOARDS:
            try:
                rows, complete = _sse_rows(fetcher.get_json(
                    _SSE_URL.format(stock_type=stock_type), headers=_SSE_HEADERS
                ))
            except Exception as exc:  # noqa: BLE001 — 한 보드 실패는 부분 결과(재수집 기록 생략).
                log.info("sse.error", board=market, err_type=type(exc).__name__, err=str(exc))
                self._partial = True
                continue
            if not rows or not complete:
                # 200 이지만 스키마 이상/0행/페이지 상한 초과 — 완료 기록 시 7일 누락 → 부분 결과.
                log.warning("sse.parse.partial", board=market, n=len(rows), complete=complete)
                self._partial = True
                if not rows:
                    continue
            for row in rows:
                if row["code"] in seen:
                    continue
                seen.add(row["code"])
                name, name_eng = english_display(row["name"], row["eng"], "CN")
                out.append(build_company(
                    source=self.name, segment=listed_seg, name=name, name_eng=name_eng, domain=None,
                    registry=self.registry, registry_id=row["code"], ticker=row["code"],
                    market=market, listed_verified=True,
                ))
                if len(out) >= cap:
                    log.info("sse.live", segment=segment.label, n=len(out))
                    return out
        log.info("sse.live", segment=segment.label, n=len(out))
        return out


# --- SZSE(선전) ---------------------------------------------------------------

_SZSE_URL = (
    "http://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1110&TABKEY=tab1"
)
_SZSE_HEADERS = {"User-Agent": _BROWSER_UA, "Referer": "http://www.szse.cn/", "Accept": "*/*"}
_SZSE_WS = re.compile(r"[\s　﻿]+")  # 공백·전각 공백·BOM — 헤더 키 정규화.


def _szse_key(value: Any) -> str:
    return _SZSE_WS.sub("", value) if isinstance(value, str) else ""


def _szse_code(value: Any) -> str | None:
    """종목코드 셀 — 숫자 셀(1 / 1.0)로 와도 6자리 문자열(000001)로 안정화."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(int(value)).zfill(6)
    s = opt_str(str(value))
    return s.zfill(6) if s and s.isdigit() else s


def _parse_szse_workbook(wb: Any) -> list[dict[str, Any]]:
    """SZSE 상장회사 목록 xlsx — 1행 헤더(板块/公司全称/英文名称/A股代码/B股代码/公司网址…)."""
    ws = wb[wb.sheetnames[0]]
    col: dict[str, int] | None = None
    out: list[dict[str, Any]] = []
    for row in ws.iter_rows(values_only=True):
        if col is None:
            if any(_szse_key(c) == "公司全称" for c in row):
                # 헤더 셀 안의 공백("B股 简 称"·"地      区")은 제거해 키를 안정화.
                col = {_szse_key(c): i for i, c in enumerate(row) if isinstance(c, str)}
            continue

        def _raw(key: str) -> Any:
            i = col.get(key)  # noqa: B023 — col 은 헤더 발견 후 고정.
            return row[i] if i is not None and i < len(row) else None

        def _cell(key: str) -> str | None:
            v = _raw(key)
            return opt_str(str(v)) if v is not None else None

        name = _cell("公司全称")
        code = _szse_code(_raw("A股代码")) or _szse_code(_raw("B股代码"))
        if not name or not code:
            continue
        out.append({
            "code": code, "name": name, "eng": _cell("英文名称"), "website": _cell("公司网址"),
            "board": _cell("板块"),
        })
    if col is None:
        log.warning("szse.parse.no_header")
    return out


class SzseSource(ExchangeSource):
    """선전 증권거래소(SZSE) 상장회사 목록 xlsx — 웹사이트 동봉."""

    name = "szse"
    registry = "szse"
    countries = _aliases("CN")

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        fetcher = self._client()
        cap = self._settings.exchange_max_per_source
        listed_seg = self._seg(segment)
        try:
            data = fetcher.get_bytes(_SZSE_URL, headers=_SZSE_HEADERS)
            rows = _parse_szse_workbook(openpyxl.load_workbook(io.BytesIO(data), data_only=True))
        except Exception as exc:  # noqa: BLE001
            log.info("szse.error", err_type=type(exc).__name__, err=str(exc))
            self._partial = True
            return []
        if not rows:  # 빈 결과는 베이스가 기록하지 않지만 TSX/SSE 와 같은 표식으로 통일.
            log.warning("szse.parse.empty")
            self._partial = True
            return []
        out = []
        seen: set[str] = set()
        for row in rows:
            if row["code"] in seen:
                continue
            seen.add(row["code"])
            name, name_eng = english_display(row["name"], row["eng"], "CN")
            out.append(build_company(
                source=self.name, segment=listed_seg, name=name, name_eng=name_eng,
                domain=normalize_domain(row["website"]),
                registry=self.registry, registry_id=row["code"], ticker=row["code"],
                market=f"SZSE {row['board']}" if row["board"] else "SZSE", listed_verified=True,
            ))
            if len(out) >= cap:
                break
        log.info("szse.live", segment=segment.label, n=len(out))
        return out


# --- Nasdaq 스크리너(미국 전 거래소) -----------------------------------------------

_NASDAQ_URL = (
    "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&offset=0&download=true"
    "&exchange={exchange}"
)
_NASDAQ_HEADERS = {"User-Agent": _BROWSER_UA, "Accept": "application/json, text/plain, */*",
                   "Accept-Language": "en-US,en;q=0.9"}
_NASDAQ_EXCHANGES = (("nasdaq", "NASDAQ"), ("nyse", "NYSE"), ("amex", "NYSE American"))
_NASDAQ_US = "United States"

# ① 보통주 꼬리(화이트리스트) — 매치되면 보통주, 꼬리 앞이 법인명. 꼬리 뒤 "(DE)"·"New"·"Nonvoting" 허용.
_NASDAQ_COMMON_TAIL = re.compile(
    r"\s*(?:-\s*)?(?:(?:Class|Series)\s+[A-Z0-9]+\s+)?"
    r"(?:Common\s+(?:Stock|Shares?)(?:\s+of\s+Beneficial\s+Interest)?"
    r"|Ordinary\s+Shares?|Capital\s+Stock|Shares?\s+of\s+Beneficial\s+Interest"
    r"|Common\s+Units?(?:\s+(?:representing|of)\b[^()]*)?|Limited\s+Partnership\s+Units?"
    r"|(?:Subordinate|Limited|Multiple)\s+Voting\s+Shares?)"
    r"(?:\s+(?:New|Nonvoting|Non-Voting))?(?:\s*\([^)]*\))?\s*\.?\s*$",
    re.I,
)
# 꼬리 없는 "… Class B" 도 보통주 클래스 표기(Lennar Corporation Class B).
_NASDAQ_CLASS_TAIL = re.compile(r"\s+(?:Class|Series)\s+[A-Z0-9]+(?:\s*\([^)]*\))?\s*$")
# ② 꼬리 없는 이름에만 적용하는 증권 문맥 — 워런트·유닛(SPAC)·권리·우선주·채권·예탁증서·수익률·펀드/신탁상품.
_NASDAQ_SECURITY = re.compile(
    r"\bWarrants?\b|\bUnits?\b|\bRights?\s*\.?\s*$|\d+(?:\.\d+)?\s*%|\bdue\s+\d{4}"
    r"|\bPreferred\b|\bPreference\b|\bDepositary\b|\bDebentures?\b|\bNotes?\s*\.?\s*$"
    r"|\bSenior\s+Notes?\b|\bSubordinated\b|\bETF\b|\bETN\b|\bFund\s*\.?\s*$|\bPortfolio\s*$"
    r"|\bIndex\s+Fund\b|\bClosed[- ]End\b",
    re.I,
)
# 꼬리 "Trust" 는 BlackRock 계열 폐쇄형 펀드가 대부분 — 단, "Real Estate Investment Trust" 는 REIT 법인.
_NASDAQ_TRUST_TAIL = re.compile(r"\bTrust\s*\.?\s*$", re.I)
_NASDAQ_REIT = re.compile(r"Real\s+Estate\s+Investment\s+Trust", re.I)
# SPAC(블랭크체크) — 사업·IR 담당이 없는 껍데기 법인(2026-10-23 NASDAQ 표본 앞머리 다수).
_NASDAQ_SPAC = re.compile(
    r"\b(?:Acquisition|Merger)\s+(?:Corp|Co|Company|Inc|Ltd|Holdings|Group|Limited)\b|\bSPAC\b",
    re.I,
)
_NASDAQ_THE_RE = re.compile(r"\s*\(The\)\s*", re.I)


def _nasdaq_company_name(raw: str) -> str | None:
    """스크리너 종목명 → 법인명(보통주가 아니면 None). 모듈 docstring 의 판정 순서."""
    name = (raw or "").strip()
    if not name:
        return None
    m = _NASDAQ_COMMON_TAIL.search(name)
    if m and m.start() > 0:
        base = name[:m.start()]
    else:
        # 단독 "Class B" 꼬리는 떼되, 남은 이름이 증권 문맥("… Preferred Stock Series B")이면 제외.
        mc = _NASDAQ_CLASS_TAIL.search(name)
        base = name[:mc.start()] if mc and mc.start() > 0 else name
        if _NASDAQ_SECURITY.search(base):
            return None
        if _NASDAQ_TRUST_TAIL.search(base) and not _NASDAQ_REIT.search(base):
            return None
    base = _NASDAQ_THE_RE.sub(" ", base).strip(" -–")
    if not base or _NASDAQ_SPAC.search(base):
        return None
    return base


def _nasdaq_symbol(symbol: str) -> str:
    """스크리너 심볼 정규화 — 클래스 구분자 ``/``(BRK/B) → ``.``(yahoo_symbol 이 US 는 ``.``→``-``)."""
    return symbol.replace("/", ".")


def _nasdaq_rows(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """응답 → (미국 법인 보통주 행, 원시 행 수). 원시 행 수 0 = 스키마 이상(부분 결과 판정용)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return [], 0
    out = []
    blank_country = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        country = opt_str(row.get("country"))
        if country is None:
            blank_country += 1
            continue
        if country != _NASDAQ_US:
            continue
        symbol = opt_str(row.get("symbol"))
        if not symbol or "^" in symbol:  # ^ = 우선주 시리즈 표기(ABR^D).
            continue
        name = _nasdaq_company_name(str(row.get("name") or ""))
        if name:
            out.append({"symbol": _nasdaq_symbol(symbol), "name": name})
    if blank_country:
        log.info("nasdaq.blank_country", n=blank_country)
    return out, len(rows)


def _nasdaq_pick_symbol(symbols: list[str]) -> str:
    """같은 법인의 복수 클래스 중 하나 — 결정적(구분자 없는 심볼 → 짧은 것 → 사전순)."""
    return min(symbols, key=lambda s: ("." in s, len(s), s))


class NasdaqScreenerSource(ExchangeSource):
    """Nasdaq 스크리너로 미국 3대 거래소(NASDAQ·NYSE·NYSE American) 상장 보통주 발행사."""

    name = "nasdaq"
    registry = "nasdaq"
    countries = _aliases("US")

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        fetcher = self._client()
        cap = self._settings.exchange_max_per_source
        listed_seg = self._seg(segment)
        # 법인명(소문자) → (첫 등장 순서, 시장, 심볼들). 거래소 간 중복 상장은 첫 거래소로.
        groups: dict[str, tuple[int, str, str, list[str]]] = {}
        for path, market in _NASDAQ_EXCHANGES:
            try:
                rows, raw_n = _nasdaq_rows(fetcher.get_json(
                    _NASDAQ_URL.format(exchange=path), headers=_NASDAQ_HEADERS
                ))
            except Exception as exc:  # noqa: BLE001 — 한 거래소 실패는 부분 결과(재수집 기록 생략).
                log.info("nasdaq.error", exchange=market, err_type=type(exc).__name__, err=str(exc))
                self._partial = True
                continue
            if raw_n == 0:  # 200 이지만 스키마 이상/0행 — 부분 결과(필터 후 0건과 구분).
                log.warning("nasdaq.parse.empty", exchange=market)
                self._partial = True
                continue
            for row in rows:
                key = row["name"].lower()
                if key in groups:
                    groups[key][3].append(row["symbol"])
                else:
                    groups[key] = (len(groups), market, row["name"], [row["symbol"]])
        out: list[DiscoveredCompany] = []
        seen_symbol: set[str] = set()
        for _, market, name, symbols in sorted(groups.values(), key=lambda g: g[0]):
            symbol = _nasdaq_pick_symbol(symbols)
            if symbol in seen_symbol:
                continue
            seen_symbol.add(symbol)
            out.append(build_company(
                source=self.name, segment=listed_seg, name=name, domain=None,
                registry=self.registry, registry_id=symbol, ticker=symbol,
                market=market, listed_verified=True,
            ))
            if len(out) >= cap:
                break
        log.info("nasdaq.live", segment=segment.label, n=len(out))
        return out
