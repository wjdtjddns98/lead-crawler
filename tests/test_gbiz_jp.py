"""gBizINFO 전수 발견 소스(gbiz_jp)·일본 상호 업종 분류기(jp_industry) 테스트 — 무네트워크."""

from __future__ import annotations

import json
from typing import Any

import pytest

from leadcrawler.config import Settings
from leadcrawler.sources import fsa_jp, gbiz_jp
from leadcrawler.sources.base import Segment
from leadcrawler.sources.gbiz_jp import GbizJpSource, slices
from leadcrawler.sources.jp_industry import classify_jp, rule_labels
from leadcrawler.sources.taxonomy import INDUSTRY_TAXONOMY


@pytest.fixture(autouse=True)
def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    gbiz_jp._list_cache.clear()
    gbiz_jp._detail_cache.clear()
    fsa_jp._xlsx_cache.clear()
    fsa_jp._edinet_cache = None
    # 기본: EDINET 제외 목록은 "있음"(더미 1건), 금융청 제외는 없음 — 개별 테스트가 덮어쓴다.
    monkeypatch.setattr(gbiz_jp, "edinet_listed_corp_numbers", lambda gb: frozenset({"9999999999999"}))
    monkeypatch.setattr(gbiz_jp, "fsa_corp_numbers", lambda gb: frozenset())


def _seg(industry: str = "전체", listed: str = "unknown", country: str = "JP") -> Segment:
    return Segment(country=country, industry=industry, listed=listed)


# ---------------- classifier
def test_rule_labels_are_taxonomy_labels() -> None:
    assert all(lbl in INDUSTRY_TAXONOMY for lbl in rule_labels())


@pytest.mark.parametrize(
    ("name", "summary", "label"),
    [
        ("株式会社みずほ銀行", None, "은행"),
        ("大和証券株式会社", None, "증권·자산운용"),
        ("鹿島建設株式会社", None, "건설·엔지니어링"),
        ("ヤマト運輸株式会社", None, "물류·운송"),
        ("武田薬品工業株式会社", None, "제약·바이오"),  # 薬品 이 工業 보다 앞 규칙.
        ("三菱地所株式会社", None, "부동산·개발"),
        ("株式会社ＩＴソリューションズ", None, "IT·소프트웨어"),  # 전각 ＩＴ 정규화.
        ("株式会社ウエスト", None, None),  # 브랜드형 상호 — 못 정함.
        ("株式会社ウエスト", "鋼材卸売業", "철강·금속"),  # 요약이 보조 신호.
        ("株式会社ランドハウジング", "総合不動産業", "부동산·개발"),
        ("", "", None),
    ],
)
def test_classify_jp(name: str, summary: str | None, label: str | None) -> None:
    assert classify_jp(name, summary) == label


# ---------------- source
def test_slices_order_big_companies_and_big_prefectures_first() -> None:
    s = slices()
    assert s[0] == ("13-300-max", "13", 300, None) and s[1][1] == "27"
    assert len(s) == 47 * 5 and len({x[0] for x in s}) == len(s)


def test_applies_to_requires_token_and_gates() -> None:
    assert not GbizJpSource(Settings(dry_run=False, gbizinfo_api_token="")).applies_to(_seg("은행"))
    src = GbizJpSource(Settings(dry_run=False, gbizinfo_api_token="t"))
    assert src.applies_to(_seg("전체")) and src.applies_to(_seg("기계·산업장비"))
    assert not src.applies_to(_seg("전체", listed="listed"))
    assert not src.applies_to(_seg("전체", country="KR"))
    assert not src.applies_to(_seg("연기금"))  # 분류기가 못 내는 라벨.


def test_dry_run_deterministic() -> None:
    src = GbizJpSource(Settings(dry_run=True), count=2)
    got = src.discover(_seg("기타 제조"))
    assert [c.canonical_key for c in got] == ["reg:gbiz:2000000000000", "reg:gbiz:2000000000001"]


