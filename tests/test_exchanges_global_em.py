"""신흥국 거래소 소스(B3/KAP/Tadawul/MOEX) 파싱 테스트 — 네트워크 0, FakeFetcher 주입."""

from __future__ import annotations

import base64
import json
from typing import Any

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.exchanges_global import B3Source, KapSource, MoexSource, TadawulSource


class FakeFetcher:
    """SupportsFetch 더블 — url/params 로 응답을 라우팅한다(tests/test_sources_live.py 관례)."""

    def __init__(self, *, json=None, text=None, post=None) -> None:  # noqa: A002
        self._json = json
        self._text = text
        self._post = post

    def get_json(self, url: str, *, params=None, headers=None) -> Any:
        return self._json(url, params or {})

    def get_text(self, url: str, *, params=None, headers=None,
                 allow_redirects=True, max_bytes=None) -> str:
        return self._text(url, params or {})

    def post_text(self, url: str, *, data=None, params=None, headers=None) -> str:
        return self._post(url, data or {})


def _b3_payload_from_url(url: str) -> dict[str, Any]:
    b64 = url.rsplit("/", 1)[-1]
    return json.loads(base64.b64decode(b64).decode())


# --- 결정적 dry_run(제약 ① 안정성) -----------------------------------------

def test_dry_run_registry_keyed_and_deterministic() -> None:
    settings = Settings(dry_run=True)
    seg = Segment(country="브라질", industry="금융")
    for cls, prefix in [
        (B3Source, "reg:b3:"), (KapSource, "reg:kap:"),
        (TadawulSource, "reg:tadawul:"), (MoexSource, "reg:moex:"),
    ]:
        rows = cls(settings).discover(seg)
        assert rows and all(r.canonical_key.startswith(prefix) for r in rows)
        assert all(r.listed == "listed" for r in rows)
        assert [r.canonical_key for r in rows] == [r.canonical_key for r in cls(settings).discover(seg)]


def test_applies_to_country_routing() -> None:
    settings = Settings(dry_run=True)
    # 상장 세그먼트는 업종 무관 적용(베이스 ExchangeSource.applies_to, PR-a 확정).
    br = Segment(country="브라질", industry="건설", listed="listed")
    tr = Segment(country="TR", industry="건설", listed="listed")
    assert B3Source(settings).applies_to(br)
    assert not B3Source(settings).applies_to(tr)
    assert KapSource(settings).applies_to(tr)
    assert not KapSource(settings).applies_to(br)
    # 비상장·구체 업종은 거래소 소스가 꺼진다(정밀도 우선, §3 기존 게이트 유지).
    assert not B3Source(settings).applies_to(
        Segment(country="브라질", industry="건설", listed="unlisted")
    )


# --- B3(브라질) -------------------------------------------------------------

def test_b3_live_filters_unlisted_date_and_confirms_via_detail() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    list_page = {
        "page": {"pageNumber": 1, "pageSize": 100, "totalRecords": 3, "totalPages": 1},
        "results": [
            {"codeCVM": "9512", "companyName": "PETROLEO BRASILEIRO S.A.",
             "dateListing": "20/07/1977", "status": "A"},
            {"codeCVM": "27359", "companyName": "AGUAS DO SERTAO S.A.",
             "dateListing": "09/02/2024", "status": "A"},
            # 미상장 성격(31/12/9999) → 상세콜 없이 1차에서 제외.
            {"codeCVM": "900049", "companyName": "1461 INVESTIMENTOS S.A",
             "dateListing": "31/12/9999", "status": "A"},
        ],
    }
    details = {
        "9512": {"companyName": "PETROLEO BRASILEIRO S.A. PETROBRAS",
                  "website": "www.petrobras.com.br", "hasQuotation": "S",
                  "code": "PETR4", "market": "BOVESPA NIVEL 2"},
        # hasQuotation "N" = 등록만 된 SPE 법인(실거래 아님) → 상장 미확정, 저장 안 함.
        "27359": {"companyName": "AGUAS DO SERTAO S.A.", "website": "",
                   "hasQuotation": "N", "code": None, "market": "BALCAO NAO ORG."},
    }
    seen_detail_codes: list[str] = []

    def _json(url: str, params: dict) -> Any:
        return list_page

    def _text(url: str, params: dict) -> str:
        code = _b3_payload_from_url(url)["codeCVM"]
        seen_detail_codes.append(code)
        return json.dumps(details[code])

    out = B3Source(settings, fetcher=FakeFetcher(json=_json, text=_text)).discover(
        Segment(country="브라질", industry="전체", listed="listed")
    )
    assert [d.registry_id for d in out] == ["9512"]
    assert sorted(seen_detail_codes) == ["27359", "9512"]  # 미상장 성격은 상세콜 자체가 없다.
    dc = out[0]
    assert dc.domain == "petrobras.com.br"
    assert dc.ticker == "PETR4"
    assert dc.market == "BOVESPA NIVEL 2"
    assert dc.registry == "b3" and dc.listed_verified
    assert dc.canonical_key == "reg:b3:9512"


