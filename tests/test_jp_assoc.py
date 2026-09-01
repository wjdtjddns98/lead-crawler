"""JSDA·IMAJ 회원명부(일본 금융사) 발견 소스 테스트 — 무네트워크."""

from __future__ import annotations

from typing import Any

import pytest

from leadcrawler.config import Settings
from leadcrawler.sources import jp_assoc
from leadcrawler.sources.base import Segment
from leadcrawler.sources.jp_assoc import _EN_PAGES, _PAGES, JpAssocSource, parse_members


@pytest.fixture(autouse=True)
def _clear_page_cache() -> None:
    jp_assoc._cache.clear()
    jp_assoc._neg.clear()


def _seg(industry: str = "전체", country: str = "JP", listed: str = "unknown") -> Segment:
    return Segment(country=country, industry=industry, listed=listed)


# JSDA 실측 마크업 축약(2026-08-31): 헤더 SNS 링크는 <main> 밖, 회원은 <main> 안 <li><a>.
_JSDA = """<html><body>
<header><a href="https://x.com/JSDAofficial" target="_blank"> </a>
<a href="https://j-flec.go.jp/">J-FLEC</a></header>
<main><h1>会員</h1><ul class="mod-c-ul">
<li><a href="http://buko.co.jp" target="_blank">武甲証券（株）</a><span class="icon_window"></span></li>
<li><a href="https://www.fujitomi.co.jp/" target="_blank">フジトミ証券（株）</a></li>
<li><a href="https://www.okb-sec.co.jp" target="_blank"></a></li>
<li><a href="https://www.jsda.or.jp/about/">協会について</a></li>
<li><a href="/kyoukaiin/">内部リンク</a></li>
</ul></main>
<footer><a href="https://off-exchange.jp/">取引所外</a></footer>
</body></html>"""

# IMAJ 실측 마크업 축약: 표 <th> 안 <a><span class=txt>상호</span><span>(別ウィンドウで開く)</span>.
_IMAJ = """<html><body><nav><a href="https://www.toushin.or.jp/statistics/">統計</a></nav>
<main><table><tbody>
<tr><th scope="row"><span class="txt">あいざわアセット</span></th><td>03-0000-0000</td></tr>
<tr><th scope="row"><a href="https://www.aizawa.co.jp/" class="txt-link"><span class="txt">
アイザワ証券株式会社</span><span class="link-icon">(別ウィンドウで開く)</span></a></th>
<td>03-6852-7711</td></tr>
<tr><th scope="row"><a href="http://www.ibjinc.com"><span class="txt">株式会社IBJ</span>
<span class="link-icon">(別ウィンドウで開く)</span></a></th><td></td></tr>
</tbody></table></main></body></html>"""


def test_parse_members_jsda_scopes_to_main_and_drops_internal() -> None:
    got = parse_members(_JSDA)
    # 헤더 SNS·J-FLEC·푸터·협회 내부 링크·상호 없는 앵커는 제외, www. 는 제거.
    assert got == [("武甲証券（株）", "buko.co.jp"), ("フジトミ証券（株）", "fujitomi.co.jp")]


def test_parse_members_imaj_strips_icon_text_and_skips_no_link_rows() -> None:
    got = parse_members(_IMAJ)
    assert got == [("アイザワ証券株式会社", "aizawa.co.jp"), ("株式会社IBJ", "ibjinc.com")]


def test_parse_members_strips_english_icon_suffix() -> None:
    html = ('<main><a href="https://www.aizawa.co.jp/"><span>Aizawa Securities Co., Ltd.</span>'
            '<span>(Open in new window)</span></a></main>')
    assert parse_members(html) == [("Aizawa Securities Co., Ltd.", "aizawa.co.jp")]


def test_live_prefers_official_english_name_from_en_list() -> None:
    """IMAJ 영문 명부와 도메인 매칭 → name=영문·name_eng=일문. 영문 목록 실패/미매칭은 일문."""
    imaj_im = _PAGES[3][0]
    en_im = _EN_PAGES[imaj_im]
    en_html = ('<main><a href="https://www.aizawa.co.jp/">Aizawa Securities Co., Ltd.'
               '(Open in new window)</a>'
               '<a href="http://www.ibjinc.com">株式会社IBJ(Open in new window)</a></main>')
    fetcher = _FakeFetcher({imaj_im: _IMAJ, en_im: en_html})
    src = JpAssocSource(_live_settings(), fetcher=fetcher)
    by_dom = {c.domain: c for c in src.discover(_seg("증권·자산운용"))}
    assert by_dom["aizawa.co.jp"].name == "Aizawa Securities Co., Ltd."
    assert by_dom["aizawa.co.jp"].name_eng == "アイザワ証券株式会社"
    # 영문 목록 값이 라틴이 아니면 기각 → 일문 유지, name_eng 없음.
    assert by_dom["ibjinc.com"].name == "株式会社IBJ" and by_dom["ibjinc.com"].name_eng is None

    jp_assoc._cache.clear()
    jp_assoc._neg.clear()
    fetcher2 = _FakeFetcher({imaj_im: _IMAJ}, fail={en_im})  # 영문 페이지 실패 → 일문으로 진행.
    got = JpAssocSource(_live_settings(), fetcher=fetcher2).discover(_seg("증권·자산운용"))
    assert {c.domain: c.name for c in got}["aizawa.co.jp"] == "アイザワ証券株式会社"


