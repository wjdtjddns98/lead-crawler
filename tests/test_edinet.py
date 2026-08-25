"""EDINET(일본 상장사 코드리스트) 발견 소스 테스트 — 무네트워크."""

from __future__ import annotations

import io
import zipfile
from typing import Any

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.edinet import (
    _SECTOR_TAXO,
    EdinetSource,
    _parse_codelist,
    _ticker,
)
from leadcrawler.sources.taxonomy import INDUSTRY_TAXONOMY


def _seg(industry: str = "전체", country: str = "JP") -> Segment:
    return Segment(country=country, industry=industry)


_HEADER = (
    "ＥＤＩＮＥＴコード,提出者種別,上場区分,連結の有無,資本金,決算日,提出者名,"
    "提出者名（英字）,提出者名（ヨミ）,所在地,提出者業種,証券コード,提出者法人番号"
)
_ROWS = [
    # 상장 내국법인 — 은행.
    'E00001,内国法人・組合,上場,有,100,3月31日,テスト銀行,Test Bank Inc.,テストギンコウ,'
    "東京都千代田区1-1,銀行業,13760,1234567890123",
    # 상장 내국법인 — 모호 업종(情報・通信業) → 미분류.
    'E00002,内国法人・組合,上場,有,50,3月31日,テスト通信,Test Telecom,テストツウシン,'
    "大阪府大阪市2-2,情報・通信業,130A0,2234567890123",
    # 비상장 — 제외.
    'E00003,内国法人・組合,非上場,無,10,3月31日,非上場商事,,ヒジョウジョウ,'
    "東京都港区3-3,卸売業,,3234567890123",
    # 외국법인 — 제외(국가 오분류 방지).
    'E00004,外国法人・組合,上場,無,10,12月31日,ガイコク HD,Foreign HD,,'
    ",銀行業,99999,",
]


def _zip_bytes(rows: list[str] | None = None, header: str = _HEADER) -> bytes:
    body = "メタ行,ダウンロード実行日,2026年08月25日現在\n" + header + "\n" + "\n".join(
        rows if rows is not None else _ROWS
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("EdinetcodeDlInfo.csv", body.encode("cp932"))
    return buf.getvalue()


class _FakeFetcher:
    def __init__(self, blob: bytes) -> None:
        self._blob = blob
        self.calls = 0

    def get_bytes(self, url: str, **kw: Any) -> bytes:
        self.calls += 1
        return self._blob


def _live_settings(**kw: Any) -> Settings:
    return Settings(dry_run=False, **kw)


# --- 매핑·파서 단위 -----------------------------------------------------------


def test_sector_taxonomy_labels_are_valid() -> None:
    # 매핑 값은 닫힌 택소노미 안에서만(라벨 파편화 방지) + 모호 업종은 None 고정.
    for v in _SECTOR_TAXO.values():
        assert v is None or v in INDUSTRY_TAXONOMY
    assert _SECTOR_TAXO["情報・通信業"] is None
    assert _SECTOR_TAXO["サービス業"] is None
    assert _SECTOR_TAXO["銀行業"] == "은행"


def test_ticker_strips_check_digit() -> None:
    assert _ticker("13760") == "1376"
    assert _ticker("130A0") == "130A"  # 영숫자 신형 코드.
    assert _ticker("") is None and _ticker(None) is None


def test_parse_codelist_skips_meta_row_and_cp932() -> None:
    rows = _parse_codelist(_zip_bytes())
    assert len(rows) == 4
    assert rows[0]["ＥＤＩＮＥＴコード"] == "E00001"
    assert rows[0]["提出者名"] == "テスト銀行"


def test_parse_codelist_broken_zip_graceful() -> None:
    assert _parse_codelist(b"not a zip") == []
    assert _parse_codelist(b"") == []


# --- applies_to 게이팅 --------------------------------------------------------


def test_applies_to_jp_only_and_mapped_industries() -> None:
    src = EdinetSource(Settings(dry_run=True))
    assert src.applies_to(_seg("전체"))
    assert src.applies_to(_seg("은행"))  # 매핑 가능 구체 업종.
    assert src.applies_to(Segment(country="일본", industry="전체"))  # 국가 별칭.
    assert not src.applies_to(_seg("게임"))  # 비매핑 구체 업종 — 왕복 낭비 차단.
    assert not src.applies_to(_seg(country="KR"))


# --- dry_run 계약 -------------------------------------------------------------


def test_dry_run_deterministic() -> None:
    src = EdinetSource(Settings(dry_run=True))
    got = src.discover(_seg())
    assert len(got) == 2
    assert got[0].canonical_key.startswith("reg:edinet:")
    assert got[0].listed == "listed"
    assert got == src.discover(_seg())  # 결정성.


# --- 라이브(가짜 fetcher) ------------------------------------------------------


def test_live_listed_domestic_only_and_mapping() -> None:
    spy = _FakeFetcher(_zip_bytes())
    src = EdinetSource(_live_settings(), fetcher=spy)
    got = src.discover(_seg("전체"))
    names = {c.name for c in got}
    assert names == {"テスト銀行", "テスト通信"}  # 비상장·외국법인 제외.
    bank = next(c for c in got if c.name == "テスト銀行")
    assert bank.canonical_key == "reg:edinet:e00001"
    assert bank.industry == "은행"
    assert bank.ticker == "1376"
    assert bank.reg_no == "1234567890123"
    assert bank.name_eng == "Test Bank Inc."
    assert bank.listed == "listed" and bank.listed_verified
    assert bank.domain is None
    telecom = next(c for c in got if c.name == "テスト通信")
    assert telecom.industry == "미분류"  # 모호 업종 → LLM 후속.


def test_live_specific_industry_filters() -> None:
    spy = _FakeFetcher(_zip_bytes())
    src = EdinetSource(_live_settings(), fetcher=spy)
    got = src.discover(_seg("은행"))
    assert [c.name for c in got] == ["テスト銀行"]


def test_live_fetch_once_memo() -> None:
    spy = _FakeFetcher(_zip_bytes())
    src = EdinetSource(_live_settings(), fetcher=spy)
    src.discover(_seg("전체"))
    src.discover(_seg("은행"))
    assert spy.calls == 1  # 런 내 재fetch 없음.


def test_live_fetch_error_graceful() -> None:
    class _Boom:
        def get_bytes(self, url: str, **kw: Any) -> bytes:
            raise RuntimeError("blocked")

    src = EdinetSource(_live_settings(), fetcher=_Boom())
    assert src.discover(_seg("전체")) == []


class _Cursor:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, int]] = []
        self.start = 0

    def get(self, source: str, key: str) -> int:
        return self.start

    def advance(self, source: str, key: str, position: int) -> None:
        self.saved.append((source, key, position))


