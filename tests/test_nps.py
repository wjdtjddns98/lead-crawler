"""국민연금(NPS) 소스 — CSV 인제스트·스냅샷 조회·발견 계약(네트워크 0, SQLite)."""

from __future__ import annotations

from pathlib import Path

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.nps import NpsSource
from leadcrawler.storage.db import get_sessionmaker, init_db
from leadcrawler.storage.nps import NpsStore, ingest_nps_csv

_HEADER = (
    "자료생성년월,사업장명,사업자등록번호,사업장가입상태코드,사업장도로명상세주소,"
    "사업장지번상세주소,사업장업종코드,사업장업종코드명,가입자수,당월고지금액,탈퇴일자"
)


def _csv(path: Path, rows: list[str], encoding: str = "utf-8-sig") -> Path:
    p = path / "nps.csv"
    p.write_text("\n".join([_HEADER, *rows]), encoding=encoding)
    return p


def _settings(tmp_path: Path) -> Settings:
    return Settings(database_url=f"sqlite:///{tmp_path}/nps.db", dry_run=True)


def _rows() -> list[str]:
    # 화학(KSIC 20x → 코드 201234)·반도체(26x)·탈퇴 사업장 각 1 + 대형/소형 화학사.
    return [
        "202606,대형화학(주),123456,1,서울특별시 강남구 화학로 1,,201234,화학제품 제조업,500,90000000,",
        "202606,소형화학상사,234567,1,경기도 수원시 화학길 2,,201234,화학제품 제조업,5,900000,",
        "202606,반도체사,345678,1,,경기도 이천시 반도체길 3,261111,반도체 제조업,50,9000000,",
        "202606,탈퇴화학,456789,2,서울특별시 중구 옛길 4,,201234,화학제품 제조업,10,0,20250101",
    ]


def test_ingest_replaces_snapshot_and_counts(tmp_path) -> None:
    s = _settings(tmp_path)
    init_db(s)
    sm = get_sessionmaker(s)
    p = _csv(tmp_path, _rows())
    inserted, skipped = ingest_nps_csv(sm, p)
    assert (inserted, skipped) == (4, 0)
    # 재실행 = 스냅샷 교체(중복 누적 없음, 멱등).
    inserted2, _ = ingest_nps_csv(sm, p)
    assert inserted2 == 4
    assert NpsStore(sm).count() == 4


def test_ingest_cp949(tmp_path) -> None:
    s = _settings(tmp_path)
    init_db(s)
    sm = get_sessionmaker(s)
    p = _csv(tmp_path, _rows()[:1], encoding="cp949")
    inserted, _ = ingest_nps_csv(sm, p)
    assert inserted == 1
    row = NpsStore(sm).page(("20",), offset=0, limit=5)[0]
    assert row.name == "대형화학(주)" and row.subscribers == 500


def test_page_prefix_filter_size_order_and_resigned_excluded(tmp_path) -> None:
    s = _settings(tmp_path)
    init_db(s)
    sm = get_sessionmaker(s)
    ingest_nps_csv(sm, _csv(tmp_path, _rows()))
    store = NpsStore(sm)
    chem = store.page(("20",), offset=0, limit=10)
    names = [r.name for r in chem]
    assert names == ["대형화학(주)", "소형화학상사"]  # 가입자수 내림차순 + 탈퇴 제외.
    assert store.page(("26",), offset=0, limit=10)[0].name == "반도체사"
    assert store.page((), offset=0, limit=10) == []  # 접두 없음 = 빈 결과.


class DictCursor:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], int] = {}

    def get(self, source: str, key: str) -> int:
        return self.data.get((source, key), 0)

    def advance(self, source: str, key: str, position: int) -> None:
        self.data[(source, key)] = position


def test_source_discover_emits_with_cursor_and_wraps(tmp_path) -> None:
    s = Settings(
        database_url=f"sqlite:///{tmp_path}/nps.db", dry_run=False,
        discovery_max_per_source=1,
    )
    init_db(s)
    sm = get_sessionmaker(s)
    ingest_nps_csv(sm, _csv(tmp_path, _rows()))
    cursor = DictCursor()
    src = NpsSource(s, nps_store=NpsStore(sm), cursor_store=cursor)
    seg = Segment(country="KR", industry="화학·석유화학")
    assert src.applies_to(seg)

    out1 = src.discover(seg)
    assert [d.name for d in out1] == ["대형화학(주)"]  # 대형 우선.
    assert out1[0].source == "nps" and out1[0].address
    assert cursor.get("nps", seg.label) == 1  # cap 가득 → 전진.

    out2 = src.discover(seg)
    assert [d.name for d in out2] == ["소형화학상사"]
    assert cursor.get("nps", seg.label) == 2  # 정확히 cap 만큼 = 아직 소진 미판정.

    out3 = src.discover(seg)
    assert out3 == []  # 매칭 소진.
    assert cursor.get("nps", seg.label) == 0  # 빈 페이지(cap 미만) → 0 리셋(랩).


def test_source_gating_and_dry_run(tmp_path) -> None:
    live_no_store = NpsSource(
        Settings(database_url=f"sqlite:///{tmp_path}/x.db", dry_run=False)
    )
    seg = Segment(country="KR", industry="화학·석유화학")
    assert not live_no_store.applies_to(seg)  # 라이브 + 스토어 미주입 = 미적용.

    dry = NpsSource(Settings(dry_run=True))
    assert dry.applies_to(seg)
    assert not dry.applies_to(Segment(country="US", industry="화학·석유화학"))
    assert not dry.applies_to(Segment(country="KR", industry="전체"))  # broad 미적용.
    out1 = dry.discover(seg)
    out2 = dry.discover(seg)
    assert out1 and [d.canonical_key for d in out1] == [d.canonical_key for d in out2]
