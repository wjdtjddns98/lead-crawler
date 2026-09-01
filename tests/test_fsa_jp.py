"""금융청(FSA) 등록·면허 일람 발견 소스 테스트 — 무네트워크."""

from __future__ import annotations

import io
import json
from typing import Any

import openpyxl
import pytest

from leadcrawler.config import Settings
from leadcrawler.sources import fsa_jp
from leadcrawler.sources.base import Segment
from leadcrawler.sources.fsa_jp import _LISTS, FsaJpSource, parse_list


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    fsa_jp._xlsx_cache.clear()
    fsa_jp._gbiz_cache.clear()


def _seg(industry: str = "전체", listed: str = "unknown", country: str = "JP") -> Segment:
    return Segment(country=country, industry=industry, listed=listed)


def _xlsx(sheets: dict[str, list[list[object]]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# 銀行免許 일람 축약(실측 헤더): 도시은행 시트 + 외국은행지점 시트(제외 대상).
_GINKOU = _xlsx({
    "都市銀行・信託銀行・その他": [
        ["銀行免許一覧（都市銀行・信託銀行・その他）"],
        ["業態", "所管", "銀行名", "法人番号", "郵便番号", "本店等所在地", "代表等電話番号"],
        ["都市銀行", "金融庁", "株式会社みずほ銀行", "6010001008845", "100-8176", "東京都千代田区", "03-3214-1111"],
        ["【小計】", None, None, None],
    ],
    "外国銀行支店": [
        ["業態", "所管", "本店所在地", "銀行名", "法人番号", "郵便番号", "在日支店", "代表等電話番号"],
        ["外国銀行支店", "金融庁", "米国", "外国銀行 東京支店", "7010001000001", "100", "東京都", "03-0000-0000"],
    ],
})

# 金融商品取引業者 일람 축약: 헤더 + 業務の種別 부헤더, 第二種 단독 행은 제외 대상.
_KINYU = _xlsx({
    "日本語": [
        ["金融商品取引業者登録一覧"],
        ["所管", "登録番号", "登録年月日", "金融商品取引業者名", "法人番号", "郵便番号", "本店等所在地",
         "代表等電話番号", "業務の種別", None, None, None],
        [None, None, None, None, None, None, None, None, "第一種", "第二種", "投資助言\n・\n代理業", "投資運用業"],
        ["金融庁", "関東財務局長(金商)第16号", "39355", "ＢＮＰパリバ・アセットマネジメント株式会社",
         "4010401061149", "1006742", "東京都千代田区丸の内", "03-6377-2800", "○", "○", "○", "○"],
        [None, "関東財務局長(金商)第18号", "39355", "いちご地所株式会社", "1010001125620", "1006920",
         "東京都千代田区", "03-4485-5230", "", "○", "", ""],
        [None, "関東財務局長(金商)第44号", "39355", "株式会社ＳＢＩ証券", "3010401049814", "1066019",
         "東京都港区六本木", "03-5562-7210", "○", "○", "○", None],
    ],
})


def test_parse_list_bank_skips_foreign_sheet_and_subtotals() -> None:
    rows = parse_list(_GINKOU, skip_sheet="外国銀行")
    assert rows == [{
        "name": "株式会社みずほ銀行", "corp_no": "6010001008845",
        "address": "東京都千代田区", "phone": "03-3214-1111",
    }]


def test_parse_list_securities_requires_first_advice_or_management_flag() -> None:
    rows = parse_list(_KINYU, sec_flags=True)
    assert [r["name"] for r in rows] == ["ＢＮＰパリバ・アセットマネジメント株式会社", "株式会社ＳＢＩ証券"]
    assert rows[1]["corp_no"] == "3010401049814" and rows[1]["phone"] == "03-5562-7210"


def test_parse_list_garbage_and_header_mismatch_are_empty() -> None:
    assert parse_list(b"not an xlsx") == []
    assert parse_list(_xlsx({"s": [["이름", "법인번호아님"], ["a", "b"]]})) == []


def test_applies_to_gating() -> None:
    src = FsaJpSource(Settings(dry_run=True))
    assert src.applies_to(_seg("전체")) and src.applies_to(_seg("은행"))
    assert src.applies_to(_seg("증권·자산운용", listed="unlisted"))
    assert not src.applies_to(_seg("전체", listed="listed"))  # 상장은 EDINET 전수.
    assert not src.applies_to(_seg("제약·바이오"))
    assert not src.applies_to(_seg("전체", country="KR"))


def test_dry_run_is_deterministic_and_registry_keyed() -> None:
    src = FsaJpSource(Settings(dry_run=True), count=2)
    got = src.discover(_seg("은행"))
    assert [c.canonical_key for c in got] == ["reg:fsa_jp:1000000000000", "reg:fsa_jp:1000000000001"]
    assert got == src.discover(_seg("은행"))


class _FakeFetcher:
    def __init__(self, blobs: dict[str, bytes], gbiz: dict[str, dict] | None = None,
                 fail: set[str] | None = None) -> None:
        self._blobs = blobs
        self._gbiz = gbiz or {}
        self._fail = fail or set()
        self.calls: list[str] = []
        self.gbiz_headers: list[dict] = []

    def get_bytes(self, url: str, **kw: Any) -> bytes:
        self.calls.append(url)
        if url in self._fail:
            raise TimeoutError("boom")
        return self._blobs.get(url, b"")

    def get_text(self, url: str, **kw: Any) -> str:
        self.calls.append(url)
        self.gbiz_headers.append(kw.get("headers") or {})
        corp = url.rsplit("/", 1)[-1]
        if corp in self._fail:
            raise TimeoutError("gbiz down")
        if corp in self._gbiz:
            return json.dumps({"hojin-infos": [self._gbiz[corp]]})
        return json.dumps({"hojin-infos": []})


def _live(**kw: Any) -> Settings:
    return Settings(dry_run=False, resolve_domains=False, **kw)


def test_live_without_token_enumerates_without_domain() -> None:
    ginkou, shinkin, kinyu = (u for u, _, _ in _LISTS)
    fetcher = _FakeFetcher({ginkou: _GINKOU, shinkin: _xlsx({"信用金庫": [
        ["業態", "所管", "都道府県", "名称", "法人番号", "郵便番号", "本店等所在地", "代表等電話番号"],
        ["信用金庫", "関東財務局", "東京都", "城南信用金庫", "9010405000001", "141", "東京都品川区", "03-1"],
    ]}), kinyu: _KINYU})
    got = FsaJpSource(_live(), fetcher=fetcher).discover(_seg("전체"))
    by_key = {c.canonical_key: c for c in got}
    assert set(by_key) == {
        "reg:fsa_jp:6010001008845", "reg:fsa_jp:9010405000001",
        "reg:fsa_jp:4010401061149", "reg:fsa_jp:3010401049814",
    }
    mizuho = by_key["reg:fsa_jp:6010001008845"]
    assert mizuho.industry == "은행" and mizuho.domain is None and mizuho.reg_no == "6010001008845"
    assert mizuho.phone == "03-3214-1111" and mizuho.name == "株式会社みずほ銀行"
    assert by_key["reg:fsa_jp:3010401049814"].industry == "증권·자산운용"
    assert not any(u.startswith("https://info.gbiz.go.jp") for u in fetcher.calls)  # 토큰 없음 → 미호출.
    assert mizuho.segment == "JP/전체/unknown"  # 커서용 파생 라벨이 새지 않음.


def test_live_with_token_attaches_domain_and_english_name() -> None:
    ginkou = _LISTS[0][0]
    fetcher = _FakeFetcher(
        {ginkou: _GINKOU},
        gbiz={"6010001008845": {"company_url": "https://www.mizuhobank.co.jp/index.html",
                                "name_en": "Mizuho Bank, Ltd."}},
    )
    src = FsaJpSource(_live(gbizinfo_api_token="tok"), fetcher=fetcher)
    got = src.discover(_seg("은행"))
    assert [c.domain for c in got] == ["mizuhobank.co.jp"]
    assert got[0].name == "Mizuho Bank, Ltd." and got[0].name_eng == "株式会社みずほ銀行"
    assert fetcher.gbiz_headers[0]["X-hojinInfo-api-token"] == "tok"
    # gBizINFO 결과는 프로세스 캐시 — 두 번째 런에 재조회 없음(일람은 캐시 적중, 신금 파일은 미제공→None).
    n = len(fetcher.calls)
    src.discover(_seg("은행"))
    assert [u for u in fetcher.calls[n:] if "gbiz" in u] == []


def test_live_gbiz_failure_is_negative_cached_and_non_latin_name_rejected() -> None:
    ginkou = _LISTS[0][0]
    fetcher = _FakeFetcher({ginkou: _GINKOU}, gbiz={"6010001008845": {"company_url": "", "name_en": "みずほ"}})
    got = FsaJpSource(_live(gbizinfo_api_token="tok"), fetcher=fetcher).discover(_seg("은행"))
    assert got[0].name == "株式会社みずほ銀行" and got[0].name_eng is None  # 비라틴 name_en 기각.

    fsa_jp._gbiz_cache.clear()
    fetcher2 = _FakeFetcher({ginkou: _GINKOU}, fail={"6010001008845"})
    src = FsaJpSource(_live(gbizinfo_api_token="tok"), fetcher=fetcher2)
    got = src.discover(_seg("은행"))
    assert got[0].domain is None and got[0].name == "株式会社みずほ銀行"  # 실패 → 도메인 없이 진행.
    n = len([u for u in fetcher2.calls if "gbiz" in u])
    src.discover(_seg("은행"))
    assert len([u for u in fetcher2.calls if "gbiz" in u]) == n  # 실패 1h 부정 캐시 — 재호출 없음.


def test_live_gbiz_consecutive_failures_trip_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """gBizINFO 전면 장애: 연속 실패 3회째부터 같은 discover() 안 나머지 행은 조회를 건너뛴다."""
    kinyu = _LISTS[2][0]
    rows = [["금융상품거래업자등록일람"],
            ["所管", "登録番号", "登録年月日", "金融商品取引業者名", "法人番号", "郵便番号", "本店等所在地",
             "代表等電話番号", "業務の種別", None, None, None],
            [None] * 8 + ["第一種", "第二種", "投資助言", "投資運用業"]]
    corps = [f"{1000000000000 + i:013d}" for i in range(10)]
    rows += [[None, "n", "d", f"会社{i}", c, "1", "住所", "03", "○", "", "", ""] for i, c in enumerate(corps)]
    fetcher = _FakeFetcher({kinyu: _xlsx({"日本語": rows})}, fail=set(corps))
    got = FsaJpSource(_live(gbizinfo_api_token="tok"), fetcher=fetcher).discover(_seg("증권·자산운용"))
    assert len(got) == 10  # 행은 전부 열거(도메인만 없음).
    assert len([u for u in fetcher.calls if "gbiz" in u]) == 3  # 3회 실패 후 차단.


def test_live_specific_industry_fetches_only_matching_lists() -> None:
    kinyu = _LISTS[2][0]
    fetcher = _FakeFetcher({kinyu: _KINYU})
    got = FsaJpSource(_live(), fetcher=fetcher).discover(_seg("증권·자산운용"))
    assert len(got) == 2 and fetcher.calls == [kinyu]


def test_live_list_failure_is_isolated_and_keeps_cursor() -> None:
    ginkou, shinkin, _ = (u for u, _, _ in _LISTS)
    store: dict[tuple[str, str], int] = {}

    class _Store:
        def get(self, source: str, key: str) -> int:
            return store.get((source, key), 0)

        def advance(self, source: str, key: str, position: int) -> None:
            store[(source, key)] = position

    fetcher = _FakeFetcher({ginkou: _GINKOU, shinkin: _xlsx({"信用金庫": [
        ["業態", "所管", "都道府県", "名称", "法人番号", "郵便番号", "本店等所在地", "代表等電話番号"],
        ["信用金庫", "関東", "東京都", "城南信用金庫", "9010405000001", "141", "東京都", "03-1"],
    ]})}, fail={ginkou})
    src = FsaJpSource(_live(), fetcher=fetcher, cursor_store=_Store())
    got = src.discover(_seg("은행"))
    assert [c.reg_no for c in got] == ["9010405000001"]  # 은행 파일 실패, 신금은 정상.
    assert ("fsa_jp", "JP/은행/unknown/list0") not in store  # 실패 파일 커서 미전진.
    assert store[("fsa_jp", "JP/은행/unknown/list1")] == 0  # 소진 → 0 리셋.


def test_live_cap_zero_and_cap_one() -> None:
    kinyu = _LISTS[2][0]
    fetcher = _FakeFetcher({kinyu: _KINYU})
    assert FsaJpSource(_live(discovery_max_per_source=0), fetcher=fetcher).discover(
        _seg("증권·자산운용")) == []
    assert len(FsaJpSource(_live(discovery_max_per_source=1), fetcher=fetcher).discover(
        _seg("증권·자산운용"))) == 1