def test_parse_members_without_main_falls_back_to_whole_document() -> None:
    html = '<div><a href="https://example.co.jp/">例株式会社</a></div>'
    assert parse_members(html) == [("例株式会社", "example.co.jp")]


def test_parse_members_garbage_is_empty() -> None:
    assert parse_members("") == []
    assert parse_members("<<<not html") == []


def test_applies_to_gating() -> None:
    src = JpAssocSource(Settings(dry_run=True))
    assert src.applies_to(_seg("전체"))
    assert src.applies_to(_seg("은행"))
    assert src.applies_to(_seg("증권·자산운용", listed="unlisted"))
    assert not src.applies_to(_seg("제약·바이오"))  # 비매핑 업종은 왕복 낭비 → 게이트.
    assert not src.applies_to(_seg("전체", country="KR"))
    assert not src.applies_to(_seg("전체", listed="listed"))  # 상장은 EDINET 전수.


def test_dry_run_is_deterministic_and_domain_keyed() -> None:
    src = JpAssocSource(Settings(dry_run=True), count=2)
    got = src.discover(_seg("증권·자산운용"))
    assert [c.canonical_key for c in got] == ["dom:jp-assoc0.co.jp", "dom:jp-assoc1.co.jp"]
    assert all(c.source == "jp_assoc" and c.country == "JP" for c in got)
    assert got == src.discover(_seg("증권·자산운용"))


class _FakeFetcher:
    def __init__(self, pages: dict[str, str], fail: set[str] | None = None) -> None:
        self._pages = pages
        self._fail = fail or set()
        self.calls: list[str] = []

    def get_text(self, url: str, **kw: Any) -> str:
        self.calls.append(url)
        if url in self._fail:
            raise TimeoutError("boom")
        # 미지정 페이지는 회원 1건짜리 더미(0건 페이지는 원자적 실패로 취급되므로).
        n = abs(hash(url)) % 10**6
        return self._pages.get(url, f'<main><a href="https://d{n}.example.jp/">dummy</a></main>')


def _live_settings(**kw: Any) -> Settings:
    return Settings(dry_run=False, resolve_domains=False, **kw)


_ONE = '<main><a href="https://www.one.co.jp/">ワン</a></main>'


def test_live_labels_by_page_and_process_cache() -> None:
    kaiin, tokutei, tokubetu, imaj_im, imaj_adv = (u for u, _ in _PAGES)
    pages = {
        kaiin: _JSDA,
        tokutei: _ONE,
        tokubetu: '<main><a href="https://www.smbc.co.jp/">（株）三井住友銀行</a></main>',
        imaj_im: _IMAJ,
        imaj_adv: _ONE,
    }
    fetcher = _FakeFetcher(pages)
    src = JpAssocSource(_live_settings(), fetcher=fetcher)

    got = src.discover(_seg("전체"))
    by_dom = {c.domain: c for c in got}
    assert set(by_dom) == {
        "buko.co.jp", "fujitomi.co.jp", "one.co.jp", "smbc.co.jp", "aizawa.co.jp", "ibjinc.com",
    }
    assert by_dom["smbc.co.jp"].industry == "은행"
    assert by_dom["aizawa.co.jp"].industry == "증권·자산운용"
    assert by_dom["buko.co.jp"].canonical_key == "dom:buko.co.jp"
    assert by_dom["buko.co.jp"].name == "武甲証券（株）"
    assert not by_dom["buko.co.jp"].listed_verified
    assert by_dom["buko.co.jp"].segment == "JP/전체/unknown"  # 커서용 파생 라벨이 새지 않음.
    # 일문 5면 + IMAJ 영문 2면 = 7 fetch. 같은 프로세스의 **다른 인스턴스**(병렬 워커)도 캐시
    # 적중 → 추가 0.
    assert len(fetcher.calls) == 7
    assert JpAssocSource(_live_settings(), fetcher=fetcher).discover(_seg("전체")) == got
    assert len(fetcher.calls) == 7


def test_live_specific_industry_fetches_only_matching_pages() -> None:
    tokubetu = _PAGES[2][0]
    fetcher = _FakeFetcher({tokubetu: '<main><a href="https://www.smbc.co.jp/">SMBC</a></main>'})
    src = JpAssocSource(_live_settings(), fetcher=fetcher)
    got = src.discover(_seg("은행"))
    assert [c.domain for c in got] == ["smbc.co.jp"]
    assert fetcher.calls == [tokubetu]  # 증권 페이지 4개는 안 건드림.