def test_live_cursor_advance_and_reset() -> None:
    spy = _FakeFetcher(_zip_bytes())
    cur = _Cursor()
    src = EdinetSource(
        _live_settings(discovery_max_per_source=1), fetcher=spy, cursor_store=cur
    )
    src.discover(_seg("전체"))
    assert cur.saved and cur.saved[0][2] == 1  # cap 절단 지점 저장.
    cur2 = _Cursor()
    cur2.start = 1
    src2 = EdinetSource(_live_settings(), fetcher=_FakeFetcher(_zip_bytes()), cursor_store=cur2)
    got = src2.discover(_seg("전체"))
    assert [c.name for c in got] == ["テスト通信"]  # 이어읽기.
    assert cur2.saved[-1][2] == 0  # 소진 → 0 리셋.


def test_live_fetch_fail_then_recover() -> None:
    # 실패를 메모하면 런 전체가 침묵 0건(리뷰 HIGH) — 다음 discover 에서 재시도돼야 한다.
    class _FlakyFetcher:
        def __init__(self, blob: bytes) -> None:
            self._blob = blob
            self.calls = 0

        def get_bytes(self, url: str, **kw: Any) -> bytes:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("timeout")
            return self._blob

    spy = _FlakyFetcher(_zip_bytes())
    src = EdinetSource(_live_settings(), fetcher=spy)
    assert src.discover(_seg("전체")) == []  # 1회차 실패 → 빈 결과.
    got = src.discover(_seg("전체"))  # 2회차 재시도 성공.
    assert len(got) == 2 and spy.calls == 2


def test_parse_codelist_header_mismatch_returns_empty() -> None:
    # 메타 행 부재/헤더 개편 — 첫 데이터 행이 헤더로 소모되면 빈 결과(경고 로그, 침묵 0건 방지).
    body = _HEADER + "\n" + _ROWS[0]  # 메타 행 없음 → 헤더 자리에 데이터.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("EdinetcodeDlInfo.csv", body.encode("cp932"))
    assert _parse_codelist(buf.getvalue()) == []


def test_parse_codelist_prefers_edinetcode_member() -> None:
    # zip 멤버 순서가 아니라 Edinetcode* 본체를 고른다(리뷰 MED — README 류 오독 방지).
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("AAA_README.csv", "meta\nno,header\n1,2".encode("cp932"))
        zf.writestr(
            "EdinetcodeDlInfo.csv",
            ("メタ行,ダウンロード\n" + _HEADER + "\n" + _ROWS[0]).encode("cp932"),
        )
    rows = _parse_codelist(buf.getvalue())
    assert rows and rows[0]["ＥＤＩＮＥＴコード"] == "E00001"


def test_live_cap_zero_returns_empty() -> None:
    src = EdinetSource(
        _live_settings(discovery_max_per_source=0), fetcher=_FakeFetcher(_zip_bytes())
    )
    assert src.discover(_seg("전체")) == []
