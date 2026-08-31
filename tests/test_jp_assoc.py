"""JSDA·IMAJ 회원명부(일본 금융사) 발견 소스 테스트 — 무네트워크."""

from __future__ import annotations

from typing import Any

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.jp_assoc import _PAGES, JpAssocSource, parse_members


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


def test_live_labels_by_page_and_dedups_across_pages() -> None:
    kaiin, tokutei, tokubetu, imaj_im, imaj_adv = (u for u, _ in _PAGES)
    pages = {
        kaiin: _JSDA,
        tokutei: _ONE,
        tokubetu: '<main><a href="https://www.smbc.co.jp/">（株）三井住友銀行</a></main>',
        # IMAJ 운용회원에 JSDA 회원(buko)이 겸업 등재 → 첫 페이지(증권) 라벨 1건만.
        imaj_im: _IMAJ + '<main><a href="http://buko.co.jp/">武甲</a></main>',
        imaj_adv: _ONE,  # tokutei 와 같은 도메인 → 첫 등장만.
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
    assert not by_dom["buko.co.jp"].listed_verified
    # 5페이지 각 1회 fetch, 두 번째 세그먼트는 메모 재사용(추가 fetch 0).
    assert len(fetcher.calls) == 5
    assert src.discover(_seg("전체")) == got
    assert len(fetcher.calls) == 5


def test_live_specific_industry_fetches_only_matching_pages() -> None:
    tokubetu = _PAGES[2][0]
    fetcher = _FakeFetcher({tokubetu: '<main><a href="https://www.smbc.co.jp/">SMBC</a></main>'})
    src = JpAssocSource(_live_settings(), fetcher=fetcher)
    got = src.discover(_seg("은행"))
    assert [c.domain for c in got] == ["smbc.co.jp"]
    assert fetcher.calls == [tokubetu]  # 증권 페이지 4개는 안 건드림.


def test_live_partial_page_failure_is_atomic_and_keeps_cursor() -> None:
    """선택 페이지 중 하나만 실패해도 빈 결과 — 부분 풀로 커서를 전진시키면 다음 런이 회원을
    건너뛴다(로컬 슬라이스 커서는 풀 길이 기준)."""
    kaiin, tokutei = _PAGES[0][0], _PAGES[1][0]
    store: dict[tuple[str, str], int] = {}

    class _Store:
        def get(self, source: str, key: str) -> int:
            return store.get((source, key), 0)

        def advance(self, source: str, key: str, position: int) -> None:
            store[(source, key)] = position

    fetcher = _FakeFetcher({kaiin: _JSDA, tokutei: _ONE}, fail={tokutei})
    src = JpAssocSource(
        _live_settings(discovery_max_per_source=1), fetcher=fetcher, cursor_store=_Store()
    )
    assert src.discover(_seg("증권·자산운용")) == []
    assert store == {}  # 커서 미전진.
    assert fetcher.calls.count(kaiin) == 1  # 성공 페이지는 메모(재시도 시 재fetch 0).
    fetcher._fail.clear()
    got = src.discover(_seg("증권·자산운용"))
    assert [c.domain for c in got] == ["buko.co.jp"] and fetcher.calls.count(kaiin) == 1


def test_live_fetch_failure_is_not_memoized() -> None:
    tokubetu = _PAGES[2][0]
    fetcher = _FakeFetcher(
        {tokubetu: '<main><a href="https://www.smbc.co.jp/">SMBC</a></main>'}, fail={tokubetu}
    )
    src = JpAssocSource(_live_settings(), fetcher=fetcher)
    assert src.discover(_seg("은행")) == []
    fetcher._fail.clear()  # 네트워크 회복 → 다음 세그먼트에서 재시도해 성공.
    assert [c.domain for c in src.discover(_seg("은행"))] == ["smbc.co.jp"]


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