def test_live_page_failure_is_isolated_and_keeps_that_pages_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """한 페이지 실패는 그 페이지만 결과 0·커서 유지 — 다른 페이지는 정상 진행(페이지별 커서라
    부분 풀로 커서가 밀리는 문제가 없다). JSDA 만 IP 차단돼도 IMAJ 분은 계속 나온다."""
    kaiin, tokutei = _PAGES[0][0], _PAGES[1][0]
    store: dict[tuple[str, str], int] = {}

    class _Store:
        def get(self, source: str, key: str) -> int:
            return store.get((source, key), 0)

        def advance(self, source: str, key: str, position: int) -> None:
            store[(source, key)] = position

    fetcher = _FakeFetcher({kaiin: _JSDA, tokutei: _ONE}, fail={kaiin})
    src = JpAssocSource(_live_settings(), fetcher=fetcher, cursor_store=_Store())
    got = src.discover(_seg("증권·자산운용"))
    assert "one.co.jp" in {c.domain for c in got}  # tokutei·IMAJ 더미는 정상.
    assert ("jp_assoc", "JP/증권·자산운용/unknown/page0") not in store  # 실패 페이지 커서 미전진.
    assert store[("jp_assoc", "JP/증권·자산운용/unknown/page1")] == 0  # 소진 → 0 리셋(기존 규약).
    fetcher._fail.clear()  # 차단 해제 → 다음 세그먼트에서 kaiin 회복, 나머지는 캐시.
    monkeypatch.setattr(jp_assoc, "_NEG_TTL_S", 0)  # 부정 캐시 창 경과.
    n_before = len(fetcher.calls)
    got2 = src.discover(_seg("증권·자산운용"))
    assert {"buko.co.jp", "fujitomi.co.jp"} <= {c.domain for c in got2}
    assert fetcher.calls[n_before:] == [kaiin]


def test_live_failure_is_negative_cached_then_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """실패는 _NEG_TTL_S 동안 재요청하지 않는다(차단 호스트 재타격 방지) — 창이 지나면 재시도."""
    tokubetu = _PAGES[2][0]
    fetcher = _FakeFetcher(
        {tokubetu: '<main><a href="https://www.smbc.co.jp/">SMBC</a></main>'}, fail={tokubetu}
    )
    src = JpAssocSource(_live_settings(), fetcher=fetcher)
    assert src.discover(_seg("은행")) == []
    fetcher._fail.clear()
    assert src.discover(_seg("은행")) == []  # 부정 캐시 창 안 — 재fetch 없음.
    assert fetcher.calls.count(tokubetu) == 1
    monkeypatch.setattr(jp_assoc, "_NEG_TTL_S", 0)  # 창 경과 시뮬레이션.
    assert [c.domain for c in src.discover(_seg("은행"))] == ["smbc.co.jp"]
    assert fetcher.calls.count(tokubetu) == 2


def test_live_stale_if_error_serves_expired_success(monkeypatch: pytest.MonkeyPatch) -> None:
    tokubetu = _PAGES[2][0]
    fetcher = _FakeFetcher({tokubetu: '<main><a href="https://www.smbc.co.jp/">SMBC</a></main>'})
    src = JpAssocSource(_live_settings(), fetcher=fetcher)
    assert [c.domain for c in src.discover(_seg("은행"))] == ["smbc.co.jp"]
    monkeypatch.setattr(jp_assoc, "_CACHE_TTL_S", 0)  # 성공값 만료.
    fetcher._fail.add(tokubetu)  # 재fetch 실패 → 만료된 성공값으로 응답.
    assert [c.domain for c in src.discover(_seg("은행"))] == ["smbc.co.jp"]
    assert fetcher.calls.count(tokubetu) == 2


def test_parse_members_scheme_is_case_insensitive() -> None:
    assert parse_members('<main><a href="HTTPS://Www.Ex.co.jp/">X</a></main>') == [("X", "ex.co.jp")]


def test_live_respects_cap_and_zero_cap() -> None:
    kaiin = _PAGES[0][0]
    fetcher = _FakeFetcher({kaiin: _JSDA})
    assert JpAssocSource(_live_settings(discovery_max_per_source=1), fetcher=fetcher).discover(
        _seg("증권·자산운용")
    ) == JpAssocSource(_live_settings(discovery_max_per_source=1), fetcher=fetcher).discover(
        _seg("증권·자산운용")
    )
    assert len(
        JpAssocSource(_live_settings(discovery_max_per_source=1), fetcher=fetcher).discover(
            _seg("증권·자산운용")
        )
    ) == 1
    assert JpAssocSource(_live_settings(discovery_max_per_source=0), fetcher=fetcher).discover(
        _seg("증권·자산운용")
    ) == []