def test_b3_live_detail_error_skips_row_not_crashes() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    list_page = {"page": {"totalPages": 1}, "results": [
        {"codeCVM": "1", "companyName": "X", "dateListing": "01/01/2020", "status": "A"},
    ]}

    def _boom_text(url: str, params: dict) -> str:
        raise RuntimeError("network down")

    out = B3Source(
        settings, fetcher=FakeFetcher(json=lambda u, p: list_page, text=_boom_text)
    ).discover(Segment(country="브라질", industry="전체", listed="listed"))
    assert out == []


def test_b3_live_pagination_stops_at_total_pages() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    calls = {"list": 0}

    def _json(url: str, params: dict) -> Any:
        calls["list"] += 1
        payload = _b3_payload_from_url(url)
        page_no = payload["pageNumber"]
        if page_no > 2:
            raise AssertionError("totalPages=2 를 넘어 페이지 요청함")
        return {
            "page": {"pageNumber": page_no, "totalPages": 2},
            "results": [{
                "codeCVM": f"{page_no}", "companyName": f"C{page_no}",
                "dateListing": "31/12/9999", "status": "A",  # 전부 미상장 성격 → 상세콜 0.
            }],
        }

    out = B3Source(settings, fetcher=FakeFetcher(json=_json, text=lambda u, p: "{}")).discover(
        Segment(country="브라질", industry="전체", listed="listed")
    )
    assert out == []
    assert calls["list"] == 2  # totalPages 를 넘겨 재요청하지 않는다.


# --- KAP(튀르키예) -----------------------------------------------------------

_KAP_ROW_HTML = (
    '<tr class="border-b hover:bg-light-danger">'
    '<td class="pl-4"><a href="/tr/sirket-bilgileri/ozet/1626-aciselsan-a-s">'
    '<div>ACSEL</div></a></td>'
    '<td class="pl-4"><a href="/tr/sirket-bilgileri/ozet/1626-aciselsan-a-s">'
    'ACISELSAN ACIPAYAM SELÜLOZ A.Ş.</a></td>'
    '<td class="pl-4">DENİZLİ</td>'
    '<td class="pl-4"><a href="/tr/sirket-bilgileri/ozet/109-drt-a-s">DRT A.Ş.</a></td>'
    "</tr>"
)
_KAP_LIST_HTML = "<html><body><table>" + _KAP_ROW_HTML + "</table></body></html>"
# 상세 페이지 flight 페이로드 — 라벨 다음에 값이 순서대로 오는 실측 구조(모듈 docstring 참조).
_KAP_DETAIL_HTML = (
    '...{\\"children\\":\\"İnternet Adresi\\"}],[\\"$\\",\\"p\\",null,'
    '{\\"children\\":\\"www.aciselsan.com.tr\\"}]]}]...'
    '{\\"children\\":\\"Sermaye Piyasası Aracının İşlem Gördüğü Pazar\\"}],'
    '[\\"$\\",\\"div\\",null,{\\"children\\":\\"ANA PAZAR\\"}]]}]...'
)


def test_kap_live_parses_table_and_detail_labels() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    out = KapSource(settings, fetcher=FakeFetcher(
        text=lambda u, p: _KAP_DETAIL_HTML if "sirket-bilgileri" in u else _KAP_LIST_HTML,
    )).discover(Segment(country="TR", industry="전체", listed="listed"))
    assert len(out) == 1
    dc = out[0]
    assert dc.registry_id == "1626"  # URL 의 숫자 id(mkkMemberOid 아님 — 모듈 docstring 참조).
    assert dc.ticker == "ACSEL"
    assert dc.name == "ACISELSAN ACIPAYAM SELÜLOZ A.Ş."
    assert dc.domain == "aciselsan.com.tr"
    assert dc.market == "ANA PAZAR"
    assert dc.canonical_key == "reg:kap:1626"


def test_kap_live_detail_error_keeps_row_without_domain() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)

    def _text(url: str, params: dict) -> str:
        if "sirket-bilgileri" in url:
            raise RuntimeError("detail down")
        return _KAP_LIST_HTML

    out = KapSource(settings, fetcher=FakeFetcher(text=_text)).discover(
        Segment(country="TR", industry="전체", listed="listed")
    )
    assert len(out) == 1
    assert out[0].domain is None
    assert out[0].market == "BIST"  # 상세 실패 시 기본 시장명 폴백.


