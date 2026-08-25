"""FSC(금융위 금융회사기본정보) 발견 소스 테스트 — 무네트워크."""

from __future__ import annotations

from typing import Any

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.fsc import FscSource, _label_for, _redact_key


def _seg(industry: str = "증권·자산운용", country: str = "KR") -> Segment:
    return Segment(country=country, industry=industry)


def _settings(**kw: Any) -> Settings:
    return Settings(dry_run=True, **kw)


# --- 라벨 매핑 ---------------------------------------------------------------


def test_label_for_keyword_mapping() -> None:
    assert _label_for("타임폴리오자산운용") == "증권·자산운용"
    assert _label_for("마스턴투자운용") == "증권·자산운용"
    assert _label_for("대신증권 주식회사") == "증권·자산운용"
    # '투자자문' 은 택소노미 밖 — 규칙 없음(미분류→LLM 후속, 리뷰 HIGH 채택).
    assert _label_for("브이아이피투자자문") is None
    assert _label_for("주식회사 국민은행") == "은행"
    assert _label_for("삼성생명보험") == "보험"
    assert _label_for("한국교직원공제회") == "연기금"
    assert _label_for("신한카드") == "핀테크·결제"
    assert _label_for("모르는상호") is None


def test_label_for_first_match_priority() -> None:
    # 결합 사명 — 운용 키워드가 증권보다 먼저 잡힌다(규칙 순서).
    assert _label_for("한화증권자산운용") == "증권·자산운용"


# --- applies_to 게이팅 -------------------------------------------------------


def test_applies_to_kr_finance_only() -> None:
    src = FscSource(_settings())
    assert src.applies_to(_seg("증권·자산운용"))
    assert src.applies_to(_seg("은행"))
    assert src.applies_to(_seg("전체"))  # broad 허용.
    assert not src.applies_to(_seg("화학·석유화학"))  # 비금융 구체 업종 제외.
    assert not src.applies_to(_seg(country="US"))  # KR 전용.


# --- dry_run 계약 ------------------------------------------------------------


def test_dry_run_deterministic_registry_key() -> None:
    src = FscSource(_settings())
    got = src.discover(_seg())
    assert len(got) == 2
    assert got[0].canonical_key.startswith("reg:fsc:")
    assert got == src.discover(_seg())  # 결정성.


# --- 라이브 파싱(가짜 fetcher) ------------------------------------------------


class _SpyFetcher:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages
        self.calls: list[dict] = []

    def get_json(self, url: str, *, params: dict | None = None) -> dict:
        self.calls.append(dict(params or {}))
        idx = int((params or {}).get("pageNo", 1)) - 1
        if idx >= len(self._pages):
            return {"response": {"body": {"items": ""}}}
        return self._pages[idx]


def _envelope(items: list[dict] | dict) -> dict:
    return {"response": {"body": {"items": {"item": items}, "totalCount": 3}}}


_RECORDS = [
    {
        "fncoNm": "가나다자산운용",
        "crno": "1101110000001",
        "fncoHmpgUrl": "https://www.ganada-am.co.kr/main",
        "fncoAdr": "서울특별시 영등포구 여의대로 1",
        "fncoTelno": "02-100-0001",
    },
    {"fncoNm": "라마바은행", "crno": "1101110000002", "fncoHmpgUrl": "http://ramaba-bank.kr"},
    {"fncoNm": "이름없는회사"},  # 라벨 매핑 실패 → 구체 세그먼트에서 제외.
]


def _live_settings() -> Settings:
    return Settings(dry_run=False, data_go_kr_service_key="k")


def test_live_specific_segment_filters_and_maps() -> None:
    spy = _SpyFetcher([_envelope(_RECORDS)])
    src = FscSource(_live_settings(), fetcher=spy)
    got = src.discover(_seg("증권·자산운용"))
    assert [c.name for c in got] == ["가나다자산운용"]  # 은행·미매핑 제외(순도).
    c = got[0]
    assert c.canonical_key == "reg:fsc:1101110000001"
    assert c.domain == "ganada-am.co.kr"  # 홈페이지 URL → 도메인 정규화.
    assert c.industry == "증권·자산운용"
    assert c.phone == "02-100-0001"
    assert c.address is not None and "여의대로" in c.address


def test_live_broad_segment_keeps_all_with_code_labels() -> None:
    spy = _SpyFetcher([_envelope(_RECORDS)])
    src = FscSource(_live_settings(), fetcher=spy)
    got = src.discover(_seg("전체"))
    labels = {c.name: c.industry for c in got}
    assert labels["가나다자산운용"] == "증권·자산운용"
    assert labels["라마바은행"] == "은행"
    assert labels["이름없는회사"] == "미분류"  # 매핑 실패 → LLM 배치 후속.


def test_live_single_item_dict_envelope() -> None:
    # 표준 envelope 관례 — 단건은 item 이 dict 로 온다.
    spy = _SpyFetcher([_envelope(_RECORDS[0])])
    src = FscSource(_live_settings(), fetcher=spy)
    got = src.discover(_seg("증권·자산운용"))
    assert [c.name for c in got] == ["가나다자산운용"]


def test_live_no_key_is_noop() -> None:
    spy = _SpyFetcher([_envelope(_RECORDS)])
    src = FscSource(Settings(dry_run=False, data_go_kr_service_key=""), fetcher=spy)
    assert src.discover(_seg()) == []
    assert spy.calls == []  # 네트워크 0.


