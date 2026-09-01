"""gBizINFO(경제산업성 법인 API) 발견 소스 — 일본 전 법인 중 종업원 10인 이상 株式会社 전수.

KR 의 NPS(국민연금 사업장) 에 해당하는 일본 전수 소스. 검색 API 로 **도도부현 × 종업원수 밴드**
슬라이스를 열거하고(슬라이스당 최대 5,000×10페이지), 행별 상세 조회로 **공식 웹사이트·영문명**을
붙인다(2026-09-01 실측: 도쿄 ≥10인 株式会社 약 2만, 상세의 company_url 보유 33%·business_summary
55%·name_en 2.5%). 무료 토큰(``gbizinfo_api_token``) 필수 — 없으면 이 소스는 비활성(applies_to False).

- 업종: gBizINFO 에 업종 필터가 없다 → :mod:`jp_industry` 규칙으로 **상호(무과금) → 사업요약(상세
  조회 후)** 순 분류. 못 정한 행은 버린다(세그먼트 작업은 구체 업종 필수라 원장에 쌓아도 못 잡음).
  구체 업종 세그먼트에서 상호가 다른 업종으로 분류된 행은 상세 조회 없이 건너뛴다(콜 절약).
- 키: ``reg:gbiz:<법인번호>``. **EDINET 상장 법인번호·금융청 일람 법인번호는 제외**(각각 reg:edinet·
  reg:fsa_jp 로 이미 들어오므로 교차키 중복 방지 — ``fsa_jp`` 의 공유 헬퍼 재사용).
- 도메인 없는 행(company_url 공란 67%)은 **원장에 넣지 않는다** — 도메인 해석 수단(Serper)이 없는
  현재로선 GLEIF JP 4,000건처럼 미승격 부채만 된다. ponytail: 해석기가 생기면 domain=None 으로 열거.
- 슬라이스 순서 = 큰 회사·큰 지역 먼저(종업원 300+ → 10-19, 도쿄·오사카·가나가와·아이치·…).
  슬라이스별 로컬 커서(끝 유지·전부 소진 시 되감기, ``fsa_jp`` 와 동일), 슬라이스 목록 24h 캐시,
  상세 24h 캐시(실패 1h)·연속 실패 차단기. 한 discover() 의 스캔 상한(``_MAX_SCAN``)으로 런 시간 제한.
- 상장여부: 스코프값 그대로(미검증). ``listed='listed'`` 세그먼트엔 미적용(EDINET 전담).
- dry_run 은 네트워크 없이 결정적 더미.

ponytail: 有限会社·合同会社(302·305)·10인 미만은 제외(순도·물량 균형 — 필요해지면 ``_TYPES``/
``_BANDS`` 한 줄). 시 단위(city) 분할은 슬라이스가 5만을 넘을 때만(현재 도쿄 300+ 도 5만 미만).
"""

from __future__ import annotations

import json
import re
import threading
import time
from urllib.parse import urlparse

from ..config import Settings
from ..logging import get_logger
from .base import DiscoveredCompany, Segment, SupportsCursorStore, build_company, opt_str
from .countries import resolve_country
from .fsa_jp import _GBIZ_URL, edinet_listed_corp_numbers, fsa_corp_numbers
from .http import Fetcher, HostRateLimiters, SupportsFetch
from .industry import is_broad_industry, resolve_industry_label
from .jp_industry import classify_jp, rule_labels

log = get_logger("sources.gbiz_jp")

_SEARCH_URL = "https://api.info.gbiz.go.jp/hojin/v2/hojin"
_PAGE = 5000
_MAX_PAGES = 10
# 도도부현 JIS 코드 — 법인 수가 많은 순(도쿄·오사카·가나가와·아이치·사이타마·지바·효고·후쿠오카·홋카이도)
# 다음 나머지 오름차순. 큰 지역부터 훑어야 초기 런의 수율이 높다.
_PREFS: tuple[str, ...] = ("13", "27", "14", "23", "11", "12", "28", "40", "01") + tuple(
    f"{i:02d}" for i in range(2, 48) if f"{i:02d}" not in {"13", "27", "14", "23", "11", "12", "28", "40"}
)
_BANDS: tuple[tuple[int, int | None], ...] = ((300, None), (100, 299), (50, 99), (20, 49), (10, 19))
_TYPES = "301"  # 株式会社 만.
_MAX_SCAN = 3000  # 한 discover() 에서 훑는 최대 행(상세 콜 상한 ≈ 런당 수 분).
_GBIZ_TRIP = 3
_LIST_TTL_S = 24 * 3600
_DETAIL_TTL_S = 24 * 3600
_NEG_TTL_S = 3600
_LATIN_NAME = re.compile(r"^[A-Za-z0-9 .,&'()\-/+]{3,120}$")

