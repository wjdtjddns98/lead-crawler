"""발견 풍부필드(주소·지역·등록번호 등) — 통로·정규화·영속 테스트(네트워크 없음)."""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.region import region_from_address
from leadcrawler.schema import DiscoveredCompanyRow
from leadcrawler.sources.base import Segment, build_company, join_address, opt_str
from leadcrawler.sources.companieshouse import CompaniesHouseSource
from leadcrawler.sources.edgar import _business_address
from leadcrawler.sources.gleif import GleifSource
from leadcrawler.sources.opencorporates import OpenCorporatesSource
from leadcrawler.sources.wikidata import WikidataSource
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.repository import save_discovered


def test_region_from_address_kr_aliases() -> None:
    cases = {
        "부산광역시 해운대구 센텀중앙로 79": "부산",
        "서울특별시 서초구 서초대로74길 11": "서울",
        "서울시 강남구": "서울",
        "경기도 수원시 영통구 삼성로 129": "경기",
        "전북특별자치도 전주시": "전북",
        "전라북도 전주시": "전북",
        "세종특별자치시 한누리대로": "세종",
        "제주특별자치도 제주시": "제주",
    }
    for address, expected in cases.items():
        assert region_from_address("KR", address) == expected, address


def test_region_from_address_no_match_or_foreign() -> None:
    assert region_from_address("KR", None) is None
    assert region_from_address("KR", "알수없는 주소") is None
    # KR 외 국가는 주소 파싱하지 않는다(소스가 구조화 region 을 직접 전달).
    assert region_from_address("US", "400 Seoul Street, CA") is None


def test_build_company_passes_rich_fields_and_derives_region() -> None:
    seg = Segment(country="KR", industry="전체", listed="listed")
    dc = build_company(
        source="dart",
        segment=seg,
        name="테스트기업",
        registry="dart",
        registry_id="00000001",
        address="부산광역시 해운대구 센텀중앙로 79",
        reg_no="123-45-67890",
        ticker="005930",
        phone="02-1234-5678",
        ir_url="https://ir.example.com",
        name_eng="Test Corp",
    )
    assert dc.address == "부산광역시 해운대구 센텀중앙로 79"
    assert dc.region == "부산"  # 명시 region 없이 주소에서 파생.
    assert dc.reg_no == "123-45-67890"
    assert dc.ticker == "005930"
    assert dc.phone == "02-1234-5678"
    assert dc.ir_url == "https://ir.example.com"
    assert dc.name_eng == "Test Corp"


def test_build_company_explicit_region_wins() -> None:
    seg = Segment(country="GB", industry="전체", listed="unknown")
    dc = build_company(
        source="companies_house",
        segment=seg,
        name="UK Ltd",
        registry="companies_house",
        registry_id="01234567",
        address="1 Main Street, Manchester, M1 1AA",
        region="Manchester",
    )
    assert dc.region == "Manchester"