def test_live_fsc_key_alone_enables() -> None:
    # 전용 키만 있어도 동작(폴백 아님 — 전용 키가 1순위).
    spy = _SpyFetcher([_envelope(_RECORDS)])
    src = FscSource(
        Settings(dry_run=False, data_go_kr_service_key="", fsc_service_key="fk"), fetcher=spy
    )
    got = src.discover(_seg())
    assert got  # 발견됨
    assert spy.calls and spy.calls[0]["serviceKey"] == "fk"


def test_live_fsc_key_takes_precedence() -> None:
    # 두 키가 다 있으면 전용 키가 이긴다 — or 순서가 뒤집히면 실패.
    spy = _SpyFetcher([_envelope(_RECORDS)])
    src = FscSource(
        Settings(dry_run=False, data_go_kr_service_key="k", fsc_service_key="fk"), fetcher=spy
    )
    src.discover(_seg())
    assert spy.calls[0]["serviceKey"] == "fk"


class _Cursor:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, int]] = []
        self.start = 0

    def get(self, source: str, key: str) -> int:
        return self.start

    def advance(self, source: str, key: str, position: int) -> None:
        self.saved.append((source, key, position))


def test_live_cursor_resets_on_exhaustion() -> None:
    spy = _SpyFetcher([_envelope(_RECORDS)])  # 2페이지째는 빈 응답 → 소진.
    cur = _Cursor()
    src = FscSource(_live_settings(), fetcher=spy, cursor_store=cur)
    src.discover(_seg("전체"))
    assert cur.saved == [("fsc", "ALL", 0)]  # 소진 → 0 리셋(재검증 재개).


def test_live_error_keeps_partial(monkeypatch) -> None:
    class _Boom(_SpyFetcher):
        def get_json(self, url: str, *, params: dict | None = None) -> dict:
            if int((params or {}).get("pageNo", 1)) >= 2:
                raise RuntimeError("HTTP 403")
            return super().get_json(url, params=params)

    spy = _Boom([_envelope(_RECORDS)] * 3)
    cur = _Cursor()
    src = FscSource(_live_settings(), fetcher=spy, cursor_store=cur)
    got = src.discover(_seg("전체"))
    assert len(got) == 3  # 1페이지 분은 보존.
    assert cur.saved == [("fsc", "ALL", 2)]  # 실패 지점 커서 저장(재기동 이어받기).


def test_live_result_error_preserves_cursor() -> None:
    # HTTP 200 + resultCode≠00 — 소진 오인 커서 0 리셋 금지(현 위치 보존).
    err = {"response": {"header": {"resultCode": "22", "resultMsg": "LIMITED"}, "body": {}}}
    spy = _SpyFetcher([err])
    cur = _Cursor()
    src = FscSource(_live_settings(), fetcher=spy, cursor_store=cur)
    assert src.discover(_seg("전체")) == []
    assert cur.saved == [("fsc", "ALL", 1)]


def test_specific_segment_cursor_key_is_label() -> None:
    # 커서 키 = 업종 필터 단위 — 공유 키의 skip-forever 결함 회귀가드.
    spy = _SpyFetcher([_envelope(_RECORDS)])
    cur = _Cursor()
    src = FscSource(_live_settings(), fetcher=spy, cursor_store=cur)
    src.discover(_seg("은행"))
    assert cur.saved and cur.saved[0][1] == "은행"


def test_live_cap_truncates_and_advances_cursor() -> None:
    # cap 도달 시 페이지 중간 절단 — 커서는 page+1 저장(소진 리셋 사이클로 self-heal).
    spy = _SpyFetcher([_envelope(_RECORDS)] * 2)
    cur = _Cursor()
    src = FscSource(
        Settings(dry_run=False, data_go_kr_service_key="k", discovery_max_per_source=1),
        fetcher=spy,
        cursor_store=cur,
    )
    got = src.discover(_seg("전체"))
    assert len(got) == 1
    assert cur.saved == [("fsc", "ALL", 2)]


def test_live_phone_fallback_field() -> None:
    # fncoTelno 부재 시 fncoTlno 폴백.
    rec = {"fncoNm": "폴백은행", "crno": "1", "fncoTlno": "02-9"}
    spy = _SpyFetcher([_envelope([rec])])
    got = FscSource(_live_settings(), fetcher=spy).discover(_seg("은행"))
    assert got and got[0].phone == "02-9"


def test_live_no_crno_falls_back_to_name_key() -> None:
    # 법인등록번호 없는 레코드 — name 티어 canonical_key 폴백(제약① 안정성 고정).
    spy = _SpyFetcher([_envelope(_RECORDS)])
    got = FscSource(_live_settings(), fetcher=spy).discover(_seg("전체"))
    keyed = {c.name: c.canonical_key for c in got}
    assert keyed["이름없는회사"].startswith("name:")


def test_redact_key_masks_service_key() -> None:
    # httpx 예외 문자열의 전체 URL 에서 serviceKey 마스킹(보안 리뷰 HIGH — 키 로그 유출 차단).
    msg = "Client error '403' for url 'https://api?serviceKey=SEC123&pageNo=1'"
    assert "SEC123" not in _redact_key(msg, "SEC123")
    assert _redact_key(msg, "") == msg  # 빈 키 = 원문 유지(무키 no-op 경로).


def test_kr_whitelist_includes_fsc() -> None:
    # kr_discovery_nps_only 게이트에서도 FSC(공식 등록처)는 돌아야 한다.
    from leadcrawler.sources.registry import discover_segment

    s = Settings(dry_run=True, kr_discovery_nps_only=True)
    rows = discover_segment(_seg("전체"), s)
    assert "fsc" in {r.source for r in rows}
