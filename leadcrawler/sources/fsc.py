"""금융위원회 금융회사기본정보 발견 소스 — KR 금융회사 전수 등록처(공공데이터포털).

공공데이터포털 "금융위원회_금융회사기본정보"(GetFnCoBasiInfoService/getFnCoOutl)를
페이징 열거한다. 응답에 **홈페이지 URL(fncoHmpgUrl)** 이 포함돼 도메인 해석 없이
바로 승격 파이프라인을 태울 수 있다(2026-08-24 실측: KR 금융 세그먼트는 검색 기반
resolve 수율이 소진됨 — 등록처가 유일한 확대 경로).

- 인증: ``settings.fsc_service_key``(비면 ``data_go_kr_service_key`` 폴백 — 활용신청
  계정이 nps-sync 와 달라 분리) (데이터셋 활용신청 필요 — 미승인이면 403,
  이 소스는 로그만 남기고 빈 결과로 무해 종료한다).
- 업권 필터가 없어 전 금융회사(은행·보험·증권·운용 등)가 한 모집단으로 온다.
  세그먼트가 구체 업종이면 **사명 키워드 매핑으로 레코드를 필터**해 순도를 지킨다
  (라벨 규칙상 비-broad 세그먼트는 세그먼트 라벨이 그대로 구분값이 되므로, 필터
  없이 반환하면 은행이 '증권·자산운용'으로 오라벨되는 실사고 패턴 — GLEIF 2026-07-13).
- 커서: **업종 필터 단위 키**(구체 라벨 또는 'ALL')로 page 를 영속한다 — 공유 키 하나로
  하면 A 업종 런이 넘긴 페이지의 B 업종 레코드가 랩 전까지 영영 스킵된다(설계 교차검증).
  listed 파티션은 키에 안 넣는다(GLEIF 국가키와 동일 트레이드오프 — 같은 모집단을
  listed 값별로 재순회하지 않는 대신, 혼용 배치에선 커서를 공유한다).
- 이 API 가 말소/해산 법인을 포함하는지는 미확인(활용신청 승인 후 샘플로 확인 예정) —
  포함되더라도 승격 파이프라인의 실존 게이트(제약 ②)가 하류에서 걸러낸다.
- canonical_key = ``reg:fsc:<법인등록번호>`` (제약 ①). 법인등록번호 없으면 name 키 폴백.
- dry_run 은 네트워크 없이 결정적 더미(전 소스 계약).

ponytail: basDt/crno/fncoNm 조회 파라미터는 안 쓴다(전수 페이징이면 충분) —
증분 갱신이 필요해지면 basDt 를 추가.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import Settings
from ..dedup import normalize_domain
from ..logging import get_logger
from .base import (
    DiscoveredCompany,
    Segment,
    SupportsCursorStore,
    build_company,
    opt_str,
)
from .countries import resolve_country
from .http import Fetcher, HostRateLimiters, SupportsFetch
from .industry import is_broad_industry, resolve_industry_label

log = get_logger("sources.fsc")

_API_URL = "https://apis.data.go.kr/1160100/service/GetFnCoBasiInfoService/getFnCoOutl"
_PAGE = 100
# 예산 보호 가드 — 전수(수천 행)는 수십 페이지면 끝난다. 서버가 빈 페이지를 안 주는
# 이상 케이스에도 무한 순회하지 않도록 절대 상한(GLEIF 동일 패턴).
_MAX_PAGES = 60

# 사명 키워드 → 택소노미 라벨. 위에서부터 첫 매치 우선 — '~증권 자산운용' 같은 결합
# 사명은 더 구체적인 운용 키워드가 먼저 잡힌다. 금융회사 사명은 업권 표기가 상호에
# 강제되는 수준으로 정형적이라(자본시장법 등 업권별 상호 규제) 키워드 매핑의 정밀도가
# 높다. 매치 실패분은 None → broad 세그먼트에서 '미분류'(LLM 배치 후속), 구체
# 세그먼트에서는 제외(순도 우선).
_NAME_LABEL_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"자산운용|투자운용|리츠운용|투자신탁|자산관리운용"), "증권·자산운용"),
    (re.compile(r"증권|선물|투자증권|종합금융"), "증권·자산운용"),
    # '투자자문' 은 INDUSTRY_TAXONOMY(닫힌 집합) 밖 라벨이라 규칙을 두지 않는다 —
    # 큐 필터 드롭다운에 없어 영구 고아화(라벨 파편화 실사고 계열, 리뷰 HIGH). 자문사는
    # 매핑 실패(None)로 흘려 broad 세그먼트에서 '미분류'→LLM 배치 후속에 맡긴다.
    (re.compile(r"저축은행|은행"), "은행"),
    (re.compile(r"생명보험|손해보험|화재보험|보험|재보험"), "보험"),
    (re.compile(r"공제회|공제조합|연금공단"), "연기금"),
    (re.compile(r"카드|캐피탈|페이먼츠|전자지급|전자금융"), "핀테크·결제"),
]
# applies_to 게이트 — 이 소스가 순도 있게 공급 가능한 구체 업종 라벨 집합.
_FIN_LABELS = frozenset(rule_label for _, rule_label in _NAME_LABEL_RULES)


def _redact_key(text: str, key: str) -> str:
    """예외/URL 문자열에서 serviceKey 를 가린다 — httpx 예외가 전체 URL 을 포함해
    키가 로그로 새는 것을 차단(nps-sync '적대 리뷰 M4' 기확립 패턴 재사용)."""
    return text.replace(key, "***") if key else text


def _label_for(name: str) -> str | None:
    """금융회사 사명에서 택소노미 라벨을 정한다(첫 매치 우선, 실패 시 None)."""
    for pattern, label in _NAME_LABEL_RULES:
        if pattern.search(name):
            return label
    return None


class FscSource:
    """금융위 금융회사기본정보 기반 KR 금융회사 발견 소스(등록처 tier)."""

    name = "fsc"

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
        """KR 전용 + (broad 또는 금융 계열 구체 업종) 세그먼트에만 적용된다.

        비금융 구체 업종(화학 등)엔 공급할 게 없다 — 전 레코드가 필터에서 떨어져
        API 쿼터만 태우므로 게이트에서 자른다.
        """
        country = resolve_country(segment.country)
        if country is None or country.iso2 != "KR":
            return False
        if is_broad_industry(segment.industry):
            return True
        return resolve_industry_label(segment.industry) in _FIN_LABELS

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트에 해당하는 금융회사 목록을 반환한다."""
        if self._settings.dry_run:
            return self._dry(segment)
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(registry_id 기반 canonical_key, 전부 active 규약)."""
        return [
            build_company(
                source=self.name,
                segment=segment,
                name=f"{segment.industry} FSC Co {i}",
                domain=f"kr-fsc{i}.co.kr",
                registry="fsc",
                registry_id=f"FSC-{i}",
                industry_code_label=None,
            )
            for i in range(self._count)
        ]

    def _client(self) -> SupportsFetch:
        # 소스 인스턴스당 1개만 생성·재사용(discover 호출마다 클라이언트 누수 방지).
        if self._fetcher is None:
            self._fetcher = Fetcher(
                user_agent=self._settings.discovery_user_agent,
                min_interval=self._settings.http_request_delay,
                timeout=self._settings.http_timeout,
                rate_limiters=self._rate_limiters,
            )
        return self._fetcher

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """실 발견 — 전수 페이징 + 커서 + (구체 업종이면) 사명 키워드 필터."""
        key = self._settings.fsc_service_key.strip() or self._settings.data_go_kr_service_key.strip()
        if not key:  # 무키 → no-op(다른 유키 소스와 동일 관례).
            return []
        want: str | None = None  # None=broad(전 금융 레코드), str=이 라벨만.
        if not is_broad_industry(segment.industry):
            want = resolve_industry_label(segment.industry)
        fetcher = self._client()
        cap = self._settings.discovery_max_per_source

        out: list[DiscoveredCompany] = []
        ckey = want or "ALL"  # 커서 키 = 업종 필터 단위(모듈 독스트링 참조).
        page = 1
        if self._cursor_store is not None:
            stored = self._cursor_store.get(self.name, ckey)
            page = stored if stored > 0 else 1
        exhausted = False
        pages_done = 0
        while len(out) < cap and pages_done < _MAX_PAGES:
            params = {
                "serviceKey": key,
                "resultType": "json",
                "numOfRows": _PAGE,
                "pageNo": page,
            }
            try:
                payload = fetcher.get_json(_API_URL, params=params)
            except Exception as exc:  # 403(미승인)/쿼터/깨진 응답 → 부분 결과 보존 후 중단.
                log.info("fsc.error", page=page, err=_redact_key(str(exc), key))
                break
            code = self._result_code(payload)
            if code is not None and code != "00":
                # HTTP 200 + resultCode≠00 오류 응답 — 소진으로 오인해 커서를 0 리셋하면
                # 오류가 지속되는 동안 같은 앞머리만 재순회한다. 커서 보존 후 중단.
                log.info("fsc.result_error", page=page, code=code)
                break
            items = self._items(payload)
            if not items:
                exhausted = True  # 빈 페이지 = 모집단 끝 → 커서 0 리셋(재검증 재개).
                break
            for rec in items:
                dc = self._candidate(segment, rec, want)
                if dc is not None:
                    out.append(dc)
                    if len(out) >= cap:
                        break
            # ponytail: cap 도달로 페이지 중간에서 끊겨도 page+1 저장 — 그 페이지 잔여
            # 행은 이번 사이클엔 스킵되지만 소진→0 리셋 후 재스캔으로 self-heal(GLEIF/CH 동일).
            page += 1
            pages_done += 1
        if self._cursor_store is not None:
            if exhausted:
                log.info("fsc.cursor.exhausted", page=page)
            self._cursor_store.advance(self.name, ckey, 0 if exhausted else page)
        log.info("fsc.live", segment=segment.label, n=len(out), page=page)
        return out

    @staticmethod
    def _result_code(payload: Any) -> str | None:
        """data.go.kr 표준 envelope 의 header.resultCode(없으면 None)."""
        if not isinstance(payload, dict):
            return None
        resp = payload.get("response")
        header = resp.get("header") if isinstance(resp, dict) else None
        code = header.get("resultCode") if isinstance(header, dict) else None
        return str(code) if code is not None else None

    @staticmethod
    def _items(payload: Any) -> list[dict]:
        """data.go.kr 표준 envelope(response.body.items.item)에서 목록을 꺼낸다."""
        if not isinstance(payload, dict):
            return []
        body = (payload.get("response") or {}).get("body") if isinstance(
            payload.get("response"), dict
        ) else None
        if not isinstance(body, dict):
            return []
        items = body.get("items")
        if isinstance(items, dict):
            items = items.get("item")
        if isinstance(items, dict):  # 단건 응답은 dict 로 온다(표준 envelope 관례).
            items = [items]
        return [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []

    def _candidate(
        self, segment: Segment, rec: dict, want: str | None
    ) -> DiscoveredCompany | None:
        """레코드 1건을 후보로 변환한다(업종 불일치/형식 불일치는 제외)."""
        name = opt_str(rec.get("fncoNm"))
        if not name:
            return None
        label = _label_for(name)
        if want is not None and label != want:
            return None  # 구체 업종 세그먼트 — 사명 매핑이 일치하는 레코드만(순도 우선).
        raw_url = opt_str(rec.get("fncoHmpgUrl"))
        domain = normalize_domain(raw_url) if raw_url else None
        crno = opt_str(rec.get("crno"))
        return build_company(
            source=self.name,
            segment=segment,
            name=name,
            domain=domain,
            registry="fsc" if crno else None,
            registry_id=crno,
            industry_code_label=label,
            address=opt_str(rec.get("fncoAdr")),
            phone=opt_str(rec.get("fncoTelno") or rec.get("fncoTlno")),
        )