def test_save_discovered_persists_rich_fields(tmp_path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db", dry_run=True)
    init_db(settings)
    seg = Segment(country="KR", industry="전체", listed="listed")
    dc = build_company(
        source="dart",
        segment=seg,
        name="테스트기업",
        registry="dart",
        registry_id="00000001",
        address="부산광역시 해운대구",
        reg_no="123-45-67890",
        ticker="005930",
        phone="02-1234-5678",
        ir_url="https://ir.example.com",
        name_eng="Test Corp",
    )
    with session_scope(settings) as s:
        save_discovered(s, dc)
    # 별도 세션에서 커밋 반영 확인.
    with session_scope(settings) as s:
        row = s.get(DiscoveredCompanyRow, dc.canonical_key)
        assert row is not None
        assert row.address == "부산광역시 해운대구"
        assert row.region == "부산"
        assert row.reg_no == "123-45-67890"
        assert row.ticker == "005930"
        assert row.phone == "02-1234-5678"
        assert row.ir_url == "https://ir.example.com"
        assert row.name_eng == "Test Corp"


# --- 헬퍼 ----------------------------------------------------------------

def test_opt_str_and_join_address() -> None:
    assert opt_str("  x  ") == "x"
    assert opt_str("") is None
    assert opt_str("   ") is None
    assert opt_str(123) is None
    assert join_address("1 Main St", None, "", "London", "M1 1AA") == "1 Main St, London, M1 1AA"
    assert join_address(None, "") is None


# --- 소스별 파싱(네트워크 없음 — 응답 페이로드 직접 주입) ----------------

def test_gleif_candidate_parses_address_and_reg_no() -> None:
    src = GleifSource(Settings(dry_run=True))
    seg = Segment(country="KR", industry="전체")
    rec = {
        "id": "LEI123",
        "attributes": {
            "lei": "LEI123",
            "entity": {
                "status": "ACTIVE",
                "legalName": {"name": "글레이프 주식회사"},
                "legalAddress": {
                    "addressLines": ["Centum jungang-ro 79"],
                    "city": "Busan",
                    "postalCode": "48058",
                },
                "registeredAs": "110111-1234567",
            },
        },
    }
    dc = src._candidate(seg, rec)
    assert dc is not None
    assert dc.address == "Centum jungang-ro 79, Busan, 48058"
    assert dc.region == "Busan"
    assert dc.reg_no == "110111-1234567"


def test_companies_house_candidate_parses_address() -> None:
    src = CompaniesHouseSource(Settings(dry_run=True))
    seg = Segment(country="GB", industry="전체")
    item = {
        "company_status": "active",
        "company_number": "01234567",
        "company_name": "UK Ltd",
        "registered_office_address": {
            "address_line_1": "1 Main Street",
            "locality": "Manchester",
            "postal_code": "M1 1AA",
        },
    }
    dc = src._candidate(seg, item, None)
    assert dc is not None
    assert dc.address == "1 Main Street, Manchester, M1 1AA"
    assert dc.region == "Manchester"


def test_opencorporates_candidate_parses_address() -> None:
    src = OpenCorporatesSource(Settings(dry_run=True))
    seg = Segment(country="SG", industry="전체")
    wrapped = {
        "company": {
            "inactive": False,
            "company_number": "201800001A",
            "jurisdiction_code": "sg",
            "name": "SG Pte Ltd",
            "registered_address_in_full": "1 Raffles Place, Singapore 048616",
            "registered_address": {"locality": "Singapore"},
        }
    }
    dc = src._candidate(seg, wrapped)
    assert dc is not None
    assert dc.address == "1 Raffles Place, Singapore 048616"
    assert dc.region == "Singapore"


def test_wikidata_candidate_parses_hq_and_filters_unresolved_qid() -> None:
    src = WikidataSource(Settings(dry_run=True))
    seg = Segment(country="DE", industry="전체")
    row = {
        "item": {"value": "http://www.wikidata.org/entity/Q1"},
        "itemLabel": {"value": "German AG"},
        "hqLabel": {"value": "Munich"},
    }
    dc = src._candidate(seg, row, set())
    assert dc is not None and dc.region == "Munich"
    # 라벨 미해소(QID 그대로)면 region 제외 — 단 Quebec 같은 실제 지명은 유지.
    row2 = {
        "item": {"value": "http://www.wikidata.org/entity/Q2"},
        "itemLabel": {"value": "Canada Inc"},
        "hqLabel": {"value": "Q999999"},
    }
    dc2 = src._candidate(seg, row2, set())
    assert dc2 is not None and dc2.region is None
    row3 = {
        "item": {"value": "http://www.wikidata.org/entity/Q3"},
        "itemLabel": {"value": "Quebec Co"},
        "hqLabel": {"value": "Quebec City"},
    }
    dc3 = src._candidate(seg, row3, set())
    assert dc3 is not None and dc3.region == "Quebec City"


def test_edgar_business_address() -> None:
    address, region = _business_address(
        {
            "business": {
                "street1": "1 Apple Park Way",
                "city": "Cupertino",
                "stateOrCountry": "CA",
                "zipCode": "95014",
            }
        }
    )
    assert address == "1 Apple Park Way, Cupertino, CA, 95014"
    assert region == "CA"
    assert _business_address(None) == (None, None)
    assert _business_address({"business": "x"}) == (None, None)


def test_dart_live_parses_rich_fields() -> None:
    """DART 2-패스 응답 주입 — adres·bizr_no·stock_code·phn_no·ir_url·corp_name_eng 흡수."""
    import io
    import zipfile

    xml = (
        "<result><list><corp_code>00126380</corp_code>"
        "<corp_name>삼성전자</corp_name><stock_code>005930</stock_code></list></result>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml)

    class _Fake:
        def get_bytes(self, url, *, params=None, headers=None):
            return buf.getvalue()

        def get_json(self, url, *, params=None, headers=None):
            return {
                "status": "000",
                "corp_name": "삼성전자",
                "corp_name_eng": "Samsung Electronics",
                "corp_cls": "Y",
                "stock_code": "005930",
                "adres": "경기도 수원시 영통구 삼성로 129",
                "bizr_no": "124-81-00998",
                "phn_no": "031-200-1114",
                "ir_url": "https://www.samsung.com/ir",
                "hm_url": "www.samsung.com",
                "induty_code": "264",
            }

    from leadcrawler.sources.dart import DartSource

    src = DartSource(Settings(dry_run=False, dart_api_key="k"), fetcher=_Fake())
    out = src.discover(Segment(country="KR", industry="전체"))
    assert len(out) == 1
    dc = out[0]
    assert dc.address == "경기도 수원시 영통구 삼성로 129"
    assert dc.region == "경기"
    assert dc.reg_no == "124-81-00998"
    assert dc.ticker == "005930"
    assert dc.phone == "031-200-1114"
    assert dc.ir_url == "https://www.samsung.com/ir"
    assert dc.name_eng == "Samsung Electronics"
    assert dc.listed == "listed"
