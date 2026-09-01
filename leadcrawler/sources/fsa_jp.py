"""금융청(FSA) 免許・登録 일람 발견 소스 — 일본 금융사 전수 등록처(은행·신금·금융상품거래업자).

금융청이 게시하는 등록·면허 일람 Excel(무인증·무과금·고정 URL) 을 읽어 **상호+법인번호**를
열거하고, gBizINFO(경제산업성 법인 API, 무료 토큰) 로 **공식 웹사이트·영문 상호**를 붙인다.
2026-08-31 실측: 金融商品取引業者 1,951(第一種 290·投資運用業 463·投資助言 1,005·第二種 1,228,
법인번호 100%) · 銀行免許 141(외국은행지점 별도 시트 제외) · 信用金庫 254. JSDA/IMAJ 협회
명부(``jp_assoc``, 회원 791) 보다 넓은 모집단 — 비회원 운용사·조언업자·지방 신금까지.

- 키: ``reg:fsa_jp:<법인번호>``(13자리, 등록처 tier). EDINET 상장사는 ``reg:edinet`` 키로 이미
  원장에 있으므로 **EDINET 코드리스트의 상장 법인번호는 제외**한다(교차키 중복 방지 — 같은
  ``listed='unknown'`` 세그먼트에서 두 소스가 함께 돈다). 코드리스트는 24h 캐시, 못 받으면
  제외 없이 진행(도메인 동치 dedup 이 백스톱).
- 업종: 파일 단위 라벨 — 銀行免許·信用金庫 → 은행 / 金融商品取引業者 중 **第一種·投資運用業·
  投資助言 중 하나라도 ○** → 증권·자산운용(1,331). 第二種 단독(620: 부동산 펀드 판매·크라우드
  펀딩 등) 은 순도 낮아 제외. 투자조언도 '증권·자산운용'(투자자문 라벨 미사용 규칙).
- 도메인·영문명: ``gbizinfo_api_token`` 이 있으면 법인번호로 gBizINFO 조회(``company_url`` →
  도메인, ``name_en`` → 표시명, 일문은 name_eng 보관 — EDINET #412 규약). 토큰이 없으면 도메인
  없이 열거(EDINET 과 같이 resolve_domains 경로에 맡김) — 인스턴스당 1회 경고.
- 커서: 파일별 로컬 슬라이스(키=segment.label#list{i}), 파일 실패는 그 파일만 결과 0·커서
  유지. **소진한 파일은 끝에 머문다**(base 의 즉시 0 리셋과 다름) — 앞 파일이 매 런 cap 을
  재소비해 뒤 파일(증권 1,305)이 기아 상태가 되는 걸 막고, 선택 파일이 **전부** 소진되면
  그때 모두 0 으로 되감아 다음 사이클(월 갱신분 재검증)을 시작한다. 프로세스 캐시: 일람 Excel 24h(실패는 캐시 안 함·stale-if-error,
  파일별 락) / gBizINFO 24h(실패 1h, 락으로 스탬피드 방지). gBizINFO 가 통째로 죽으면 같은
  discover() 안에서 연속 실패 3회째부터 나머지 행은 조회를 건너뛴다(행마다 타임아웃 대기 방지).
- 상장여부: 일람은 상장을 안 알려줌 → 스코프값 그대로(listed_verified=False), ``listed='listed'``
  세그먼트엔 미적용.
- dry_run 은 네트워크 없이 결정적 더미(전 소스 계약).

ponytail: 登録金融機関 일람(896, 은행·신금·신조·농협의 증권업 겸영 등록) 은 銀行·信金 일람과
겹쳐 제외. 보험·신탁·대금업 일람은 택소노미 라벨(보험 등) 수요가 생기면 ``_LISTS`` 에 한 줄.
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
from urllib.parse import urlparse

from ..config import Settings
from ..logging import get_logger
from .base import (
    DiscoveredCompany,
    Segment,
    SupportsCursorStore,
    build_company,
    opt_str,
)
from .countries import resolve_country
from .edinet import _CODELIST_URL, _COL_CORP_NO, _COL_LISTED, _LISTED_MARK, _parse_codelist
from .http import Fetcher, HostRateLimiters, SupportsFetch
from .industry import is_broad_industry, resolve_industry_label

log = get_logger("sources.fsa_jp")

_SEC = "증권·자산운용"
_BANK = "은행"
_BASE = "https://www.fsa.go.jp/menkyo/menkyoj/"
# (URL, 라벨, 시트 제외 키워드) — 순서 = 신뢰도·크기(은행 → 신금 → 금융상품거래업자).
_LISTS: tuple[tuple[str, str, str | None], ...] = (
    (_BASE + "ginkou.xlsx", _BANK, "外国銀行"),  # 외국은행지점 시트 제외(국가 오분류 방지).
    (_BASE + "shinkin.xlsx", _BANK, None),
    (_BASE + "kinyushohin.xlsx", _SEC, None),
)
_LABELS = frozenset(label for _, label, _ in _LISTS)
_GBIZ_URL = "https://info.gbiz.go.jp/hojin/v1/hojin/{corp_no}"

_COL_CORP = "法人番号"
_NAME_COLS = ("金融商品取引業者名", "銀行名", "名称")
_COL_ADDR = "本店等所在地"
_COL_PHONE = "代表等電話番号"
# 金融商品取引業者 業務の種別 부헤더 — 이 중 하나라도 ○ 인 행만(第二種 단독 제외).
_SEC_FLAGS = ("第一種", "投資助言", "投資運用")
_CORP_NO = re.compile(r"^\d{13}$")
_LATIN_NAME = re.compile(r"^[A-Za-z0-9 .,&'()\-/+]{3,120}$")

_XLSX_TTL_S = 24 * 3600
_GBIZ_TTL_S = 24 * 3600
_NEG_TTL_S = 3600
_xlsx_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
_edinet_cache: tuple[float, frozenset[str]] | None = None  # 상장 법인번호 집합(24h).
_edinet_lock = threading.Lock()
_gbiz_cache: dict[str, tuple[float, dict[str, str] | None]] = {}
_gbiz_lock = threading.Lock()  # check/write 구간만 — 병렬 워커의 같은 법인번호 중복 호출 방지.
_GBIZ_TRIP = 3  # 한 discover() 안 연속 실패 허용치 — 넘으면 이번 호출은 gBizINFO 를 포기.
_locks: dict[str, threading.Lock] = {url: threading.Lock() for url, _, _ in _LISTS}
_lock_fallback = threading.Lock()


def _cell(v: object) -> str:
    return " ".join(str(v).split()) if v is not None else ""


def parse_list(blob: bytes, *, skip_sheet: str | None = None, sec_flags: bool = False) -> list[dict[str, str]]:
    """일람 Excel → 행 dict(name·corp_no·address·phone). 형식 붕괴는 빈 목록(graceful).

    헤더 행 = ``法人番号`` 가 있는 첫 행. ``sec_flags`` 면 헤더 다음 행(부헤더)에서 第一種/
    投資助言/投資運用 열을 찾아 하나라도 ○ 인 행만 남긴다(第二種 단독 제외).
    """
    try:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    except Exception as exc:
        log.info("fsa_jp.xlsx_error", err=str(exc))
        return []
    out: list[dict[str, str]] = []
    for ws in wb.worksheets:
        if skip_sheet and skip_sheet in (ws.title or ""):
            continue
        rows = [r for r in ws.iter_rows(values_only=True) if any(c is not None for c in r)]
        # 헤더 = 앞 30행 중 '法人番号' **정확 일치** 셀이 있는 첫 행(주석 행의 부분일치 오탐 방지).
        hi = next(
            (i for i, r in enumerate(rows[:30]) if any(_cell(c) == _COL_CORP for c in r)), None
        )
        if hi is None:
            if rows:  # 빈 시트가 아닌데 헤더가 없다 — 포맷 변경 가시화(침묵 스킵 금지).
                log.warning("fsa_jp.header_missing", sheet=ws.title, rows=len(rows))
            continue
        hdr = [_cell(c) for c in rows[hi]]
        try:
            ci = hdr.index(_COL_CORP)
            ni = next(hdr.index(n) for n in _NAME_COLS if n in hdr)
        except (ValueError, StopIteration):
            log.warning("fsa_jp.header_mismatch", sheet=ws.title, header=hdr[:8])
            continue
        ai = hdr.index(_COL_ADDR) if _COL_ADDR in hdr else None
        pi = hdr.index(_COL_PHONE) if _COL_PHONE in hdr else None
        flag_idx: list[int] = []
        start = hi + 1
        if sec_flags and start < len(rows):
            sub = [_cell(c) for c in rows[start]]
            flag_idx = [i for i, c in enumerate(sub) if any(f in c for f in _SEC_FLAGS)]
            if flag_idx:
                start += 1
            else:
                log.warning("fsa_jp.flags_missing", sheet=ws.title)
                continue  # 업무종별을 못 읽으면 第二種 단독까지 섞여 순도 규약 위반 → 시트 스킵.
        for r in rows[start:]:
            name = _cell(r[ni]) if ni < len(r) else ""
            corp = _cell(r[ci]) if ci < len(r) else ""
            if not name or not _CORP_NO.match(corp):
                continue  # 소계·주석 행, 법인번호 없는 조합 등.
            if flag_idx and not any(i < len(r) and "○" in _cell(r[i]) for i in flag_idx):
                continue
            out.append({
                "name": name, "corp_no": corp,
                "address": _cell(r[ai]) if ai is not None and ai < len(r) else "",
                "phone": _cell(r[pi]) if pi is not None and pi < len(r) else "",
            })
    return out


def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host if host and "." in host else None


class FsaJpSource:
    """금융청 등록·면허 일람 기반 일본 금융사 발견 소스(등록처 tier — EDINET 뒤·협회 명부 앞)."""

    name = "fsa_jp"

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
        self._warned_no_token = False

    def applies_to(self, segment: Segment) -> bool:
        """JP 전용 + 비상장/미상 스코프 + (broad 또는 은행·증권·자산운용)."""
        country = resolve_country(segment.country)
        if country is None or country.iso2 != "JP":
            return False
        if segment.listed == "listed":
            return False  # 상장은 EDINET 전수(스코프값 오염 방지).
        if is_broad_industry(segment.industry):
            return True
        return resolve_industry_label(segment.industry) in _LABELS

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        if self._settings.dry_run:
            return self._dry(segment)
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{segment.industry} FSA JP Co {i}",
                domain=f"jp-fsa{i}.co.jp",  # dry 는 실존 active 시뮬레이션(GLEIF 규약).
                registry="fsa_jp",
                registry_id=f"{1000000000000 + i:013d}",
                industry_code_label=_SEC,
            )
            for i in range(self._count)
        ]

    def _client(self) -> SupportsFetch:
        if self._fetcher is None:
            self._fetcher = Fetcher(
                user_agent=self._settings.discovery_user_agent,
                min_interval=self._settings.http_request_delay,
                timeout=self._settings.http_timeout,
                rate_limiters=self._rate_limiters,
            )
        return self._fetcher

    def _rows(self, url: str, skip_sheet: str | None, sec_flags: bool) -> list[dict[str, str]] | None:
        """일람 1개의 행 — 프로세스 캐시(24h) 우선. 실패/0건은 None(캐시 안 함)."""
        now = time.monotonic()
        with _locks.get(url) or _lock_fallback:
            hit = _xlsx_cache.get(url)
            if hit is not None and now - hit[0] < _XLSX_TTL_S:
                return hit[1]
            try:
                blob = self._client().get_bytes(url)
            except Exception as exc:
                log.info("fsa_jp.fetch_error", url=url, err=str(exc))
                return hit[1] if hit is not None else None  # stale-if-error.
            rows = parse_list(blob, skip_sheet=skip_sheet, sec_flags=sec_flags)
            if not rows:
                log.warning("fsa_jp.parse_empty", url=url)
                return hit[1] if hit is not None else None
            _xlsx_cache[url] = (now, rows)
            return rows

    def _gbiz(self, corp_no: str) -> tuple[dict[str, str] | None, bool]:
        """gBizINFO 법인 기본정보(company_url·name_en) → (정보, 호출 실패 여부). 프로세스 캐시.

        실패 여부는 호출부의 연속 실패 차단기용 — 캐시 적중(부정 캐시 포함)은 실패로 안 센다.
        """
        token = self._settings.gbizinfo_api_token
        if not token:
            return None, False
        now = time.monotonic()
        with _gbiz_lock:
            hit = _gbiz_cache.get(corp_no)
            if hit is not None and now - hit[0] < (_GBIZ_TTL_S if hit[1] is not None else _NEG_TTL_S):
                return hit[1], False
        info: dict[str, str] | None = None
        failed = False
        try:
            text = self._client().get_text(
                _GBIZ_URL.format(corp_no=corp_no),
                headers={"X-hojinInfo-api-token": token, "Accept": "application/json"},
            )
            items = json.loads(text).get("hojin-infos") or []
            if items:
                first = items[0]
                info = {
                    "company_url": opt_str(first.get("company_url")) or "",
                    "name_en": opt_str(first.get("name_en")) or "",
                }
        except Exception as exc:  # 429/5xx/JSON 오류 — 이 행만 도메인 없이 진행(1h 후 재시도).
            log.info("fsa_jp.gbiz_error", corp_no=corp_no, err=str(exc))
            failed = True
        with _gbiz_lock:
            prev = _gbiz_cache.get(corp_no)
            if failed and prev is not None and prev[1] is not None:
                return prev[1], failed  # 늦은 실패가 앞선 성공을 덮지 않는다(동시 호출 경합).
            _gbiz_cache[corp_no] = (now, info)
        return info, failed

    def _edinet_listed(self) -> frozenset[str]:
        """EDINET 코드리스트의 상장 내국법인 법인번호 집합(24h 캐시). 실패 시 빈 집합(제외 없음)."""
        global _edinet_cache
        now = time.monotonic()
        with _edinet_lock:
            if _edinet_cache is not None and now - _edinet_cache[0] < _XLSX_TTL_S:
                return _edinet_cache[1]
            try:
                rows = _parse_codelist(self._client().get_bytes(_CODELIST_URL))
            except Exception as exc:
                log.info("fsa_jp.edinet_fetch_error", err=str(exc))
                return _edinet_cache[1] if _edinet_cache is not None else frozenset()
            listed = frozenset(
                r.get(_COL_CORP_NO, "").strip() for r in rows
                if r.get(_COL_LISTED, "").strip() == _LISTED_MARK and r.get(_COL_CORP_NO, "").strip()
            )
            if listed:
                _edinet_cache = (now, listed)
            else:
                log.warning("fsa_jp.edinet_empty")
            return listed

    def _cursor(self, list_seg: Segment) -> int:
        """저장된 커서(없으면 0). base.cursor_offset 과 달리 끝(=total) 도 그대로 돌려준다."""
        if self._cursor_store is None:
            return 0
        pos = self._cursor_store.get(self.name, list_seg.label)
        return pos if pos > 0 else 0

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        cap = self._settings.discovery_max_per_source
        if cap <= 0:
            return []
        if not self._settings.gbizinfo_api_token and not self._warned_no_token:
            self._warned_no_token = True
            log.warning("fsa_jp.no_gbiz_token", hint="LEADCRAWLER_GBIZINFO_API_TOKEN 설정 시 도메인·영문명 동봉")
        want: str | None = None
        if not is_broad_industry(segment.industry):
            want = resolve_industry_label(segment.industry)
        out: list[DiscoveredCompany] = []
        gbiz_fail_streak = 0  # 연속 실패 차단기(리뷰 HIGH) — 이번 호출 안에서만 유효.
        selected = [
            (i, url, label, skip)
            for i, (url, label, skip) in enumerate(_LISTS)
            if want is None or label == want
        ]
        if not selected:
            return []
        listed = self._edinet_listed()
        for cycle in (0, 1):  # 1회차: 저장 커서부터. 전부 소진·0건이면 0 으로 되감아 2회차 1번만.
            exhausted_all = True
            for i, url, label, skip_sheet in selected:
                if len(out) >= cap:
                    exhausted_all = False
                    break
                rows = self._rows(url, skip_sheet, sec_flags=(label == _SEC))
                if rows is None:
                    exhausted_all = False  # 실패 파일: 결과 없음 + 커서 유지(다음 런 재시도).
                    continue
                list_seg = segment.model_copy(update={"region": f"list{i}"})  # 커서 키 전용.
                start = 0 if cycle else min(self._cursor(list_seg), len(rows))
                pos = start
                for rec in rows[start:]:
                    pos += 1
                    if rec["corp_no"] in listed:
                        continue  # EDINET 상장사 — reg:edinet 으로 이미 원장에 있음.
                    info: dict[str, str] = {}
                    if gbiz_fail_streak < _GBIZ_TRIP:
                        got, failed = self._gbiz(rec["corp_no"])
                        info = got or {}
                        gbiz_fail_streak = gbiz_fail_streak + 1 if failed else 0
                        if gbiz_fail_streak >= _GBIZ_TRIP:
                            log.warning(
                                "fsa_jp.gbiz_tripped", segment=segment.label, streak=gbiz_fail_streak
                            )
                    eng = info.get("name_en") or ""
                    if eng and not _LATIN_NAME.match(eng):
                        eng = ""
                    out.append(
                        build_company(
                            source=self.name,
                            segment=segment,
                            name=eng or rec["name"],
                            domain=_domain_of(info.get("company_url")),
                            registry="fsa_jp",
                            registry_id=rec["corp_no"],
                            industry_code_label=label,
                            address=opt_str(rec["address"]),
                            reg_no=rec["corp_no"],
                            phone=opt_str(rec["phone"]),
                            name_eng=rec["name"] if eng else None,
                        )
                    )
                    if len(out) >= cap:
                        break
                if pos < len(rows):
                    exhausted_all = False
                if self._cursor_store is not None and pos != start:
                    self._cursor_store.advance(self.name, list_seg.label, pos)  # 끝이면 끝에 머문다.
            if not exhausted_all or out or cycle:
                break
            log.info("fsa_jp.cycle_reset", segment=segment.label)  # 전부 소진·0건 → 되감기.
        log.info("fsa_jp.live", segment=segment.label, n=len(out))
        return out