class _Fake:
    """검색은 슬라이스별 행 목록, 상세는 법인번호별 dict. Excel/EDINET 은 빈 바이트(제외 없음)."""

    def __init__(self, search: dict[str, list[dict]], detail: dict[str, dict], fail: set[str] | None = None) -> None:
        self.search = search
        self.detail = detail
        self.fail = fail or set()
        self.calls: list[str] = []

    def get_bytes(self, url: str, **kw: Any) -> bytes:
        self.calls.append(url)
        return b""

    def get_text(self, url: str, **kw: Any) -> str:
        self.calls.append(url)
        params = kw.get("params") or {}
        if url.endswith("/v2/hojin"):
            sid = f"{params['prefecture']}-{params['employee_number_from']}-{params.get('employee_number_to', 'max')}"
            rows = self.search.get(sid, [])
            page = int(params["page"])
            chunk = rows[(page - 1) * gbiz_jp._PAGE : page * gbiz_jp._PAGE]
            return json.dumps({"hojin-infos": chunk})
        corp = url.rsplit("/", 1)[-1]
        if corp in self.fail:
            raise TimeoutError("down")
        return json.dumps({"hojin-infos": [self.detail[corp]] if corp in self.detail else []})


def _row(corp: str, name: str) -> dict:
    return {"corporate_number": corp, "name": name, "location": "東京都"}


def _live(**kw: Any) -> Settings:
    return Settings(dry_run=False, resolve_domains=False, gbizinfo_api_token="tok", **kw)


def test_live_classifies_by_name_then_summary_and_emits_only_with_domain() -> None:
    fake = _Fake(
        search={"13-300-max": [
            _row("1000000000001", "山田建設株式会社"),      # 상호로 건설 → 상세 → URL 있음 → emit
            _row("1000000000002", "株式会社ウエスト"),       # 상호 불가 → 요약 '鋼材卸売業' → 철강 → URL 있음
            _row("1000000000003", "株式会社ノーネーム"),     # 상호·요약 모두 불가 → 버림
            _row("1000000000004", "株式会社サイト無し建設"),  # 건설이지만 URL 없음 → 버림
        ]},
        detail={
            "1000000000001": {"company_url": "https://www.yamada-kensetsu.co.jp/", "name_en": "Yamada Construction Co., Ltd."},
            "1000000000002": {"company_url": "http://west.co.jp", "business_summary": "鋼材卸売業"},
            "1000000000003": {"company_url": "http://noname.jp", "business_summary": ""},
            "1000000000004": {"company_url": ""},
        },
    )
    got = GbizJpSource(_live(), fetcher=fake).discover(_seg("전체"))
    by = {c.reg_no: c for c in got}
    assert set(by) == {"1000000000001", "1000000000002"}
    assert by["1000000000001"].name == "Yamada Construction Co., Ltd." and by["1000000000001"].name_eng == "山田建設株式会社"
    assert by["1000000000001"].industry == "건설·엔지니어링" and by["1000000000001"].canonical_key == "reg:gbiz:1000000000001"
    assert by["1000000000002"].industry == "철강·금속" and by["1000000000002"].domain == "west.co.jp"
    assert by["1000000000002"].segment == "JP/전체/unknown"  # 커서용 파생 라벨이 새지 않음.


def test_live_specific_industry_skips_detail_for_other_named_rows() -> None:
    fake = _Fake(
        search={"13-300-max": [_row("1000000000001", "山田建設株式会社"), _row("1000000000005", "東京運輸株式会社")]},
        detail={"1000000000001": {"company_url": "https://yamada.co.jp"}, "1000000000005": {"company_url": "https://unyu.co.jp"}},
    )
    got = GbizJpSource(_live(), fetcher=fake).discover(_seg("건설·엔지니어링"))
    assert [c.reg_no for c in got] == ["1000000000001"]
    assert not any(u.endswith("1000000000005") for u in fake.calls)  # 運輸 상호는 상세 콜 없이 스킵.


