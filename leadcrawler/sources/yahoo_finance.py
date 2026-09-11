"""Yahoo Finance 회사 프로필 — 상장사 티커로 공식 웹사이트를 무료로 얻는다(도메인 해석 ⓪ 경로).

배경(2026-09-11 실측): Serper 소진 상태에서 도메인 없는 상장 발견분(JP EDINET 1,223·US EDGAR
2,898)이 승격을 못 넘었다. 표본 30건에서 티커→``quoteSummary?modules=assetProfile`` 의
``website`` 가 JP 28/30 을 맞혔고(무료·정확 매칭), 이름 검색·LLM 추측은 그보다 훨씬 낮았다.

프로토콜: ``fc.yahoo.com`` GET 으로 쿠키 → ``/v1/test/getcrumb`` 로 crumb → 조회에 ``crumb``
동봉. 비공식 API 라 (a) 401 이면 crumb 1회 재발급, (b) 429/연속 오류면 인스턴스 래치로 이후 호출을
건너뛴다(Serper 소진 latch 와 같은 관례), (c) 호출 간격을 둔다(``min_interval``). 무료라 cost_ledger
미적재. dry_run 은 호출측(DomainResolver)이 먼저 걸러 여기 오지 않는다.

ponytail: 티커 접미(거래소) 표는 정적 dict — 새 국가는 한 줄 추가. CN 은 티커 첫 자리로 상해/심천 분기.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from ..dedup import normalize_domain
from ..logging import get_logger

log = get_logger("sources.yahoo_finance")

_COOKIE_URL = "https://fc.yahoo.com"
_CRUMB_URL = "https://query2.finance.yahoo.com/v1/test/getcrumb"
_SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

# ISO2 → Yahoo 심볼 접미. 값이 None 인 국가는 미지원(호출 안 함).
_SUFFIX: dict[str, str] = {
    "US": "", "JP": ".T", "GB": ".L", "DE": ".DE", "FR": ".PA", "AU": ".AX", "CA": ".TO",
    "SG": ".SI", "PH": ".PS", "TH": ".BK", "ID": ".JK", "MY": ".KL", "IN": ".NS", "TW": ".TW",
    "HK": ".HK", "IT": ".MI", "ES": ".MC", "NL": ".AS", "CH": ".SW", "SE": ".ST", "NO": ".OL",
    "DK": ".CO", "FI": ".HE", "BR": ".SA", "MX": ".MX", "TR": ".IS", "NZ": ".NZ", "BE": ".BR",
    "AT": ".VI", "PL": ".WA", "PT": ".LS", "IE": ".IR", "IL": ".TA", "GR": ".AT", "SA": ".SR",
    "VN": ".VN",  # HOSE/HNX 공통(2026-09-11 실측 VNM.VN·VIC.VN 200, .HM 은 404)
}
# 연속 오류 이 횟수면 래치(비공식 API 차단·장애 시 잔여 행마다 대기하지 않게).
_LATCH_AFTER = 5


def yahoo_symbol(ticker: str | None, country: str | None, market: str | None = None) -> str | None:
    """등록처 티커 + 국가(+시장) → Yahoo 심볼. 미지원 국가·빈 티커는 None."""
    t = (ticker or "").strip().upper()
    cc = (country or "").strip().upper()
    if not t or not cc:
        return None
    if cc == "KR":
        if len(t) == 12 and t.startswith("KR7"):  # fsc 는 ticker 에 ISIN(KR7005930003) — 종목코드 6자리 추출.
            t = t[3:9]
        # ponytail: KONEX 는 Yahoo 미수록이 대부분 — .KS 로 흘려 404(무오류)로 끝난다.
        return f"{t}.KQ" if (market or "").upper() == "KOSDAQ" else f"{t}.KS"
    if cc == "US":
        t = t.replace(".", "-")  # 클래스 주식 BRK.A → Yahoo 표기 BRK-A
    if cc == "CN":
        return f"{t}.SS" if t.startswith(("6", "9")) else f"{t}.SZ"
    if cc == "HK":
        return f"{t.zfill(4)}.HK"
    if cc == "JP":
        t = t.split(".")[0]  # EDINET 은 '7203' — 이미 접미가 붙은 값은 정리.
    suffix = _SUFFIX.get(cc)
    if suffix is None:
        return None
    return t if t.endswith(suffix) and suffix else f"{t}{suffix}"


class YahooProfile:
    """티커 → 공식 웹사이트 도메인(없으면 None). 워커 스레드 공유 안전(락으로 직렬화)."""

    def __init__(
        self, *, min_interval: float = 0.5, timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": _UA}, timeout=timeout, follow_redirects=True, transport=transport,
        )
        self._min_interval = min_interval
        self._crumb: str | None = None
        self._errors = 0
        self._latched = False
        self._last = 0.0
        # 락은 페이싱 타임스탬프·오류 카운터·crumb 발급만 감싼다 — 조회 GET 은 락 밖(공유
        # 인스턴스라 락 안에서 네트워크를 하면 전 워커가 직렬화된다, 리뷰 MED).
        self._lock = threading.Lock()
        self._crumb_lock = threading.Lock()

    def website(self, ticker: str | None, country: str | None, market: str | None = None) -> str | None:
        symbol = yahoo_symbol(ticker, country, market)
        if symbol is None:
            return None
        with self._lock:
            if self._latched:
                return None
            self._pace()
        try:
            data = self._summary(symbol)
        except Exception as exc:
            with self._lock:
                self._errors += 1
                log.info("yahoo.error", symbol=symbol, err=str(exc)[:120])
                # ponytail: 연속 오류만 센다(성공이 섞이는 소프트 스로틀은 못 잡음 — Serper 래치와 동일).
                if self._errors >= _LATCH_AFTER and not self._latched:
                    self._latched = True
                    log.warning("yahoo.latched", errors=self._errors)
            return None
        with self._lock:
            self._errors = 0
        return _website_from(data)

    def _pace(self) -> None:
        """호출 간격 예약 — 락 안에서 호출. 슬롯을 먼저 잡고(타임스탬프 전진) 필요한 만큼 잔다."""
        now = time.monotonic()
        slot = max(now, self._last + self._min_interval)
        self._last = slot
        if slot > now:
            time.sleep(slot - now)

    def _ensure_crumb(self, *, force: bool = False) -> str:
        with self._crumb_lock:
            if self._crumb is None or force:
                self._client.get(_COOKIE_URL)  # 404 라도 세션 쿠키가 붙는다.
                r = self._client.get(_CRUMB_URL)
                r.raise_for_status()
                crumb = r.text.strip()
                if not crumb or len(crumb) > 32 or "<" in crumb:
                    raise RuntimeError("crumb 발급 실패")
                self._crumb = crumb
            return self._crumb

    def _summary(self, symbol: str) -> Any:
        crumb = self._ensure_crumb()
        r = self._client.get(
            _SUMMARY_URL.format(symbol=symbol), params={"modules": "assetProfile", "crumb": crumb},
        )
        if r.status_code == 401:  # crumb 만료 → 1회 재발급 후 재시도.
            crumb = self._ensure_crumb(force=True)
            r = self._client.get(
                _SUMMARY_URL.format(symbol=symbol), params={"modules": "assetProfile", "crumb": crumb},
            )
        if r.status_code == 404:  # 미상장/심볼 없음 — 오류로 세지 않는다.
            return None
        if r.status_code == 429:
            raise RuntimeError("429 rate limited")
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._client.close()


def _website_from(data: Any) -> str | None:
    try:
        result = data["quoteSummary"]["result"]
        site = (result or [{}])[0].get("assetProfile", {}).get("website")
    except (TypeError, KeyError, AttributeError, IndexError):
        return None
    if not site:
        return None
    dom = normalize_domain(str(site))
    return dom or None
