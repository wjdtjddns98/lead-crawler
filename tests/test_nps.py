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
        "202606,소형화학상사,234567,1,경기도 수원시 화학길 2,,201234,화학제품 제조업,15,900000,",
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


def test_ingest_rows_accepts_api_shaped_dicts(tmp_path) -> None:
    """odcloud API JSON 행(한국어 키, 숫자값 비문자열)도 같은 인제스트를 탄다."""
    from leadcrawler.storage.nps import ingest_nps_rows

    s = _settings(tmp_path)
    init_db(s)
    sm = get_sessionmaker(s)
    rows = [
        {
            "자료생성년월": 202606, "사업장명": "API화학(주)", "사업자등록번호": "123456",
            "사업장가입상태코드": 1, "사업장도로명상세주소": "서울 강남구 1",
            "사업장지번상세주소": "", "사업장업종코드": 201234,
            "사업장업종코드명": "화학제품 제조업", "가입자수": 42, "당월고지금액": 1234567,
            "탈퇴일자": None,
        },
        {"사업장명": ""},  # 이름 없음 → skip.
        {"사업장명": "영세상회", "사업장업종코드": 201234, "가입자수": 9},  # 10인 미만 → skip.
    ]
    inserted, skipped = ingest_nps_rows(sm, iter(rows))
    assert (inserted, skipped) == (1, 2)
    row = NpsStore(sm).page(("20",), offset=0, limit=5)[0]
    assert row.name == "API화학(주)" and row.subscribers == 42 and row.industry_code == "201234"


def test_ingest_failure_keeps_active_snapshot(tmp_path) -> None:
    """소비 중 예외(API 5xx 등) → 활성 스냅샷 무손상(원자 스왑 — 리뷰 H2)."""
    from leadcrawler.storage.nps import ingest_nps_rows

    s = _settings(tmp_path)
    init_db(s)
    sm = get_sessionmaker(s)
    ingest_nps_csv(sm, _csv(tmp_path, _rows()))
    store = NpsStore(sm)
    assert store.count() == 4

    def _boom():
        yield {"사업장명": "새사업장", "사업장업종코드": "201234", "가입자수": "20"}
        raise RuntimeError("api down")

    try:
        ingest_nps_rows(sm, _boom())
    except RuntimeError:
        pass
    assert store.count() == 4  # 구 스냅샷 그대로(부분 적재분은 pending 격리).
    names = {r.name for r in store.page(("20",), offset=0, limit=10)}
    assert "새사업장" not in names  # pending 행은 조회에 안 나온다.

    # 다음 정상 인제스트가 잔재 pending 을 청소하고 교체한다.
    inserted, _ = ingest_nps_csv(sm, _csv(tmp_path, _rows()[:1]))
    assert inserted == 1 and store.count() == 1


def test_ingest_empty_keeps_active_snapshot(tmp_path) -> None:
    """0행 응답 → 스냅샷 교체 안 함(빈 API 응답이 데이터를 지우지 않게 — 리뷰 H2)."""
    from leadcrawler.storage.nps import ingest_nps_rows

    s = _settings(tmp_path)
    init_db(s)
    sm = get_sessionmaker(s)
    ingest_nps_csv(sm, _csv(tmp_path, _rows()))
    inserted, _ = ingest_nps_rows(sm, iter([]))
    assert inserted == 0 and NpsStore(sm).count() == 4


class FakeVerdict:
    def __init__(self, label):
        self.label = label


class FakeClassifier:
    """닫힌 택소노미 분류기 더블 — 코드명 키워드로 결정적 라벨."""

    model = "stub"

    def __init__(self):
        self.calls = 0

    def classify(self, name: str, domain, text):  # noqa: ARG002
        self.calls += 1
        if "게임" in name:
            return FakeVerdict("게임")
        return FakeVerdict(None)  # abstain → 미분류.


def test_map_industry_codes_rule_llm_and_idempotent(tmp_path) -> None:
    """3층 매핑: 규칙(industry_from_ksic) 우선, 잔여만 LLM, 재실행 멱등(리뷰 H2 반영)."""
    from leadcrawler.storage.nps import map_industry_codes

    s = _settings(tmp_path)
    init_db(s)
    sm = get_sessionmaker(s)
    rows = _rows() + [
        # 888888: 규칙 역매핑에 없는 코드 → LLM 경로(코드명에 '게임' → FakeClassifier 매칭).
        "202606,넥스트게임즈,345670,1,서울 강남구 게임로 1,,888888,게임 아이템 중개업,120,5000000,",
        "202606,알수없는업,345671,1,서울 중구 모호로 2,,999999,기타 분류 안된 업,30,100000,",
    ]
    ingest_nps_csv(sm, _csv(tmp_path, rows))
    clf = FakeClassifier()
    stats = map_industry_codes(sm, clf)
    # 화학·반도체 코드는 규칙으로, 888888(게임)은 LLM, 999999 는 abstain → 미분류.
    assert stats["rule"] >= 1 and stats["llm"] == 1 and stats["unclassified"] >= 1
    calls_first = clf.calls

    stats2 = map_industry_codes(sm, clf)
    assert stats2["rule"] == stats2["llm"] == stats2["unclassified"] == 0  # 전부 기매핑.
    assert clf.calls == calls_first  # 재실행 LLM 재호출 0(멱등).

    store = NpsStore(sm)
    assert "게임" in store.mapped_labels()
    hits = store.page_by_label("게임", offset=0, limit=10)
    assert [r.name for r in hits] == ["넥스트게임즈"]