def test_live_excludes_edinet_and_fsa_corp_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gbiz_jp, "edinet_listed_corp_numbers", lambda gb: frozenset({"1000000000001"}))
    monkeypatch.setattr(gbiz_jp, "fsa_corp_numbers", lambda gb: frozenset({"1000000000002"}))
    fake = _Fake(
        search={"13-300-max": [_row("1000000000001", "山田建設株式会社"), _row("1000000000002", "東京証券株式会社"),
                               _row("1000000000003", "大阪運輸株式会社")]},
        detail={"1000000000003": {"company_url": "https://osaka-unyu.co.jp"}},
    )
    got = GbizJpSource(_live(), fetcher=fake).discover(_seg("전체"))
    assert [c.reg_no for c in got] == ["1000000000003"]


class _Store:
    def __init__(self) -> None:
        self.d: dict[tuple[str, str], int] = {}

    def get(self, source: str, key: str) -> int:
        return self.d.get((source, key), 0)

    def advance(self, source: str, key: str, position: int) -> None:
        self.d[(source, key)] = position


def test_live_cursor_holds_at_end_then_rewinds_when_all_exhausted() -> None:
    rows = [_row(f"{1000000000010 + i:013d}", f"会社{i}運輸") for i in range(4)]
    detail = {r["corporate_number"]: {"company_url": f"https://u{i}.co.jp"} for i, r in enumerate(rows)}
    fake = _Fake(search={"13-300-max": rows}, detail=detail)
    store = _Store()
    src = GbizJpSource(_live(discovery_max_per_source=3), fetcher=fake, cursor_store=store)
    assert len(src.discover(_seg("물류·운송"))) == 3
    assert store.d[("gbiz_jp", "JP/물류·운송/unknown/13-300-max")] == 3
    assert [c.reg_no for c in src.discover(_seg("물류·운송"))] == ["1000000000013"]  # 이어서 1건, 슬라이스 끝.
    assert store.d[("gbiz_jp", "JP/물류·운송/unknown/13-300-max")] == 4  # 끝에 머문다.


def test_live_detail_failures_trip_breaker_and_keep_cursor() -> None:
    rows = [_row(f"{1000000000020 + i:013d}", f"会社{i}運輸") for i in range(6)]
    fake = _Fake(search={"13-300-max": rows}, detail={}, fail={r["corporate_number"] for r in rows})
    store = _Store()
    got = GbizJpSource(_live(), fetcher=fake, cursor_store=store).discover(_seg("물류·운송"))
    assert got == []
    assert len([u for u in fake.calls if "/v1/hojin/" in u]) == 3  # 3회 실패 후 차단.
    assert store.d[("gbiz_jp", "JP/물류·운송/unknown/13-300-max")] == 2  # 실패한 3번째 행은 다음 런이 다시 본다.


def test_live_fails_closed_without_edinet_exclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    """EDINET 상장 제외 목록을 못 받으면 아무것도 emit 하지 않는다(상장사 누출 방지)."""
    monkeypatch.setattr(gbiz_jp, "edinet_listed_corp_numbers", lambda gb: frozenset())
    fake = _Fake(search={"13-300-max": [_row("1000000000001", "山田建設株式会社")]},
                 detail={"1000000000001": {"company_url": "https://yamada.co.jp"}})
    store = _Store()
    assert GbizJpSource(_live(), fetcher=fake, cursor_store=store).discover(_seg("전체")) == []
    assert store.d == {} and not any("/v1/hojin/" in u for u in fake.calls)


def test_live_search_failure_returns_nothing_and_keeps_cursor() -> None:
    class _Boom(_Fake):
        def get_text(self, url: str, **kw: Any) -> str:
            if url.endswith("/v2/hojin"):
                raise TimeoutError("search down")
            return super().get_text(url, **kw)

    store = _Store()
    assert GbizJpSource(_live(), fetcher=_Boom({}, {}), cursor_store=store).discover(_seg("전체")) == []
    assert store.d == {}