_list_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
_detail_cache: dict[str, tuple[float, dict[str, str] | None]] = {}
_lock = threading.Lock()


def slices() -> list[tuple[str, str, int, int | None]]:
    """(슬라이스 id, 도도부현, 종업원 하한, 상한) — 밴드(큰 회사 먼저) × 도도부현(큰 지역 먼저)."""
    return [
        (f"{pref}-{lo}-{hi or 'max'}", pref, lo, hi)
        for lo, hi in _BANDS
        for pref in _PREFS
    ]


def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host if host and "." in host else None


class GbizJpSource:
    """gBizINFO 검색+상세 기반 일본 전수 발견 소스(집계원 tier — 등록처·협회 명부 뒤)."""

    name = "gbiz_jp"

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
        """JP + 토큰 보유 + 비상장/미상 스코프 + (broad 또는 분류기가 낼 수 있는 라벨)."""
        if not self._settings.dry_run and not self._settings.gbizinfo_api_token:
            return False
        country = resolve_country(segment.country)
        if country is None or country.iso2 != "JP":
            return False
        if segment.listed == "listed":
            return False
        if is_broad_industry(segment.industry):
            return True
        return resolve_industry_label(segment.industry) in rule_labels()

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        if self._settings.dry_run:
            return self._dry(segment)
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        return [
            build_company(
                source=self.name, segment=segment, name=f"{segment.industry} gBiz Co {i}",
                domain=f"jp-gbiz{i}.co.jp", registry="gbiz", registry_id=f"{2000000000000 + i:013d}",
                industry_code_label="기타 제조",
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

    def _headers(self) -> dict[str, str]:
        return {"X-hojinInfo-api-token": self._settings.gbizinfo_api_token, "Accept": "application/json"}

    def _slice_rows(self, sid: str, pref: str, lo: int, hi: int | None) -> list[dict[str, str]] | None:
        """슬라이스의 검색 행(법인번호·상호·소재지) — 페이지 순회, 24h 캐시. 실패 시 None(stale-if-error)."""
        now = time.monotonic()
        with _lock:
            hit = _list_cache.get(sid)
            if hit is not None and now - hit[0] < _LIST_TTL_S:
                return hit[1]
        rows: list[dict[str, str]] = []
        for page in range(1, _MAX_PAGES + 1):
            params = {
                "prefecture": pref, "exist_flg": "true", "corporate_type": _TYPES,
                "employee_number_from": str(lo), "limit": str(_PAGE), "page": str(page),
            }
            if hi is not None:
                params["employee_number_to"] = str(hi)
            try:
                text = self._client().get_text(_SEARCH_URL, params=params, headers=self._headers())
                items = json.loads(text).get("hojin-infos") or []
            except Exception as exc:
                log.info("gbiz_jp.search_error", slice=sid, page=page, err=str(exc))
                return hit[1] if hit is not None else None  # 부분 목록으로 커서를 밀지 않는다.
            rows.extend(
                {"corp_no": str(x.get("corporate_number") or ""), "name": str(x.get("name") or ""),
                 "address": str(x.get("location") or "")}
                for x in items if x.get("corporate_number") and x.get("name")
            )
            if len(items) < _PAGE:
                break
        else:
            log.warning("gbiz_jp.slice_overflow", slice=sid)  # 5만 초과 — 밴드/시 분할 필요 신호.
        with _lock:
            _list_cache[sid] = (now, rows)
        log.info("gbiz_jp.slice", slice=sid, rows=len(rows))
        return rows

    def _detail(self, corp_no: str) -> tuple[dict[str, str] | None, bool]:
        """상세(company_url·name_en·business_summary) → (정보, 호출 실패 여부). 24h 캐시·실패 1h."""
        now = time.monotonic()
        with _lock:
            hit = _detail_cache.get(corp_no)
            if hit is not None and now - hit[0] < (_DETAIL_TTL_S if hit[1] is not None else _NEG_TTL_S):
                return hit[1], False
        info: dict[str, str] | None = None
        failed = False
        try:
            text = self._client().get_text(_GBIZ_URL.format(corp_no=corp_no), headers=self._headers())
            items = json.loads(text).get("hojin-infos") or []
            first = items[0] if items else {}
            info = {
                "company_url": opt_str(first.get("company_url")) or "",
                "name_en": opt_str(first.get("name_en")) or "",
                "summary": opt_str(first.get("business_summary")) or "",
            }
        except Exception as exc:
            log.info("gbiz_jp.detail_error", corp_no=corp_no, err=str(exc))
            failed = True
        with _lock:
            prev = _detail_cache.get(corp_no)
            if failed and prev is not None and prev[1] is not None:
                return prev[1], failed
            _detail_cache[corp_no] = (now, info)
        return info, failed

    def _cursor(self, seg: Segment) -> int:
        if self._cursor_store is None:
            return 0
        pos = self._cursor_store.get(self.name, seg.label)
        return pos if pos > 0 else 0

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        cap = self._settings.discovery_max_per_source
        if cap <= 0:
            return []
        want: str | None = None
        if not is_broad_industry(segment.industry):
            want = resolve_industry_label(segment.industry)
        get_bytes = self._client().get_bytes
        listed = edinet_listed_corp_numbers(get_bytes)
        if not listed:
            # 제외 목록 없이 돌면 상장사가 비상장 스코프의 reg:gbiz 키로 새어 EDINET 행과 이중이 된다
            # (스코프값 오염 실사고 계열) — fail-closed(Codex 설계 채택). 다음 런에 재시도.
            log.warning("gbiz_jp.edinet_exclusion_unavailable", segment=segment.label)
            return []
        skip = listed | fsa_corp_numbers(get_bytes)  # 금융청 분은 best-effort(겹쳐도 도메인 dedup 백스톱).
        out: list[DiscoveredCompany] = []
        scanned = 0
        fail_streak = 0
        for cycle in (0, 1):
            exhausted_all = True
            for sid, pref, lo, hi in slices():
                if len(out) >= cap or scanned >= _MAX_SCAN or fail_streak >= _GBIZ_TRIP:
                    exhausted_all = False
                    break
                rows = self._slice_rows(sid, pref, lo, hi)
                if rows is None:
                    exhausted_all = False
                    continue
                cur_seg = segment.model_copy(update={"region": sid})  # 커서 키 전용.
                start = 0 if cycle else min(self._cursor(cur_seg), len(rows))
                pos = start
                for rec in rows[start:]:
                    if len(out) >= cap or scanned >= _MAX_SCAN or fail_streak >= _GBIZ_TRIP:
                        break
                    pos += 1
                    if rec["corp_no"] in skip:
                        continue  # EDINET 상장·금융청 등록사 — 다른 키로 이미 원장에 있음.
                    label = classify_jp(rec["name"])
                    if want is not None and label is not None and label != want:
                        continue  # 상호가 다른 업종을 말함 — 상세 콜 없이 건너뜀.
                    scanned += 1
                    info, failed = self._detail(rec["corp_no"])
                    fail_streak = fail_streak + 1 if failed else 0
                    if fail_streak >= _GBIZ_TRIP:
                        log.warning("gbiz_jp.detail_tripped", segment=segment.label)
                        pos -= 1  # 이 행은 처리 못 함 — 다음 런이 다시 본다.
                        break
                    if not info:
                        continue
                    if label is None:
                        label = classify_jp(rec["name"], info.get("summary"))
                    if label is None or (want is not None and label != want):
                        continue  # 분류 불가/불일치 — 세그먼트로 못 잡는 행은 원장에 안 쌓는다.
                    dom = _domain_of(info.get("company_url"))
                    if dom is None:
                        continue  # 도메인 없는 행은 미승격 부채만 됨(모듈 docstring).
                    eng = info.get("name_en") or ""
                    if eng and not _LATIN_NAME.match(eng):
                        eng = ""
                    out.append(
                        build_company(
                            source=self.name, segment=segment, name=eng or rec["name"], domain=dom,
                            registry="gbiz", registry_id=rec["corp_no"], industry_code_label=label,
                            address=opt_str(rec["address"]), reg_no=rec["corp_no"],
                            name_eng=rec["name"] if eng else None,
                        )
                    )
                if pos < len(rows):
                    exhausted_all = False
                if self._cursor_store is not None and pos != start:
                    self._cursor_store.advance(self.name, cur_seg.label, pos)
            if not exhausted_all or out or scanned or cycle:
                break
            log.info("gbiz_jp.cycle_reset", segment=segment.label)
        log.info("gbiz_jp.live", segment=segment.label, n=len(out), scanned=scanned)
        return out