def test_kap_live_list_error_returns_empty() -> None:
    settings = Settings(dry_run=False)

    def _boom(u: str, p: dict) -> str:
        raise RuntimeError("waf")

    out = KapSource(settings, fetcher=FakeFetcher(text=_boom)).discover(
        Segment(country="TR", industry="전체", listed="listed")
    )
    assert out == []


# --- Tadawul(사우디아라비아) --------------------------------------------------

def test_tadawul_impersonate_flag() -> None:
    assert TadawulSource.impersonate is True


def test_tadawul_live_parses_base_href_and_post_body() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    landing = '<html><head><base href="https://x.sa/wps/portal/.../action/"></head></html>'
    posted = {"data": [
        {"symbol": "4001", "lonaName": "Abdullah Al Othaim Markets Co.", "isinCode": "SA1"},
        {"symbol": "2082", "lonaName": "ACWA POWER Co.", "isinCode": "SA2"},
        {"symbol": "", "lonaName": "코드 없음"},  # symbol 없음 → 스킵.
    ]}
    seen_urls: list[str] = []

    def _text(url: str, params: dict) -> str:
        return landing

    def _post(url: str, data: dict) -> str:
        seen_urls.append(url)
        return json.dumps(posted)

    out = TadawulSource(settings, fetcher=FakeFetcher(text=_text, post=_post)).discover(
        Segment(country="사우디아라비아", industry="전체", listed="listed")
    )
    assert [d.registry_id for d in out] == ["4001", "2082"]
    assert out[0].name == "Abdullah Al Othaim Markets Co."
    assert out[0].domain is None  # Yahoo `.SR` 해석에 위임.
    assert out[0].market == "Tadawul Main Market"
    assert seen_urls[0].startswith("https://x.sa/wps/portal/.../action/p0/")


def test_tadawul_live_error_returns_empty() -> None:
    settings = Settings(dry_run=False)

    def _boom(u: str, p: dict) -> str:
        raise RuntimeError("akamai 403")

    out = TadawulSource(settings, fetcher=FakeFetcher(text=_boom)).discover(
        Segment(country="사우디아라비아", industry="전체", listed="listed")
    )
    assert out == []


# --- MOEX(러시아) ------------------------------------------------------------

def test_moex_live_filters_types_and_dedups_by_issuer() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    securities = {
        "securities": {
            "columns": ["secid", "emitent_id", "emitent_title", "type"],
            "data": [
                ["SBER", "3", "Сбербанк", "common_share"],
                ["SBERP", "3", "Сбербанк", "preferred_share"],  # 같은 발행자 → dedup.
                ["ABIO", "1142", "Артген", "common_share"],
                ["FXUS", "99", "ETF 발행자", "exchange_ppif"],  # 비주식 → 제외.
            ],
        }
    }
    emitters = {
        "3": {"emitter": {
            "columns": ["URL", "TRANSLITERATION_TITLE"],
            "data": [["https://www.sberbank.com/ru", "Sberbank PJSC"]],
        }},
        "1142": {"emitter": {
            "columns": ["URL", "TRANSLITERATION_TITLE"],
            "data": [["https://artgen.ru/", None]],
        }},
    }

    def _json(url: str, params: dict) -> Any:
        if "securities.json" in url:
            return securities if params.get("start") == 0 else {"securities": {"columns": [], "data": []}}
        emitent_id = url.rsplit("/", 1)[-1].split(".")[0]
        return emitters[emitent_id]

    out = MoexSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="러시아", industry="전체", listed="listed")
    )
    assert {d.registry_id for d in out} == {"3", "1142"}
    sber = next(d for d in out if d.registry_id == "3")
    assert sber.ticker == "SBER"  # 보통주 ticker 가 대표.
    assert sber.domain == "sberbank.com"
    assert sber.name == "Sberbank PJSC" and sber.name_eng == "Сбербанк"  # 영문 우선(KR 제외).
    artgen = next(d for d in out if d.registry_id == "1142")
    assert artgen.name == "Артген" and artgen.name_eng is None  # 음역 없으면 원어 유지.


def test_moex_live_emitter_error_keeps_row_without_domain() -> None:
    settings = Settings(dry_run=False, discovery_max_per_source=10)
    securities = {
        "securities": {
            "columns": ["secid", "emitent_id", "emitent_title", "type"],
            "data": [["ABIO", "1142", "Artgen", "common_share"]],
        }
    }

    def _json(url: str, params: dict) -> Any:
        if "securities.json" in url:
            return securities if params.get("start") == 0 else {"securities": {"columns": [], "data": []}}
        raise RuntimeError("emitter api down")

    out = MoexSource(settings, fetcher=FakeFetcher(json=_json)).discover(
        Segment(country="러시아", industry="전체", listed="listed")
    )
    assert len(out) == 1
    assert out[0].domain is None
    assert out[0].registry_id == "1142"
