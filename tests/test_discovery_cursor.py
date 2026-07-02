"""등록처 발견 커서(런 간 offset 영속, 딥백필) — storage 왕복 + 소스 이어스캔 테스트.

전부 네트워크 0: storage 는 in-process SQLite, 소스는 FakeFetcher + 딕셔너리 store.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.companieshouse import CompaniesHouseSource
from leadcrawler.sources.dart import DartSource
from leadcrawler.sources.edgar import EdgarSource
from leadcrawler.sources.registry import build_sources
from leadcrawler.storage.db import get_sessionmaker, init_db, session_scope
from leadcrawler.storage.discovery_cursor import DbCursorStore, get_cursor, set_cursor


class DictStore:
    """SupportsCursorStore 더블 — 메모리 딕셔너리."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], int] = {}

    def get(self, source: str, key: str) -> int:
        return self.data.get((source, key), 0)

    def advance(self, source: str, key: str, position: int) -> None:
        self.data[(source, key)] = position


class BoomStore:
    """어떤 호출이든 터지는 store — dry_run 이 커서를 안 만짐을 증명."""

    def get(self, source: str, key: str) -> int:
        raise AssertionError("dry_run 은 커서를 만지면 안 된다")

    def advance(self, source: str, key: str, position: int) -> None:
        raise AssertionError("dry_run 은 커서를 만지면 안 된다")


class FakeFetcher:
    """SupportsFetch 더블 — url/params 로 응답을 라우팅한다."""

    def __init__(self, *, json=None, data=None) -> None:
        self._json = json
        self._data = data

    def get_json(self, url: str, *, params=None, headers=None) -> Any:
        return self._json(url, params or {})

    def get_bytes(self, url: str, *, params=None, headers=None) -> bytes:
        return self._data(url, params or {})


# --- storage ---------------------------------------------------------------