def test_source_uses_label_path_for_unprefixed_industry(tmp_path) -> None:
    """KSIC 접두표에 없는 라벨(게임)도 3층 매핑이 있으면 발견된다(리뷰 HIGH-1 소비 경로)."""
    from leadcrawler.storage.nps import map_industry_codes

    s = Settings(
        database_url=f"sqlite:///{tmp_path}/nps.db", dry_run=False,
        discovery_max_per_source=10,
    )
    init_db(s)
    sm = get_sessionmaker(s)
    rows = _rows() + [
        "202606,넥스트게임즈,345670,1,서울 강남구 게임로 1,,888888,게임 아이템 중개업,120,5000000,",
    ]
    ingest_nps_csv(sm, _csv(tmp_path, rows))
    map_industry_codes(sm, FakeClassifier())

    src = NpsSource(s, nps_store=NpsStore(sm), cursor_store=DictCursor())
    seg = Segment(country="KR", industry="게임")
    assert src.applies_to(seg)  # 접두표엔 없지만 3층 매핑이 연다.
    out = src.discover(seg)
    assert [d.name for d in out] == ["넥스트게임즈"]


def test_dart_cache_join_attaches_fields_and_reg_key(tmp_path) -> None:
    """NPS×DART 조인: (사업자번호6+정규화명) 매치 시 홈페이지·reg: 키·상장 부착,
    미스는 기존(name 키·무도메인) 경로 그대로."""
    from leadcrawler.sources.dart import _FetchedCorp
    from leadcrawler.storage.dart_cache import DbDartCorpCache

    s = Settings(
        database_url=f"sqlite:///{tmp_path}/nps.db", dry_run=False,
        discovery_max_per_source=10,
    )
    init_db(s)
    sm = get_sessionmaker(s)
    rows = [
        # 대형화학(주) — 사업자번호 123456, DART 캐시에 동일 법인 존재(하단 put).
        "202606,대형화학(주),123456,1,서울특별시 강남구 화학로 1,,201234,화학제품 제조업,500,90000000,",
        "202606,무연고상사,777777,1,서울 중구 무연고로 2,,201234,화학제품 제조업,15,900000,",
    ]
    ingest_nps_csv(sm, _csv(tmp_path, rows))
    cache = DbDartCorpCache(sm)
    cache.put_many([
        _FetchedCorp(
            "00099999", "주식회사 대한화학", "000",
            {
                "status": "000", "corp_name": "주식회사 대형화학",  # (주) 표기 변형.
                "bizr_no": "1234567890", "hm_url": "http://bigchem.co.kr",
                "corp_cls": "K", "stock_code": "099999", "induty_code": "201234",
            },
        ),
    ])
    # put_many 가 name_norm 을 corp_name("주식회사 대형화학")에서 계산 — H1 픽스로
    # NPS 의 "대형화학(주)" 정규화와 같은 값이어야 매치된다.
    src = NpsSource(s, nps_store=NpsStore(sm), cursor_store=DictCursor(), dart_cache=cache)
    out = src.discover(Segment(country="KR", industry="화학·석유화학"))
    by_name = {d.name: d for d in out}
    hit = by_name["대형화학(주)"]
    assert hit.canonical_key.startswith("reg:dart:")  # DART 발견분과 완전 합치(제약①).
    assert hit.domain and "bigchem" in hit.domain
    assert hit.listed == "listed" and hit.market == "KOSDAQ"
    miss = by_name["무연고상사"]
    assert miss.domain is None and miss.canonical_key.startswith("name:")


def test_relink_cli_moves_name_key_to_reg_key(tmp_path, monkeypatch) -> None:
    """nps-relink-dart: 기존 name: 원장 행이 캐시 정밀 매치로 reg: 키에 재연결(리뷰 H1)."""
    from sqlalchemy import select as sa_select
    from typer.testing import CliRunner

    from leadcrawler import config as config_mod
    from leadcrawler.cli import app
    from leadcrawler.dedup import canonical_key
    from leadcrawler.schema import DiscoveredCompanyRow
    from leadcrawler.sources.dart import _FetchedCorp
    from leadcrawler.storage.dart_cache import DbDartCorpCache

    db_url = f"sqlite:///{tmp_path}/relink.db"
    s = Settings(database_url=db_url, dry_run=True)
    init_db(s)
    sm = get_sessionmaker(s)
    ingest_nps_csv(sm, _csv(tmp_path, _rows()[:1]))  # 대형화학(주), bizno 123456.
    DbDartCorpCache(sm).put_many([
        _FetchedCorp(
            "00012345", "주식회사 대형화학", "000",
            {"status": "000", "corp_name": "주식회사 대형화학",
             "bizr_no": "1234567890", "hm_url": "http://bigchem.co.kr"},
        ),
    ])
    old_key = canonical_key(name="대형화학(주)", country="KR")
    with sm() as session:
        session.add(
            DiscoveredCompanyRow(
                canonical_key=old_key, name="대형화학(주)", country="KR",
                industry="화학·석유화학", source="nps",
            )
        )
        session.commit()

    # CLI 는 get_settings()(캐시)를 쓰므로 env 주입 + 캐시 무효화로 이 DB 를 보게 한다.
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", db_url)
    config_mod.get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["nps-relink-dart"])
    finally:
        config_mod.get_settings.cache_clear()  # 다른 테스트 오염 방지.
    assert result.exit_code == 0, result.output
    with sm() as session:
        keys = list(session.scalars(sa_select(DiscoveredCompanyRow.canonical_key)))
    assert keys == ["reg:dart:00012345"]