def test_get_set_cursor_roundtrip_and_upsert(tmp_path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/c.db", dry_run=True)
    init_db(settings)
    with session_scope(settings) as s:
        assert get_cursor(s, "dart", "KR/전체/unknown") == 0  # 없으면 0.
        set_cursor(s, "dart", "KR/전체/unknown", 500)
        assert get_cursor(s, "dart", "KR/전체/unknown") == 500
        set_cursor(s, "dart", "KR/전체/unknown", 1000)  # 멱등 upsert(갱신).
        assert get_cursor(s, "dart", "KR/전체/unknown") == 1000
        assert get_cursor(s, "edgar", "KR/전체/unknown") == 0  # source 로 분리.


def test_db_cursor_store_roundtrip(tmp_path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/c.db", dry_run=True)
    init_db(settings)
    store = DbCursorStore(get_sessionmaker(settings))
    assert store.get("edgar", "US/전체/unknown") == 0
    store.advance("edgar", "US/전체/unknown", 7)
    assert store.get("edgar", "US/전체/unknown") == 7


def test_db_cursor_store_swallows_failures() -> None:
    def _boom() -> None:
        raise RuntimeError("db down")

    store = DbCursorStore(_boom)  # type: ignore[arg-type]
    assert store.get("dart", "k") == 0  # 읽기 실패 → 0 폴백.
    store.advance("dart", "k", 5)  # 쓰기 실패 → 무시(크롤 본체 보호).


# --- DART ------------------------------------------------------------------


def _corp_zip(n: int) -> bytes:
    """상장사 n 개짜리 corpCode.xml(ZIP) 더미."""
    rows = "".join(
        f"<list><corp_code>{i:08d}</corp_code><corp_name>회사{i}</corp_name>"
        f"<stock_code>{i:06d}</stock_code></list>"
        for i in range(1, n + 1)
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", f"<result>{rows}</result>".encode())
    return buf.getvalue()


def test_dart_two_runs_scan_disjoint_windows() -> None:
    settings = Settings(dry_run=False, dart_api_key="k", discovery_max_per_source=2)
    called: list[str] = []

    def _json(url: str, params: dict) -> Any:
        called.append(params["corp_code"])
        return {"status": "000", "corp_name": "X", "hm_url": "", "corp_cls": "Y"}

    store = DictStore()
    src = DartSource(
        settings,
        fetcher=FakeFetcher(json=_json, data=lambda u, p: _corp_zip(4)),
        cursor_store=store,
    )
    seg = Segment(country="KR", industry="전체")  # broad → 업종 필터 없음(scan_limit=cap).

    src.discover(seg)
    assert called == ["00000001", "00000002"]  # 1런: 앞 윈도우.
    assert store.get("dart", seg.label) == 2

    src.discover(seg)
    assert called[2:] == ["00000003", "00000004"]  # 2런: 다음 윈도우(겹침 없음).
    assert store.get("dart", seg.label) == 0  # 끝 도달 → 0 리셋(재검증 재개).


def test_dart_without_store_keeps_old_behavior() -> None:
    settings = Settings(dry_run=False, dart_api_key="k", discovery_max_per_source=2)
    called: list[str] = []

    def _json(url: str, params: dict) -> Any:
        called.append(params["corp_code"])
        return {"status": "000", "corp_name": "X", "corp_cls": "Y"}

    src = DartSource(settings, fetcher=FakeFetcher(json=_json, data=lambda u, p: _corp_zip(4)))
    src.discover(Segment(country="KR", industry="전체"))
    src.discover(Segment(country="KR", industry="전체"))
    # store 미주입 → 매 런 같은 머리(기존 동작, 회귀 0).
    assert called == ["00000001", "00000002", "00000001", "00000002"]


def test_dart_dry_run_never_touches_store() -> None:
    src = DartSource(Settings(dry_run=True), cursor_store=BoomStore())
    assert len(src.discover(Segment(country="KR", industry="전체"))) == 2


# --- EDGAR -----------------------------------------------------------------


def _tickers(n: int) -> dict:
    return {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[i, f"Corp {i}", f"T{i}", "Nasdaq"] for i in range(1, n + 1)],
    }


def test_edgar_two_runs_scan_disjoint_windows() -> None:
    settings = Settings(
        dry_run=False, edgar_user_agent="LeadCrawler test@example.com", discovery_max_per_source=2
    )
    called: list[int] = []

    def _json(url: str, params: dict) -> Any:
        if "company_tickers_exchange" in url:
            return _tickers(4)
        cik = int(url.split("CIK")[1].split(".")[0])
        called.append(cik)
        return {"name": f"Corp {cik}", "sic": "3571", "website": ""}

    store = DictStore()
    src = EdgarSource(settings, fetcher=FakeFetcher(json=_json), cursor_store=store)
    seg = Segment(country="US", industry="전체")

    src.discover(seg)
    assert called == [1, 2]
    assert store.get("edgar", seg.label) == 2

    src.discover(seg)
    assert called[2:] == [3, 4]
    assert store.get("edgar", seg.label) == 0  # 끝 도달 → 0 리셋.


def test_edgar_stale_cursor_beyond_universe_rewinds() -> None:
    settings = Settings(
        dry_run=False, edgar_user_agent="LeadCrawler test@example.com", discovery_max_per_source=2
    )
    called: list[int] = []

    def _json(url: str, params: dict) -> Any:
        if "company_tickers_exchange" in url:
            return _tickers(2)
        called.append(int(url.split("CIK")[1].split(".")[0]))
        return {"name": "X", "sic": "3571", "website": ""}

    store = DictStore()
    store.advance("edgar", "US/전체/unknown", 99)  # 모집단 축소 등으로 범위 밖.
    src = EdgarSource(settings, fetcher=FakeFetcher(json=_json), cursor_store=store)
    src.discover(Segment(country="US", industry="전체"))
    assert called == [1, 2]  # 0 으로 되감아 처음부터.


# --- Companies House --------------------------------------------------------


def _ch_item(i: int) -> dict:
    return {"company_status": "active", "company_number": f"GB{i:06d}", "company_name": f"Ltd {i}"}


def test_companies_house_continues_from_cursor_and_resets_on_exhaust() -> None:
    settings = Settings(dry_run=False, companies_house_api_key="k", discovery_max_per_source=2)
    starts: list[int] = []
    # 모집단 4건: start 0→2건, 2→2건, 4→빈 페이지(끝).
    pages = {0: [_ch_item(1), _ch_item(2)], 2: [_ch_item(3), _ch_item(4)], 4: []}

    def _json(url: str, params: dict) -> Any:
        starts.append(params["start_index"])
        return {"items": pages[params["start_index"]]}

    store = DictStore()
    src = CompaniesHouseSource(settings, fetcher=FakeFetcher(json=_json), cursor_store=store)
    seg = Segment(country="GB", industry="전체")

    out1 = src.discover(seg)
    assert [d.registry_id for d in out1] == ["GB000001", "GB000002"]
    assert store.get("companies_house", seg.label) == 2

    out2 = src.discover(seg)  # 2런: 이전 start_index 에서 이어 페이징.
    assert starts[1] == 2
    assert [d.registry_id for d in out2] == ["GB000003", "GB000004"]
    assert store.get("companies_house", seg.label) == 4

    assert src.discover(seg) == []  # 3런: 빈 페이지 = 모집단 끝.
    assert store.get("companies_house", seg.label) == 0  # 0 리셋(재검증 재개).


def test_companies_house_dry_run_never_touches_store() -> None:
    src = CompaniesHouseSource(Settings(dry_run=True), cursor_store=BoomStore())
    assert len(src.discover(Segment(country="GB", industry="전체"))) == 2


# --- 배선 --------------------------------------------------------------------


def test_build_sources_passes_cursor_store_to_registry_sources() -> None:
    store = DictStore()
    sources = build_sources(Settings(dry_run=True), cursor_store=store)
    wired = {
        s.name: s._cursor_store
        for s in sources
        if isinstance(s, (DartSource, EdgarSource, CompaniesHouseSource))
    }
    assert set(wired) == {"dart", "edgar", "companies_house"}
    assert all(v is store for v in wired.values())
