"""국가 식별 — 세그먼트의 자유형식 국가 문자열 → ISO 3166-1 alpha-2 + Wikidata QID.

글로벌 집계원 소스(GLEIF/Wikidata)는 표준 코드로 질의하므로, 세그먼트 국가명을
정규화해야 한다. 여기 등록된 국가에만 해당 소스가 ``applies_to`` 로 적용되고,
미등록 국가는 검색 소스(SearchSource)로 폴백한다.

향후 '국가 세그먼트 제너레이터'(글로벌 발견 구동)의 단일 출처이기도 하다 —
크롤 대상 국가 목록을 이 레지스트리에서 우선순위 순으로 뽑는다.
"""

from __future__ import annotations

from pydantic import BaseModel


class Country(BaseModel):
    """표준 국가 식별자."""

    iso2: str  # ISO 3166-1 alpha-2 (GLEIF legalAddress.country 필터)
    qid: str  # Wikidata 국가 엔티티 QID (SPARQL P17 매칭)
    aliases: tuple[str, ...]  # 소문자 별칭(ISO2/ISO3/영문/한글)


# 우선순위 순(SEA 포함 — 필리핀·태국 등 사용자 요구 반영). QID 는 Wikidata 안정 식별자.
_COUNTRIES: tuple[Country, ...] = (
    Country(iso2="US", qid="Q30", aliases=("us", "usa", "united states", "america", "미국")),
    Country(iso2="KR", qid="Q884",
            aliases=("kr", "kor", "korea", "south korea", "대한민국", "한국")),
    Country(iso2="JP", qid="Q17", aliases=("jp", "jpn", "japan", "일본")),
    Country(iso2="CN", qid="Q148", aliases=("cn", "chn", "china", "중국")),
    Country(iso2="PH", qid="Q928", aliases=("ph", "phl", "philippines", "필리핀")),
    Country(iso2="TH", qid="Q869", aliases=("th", "tha", "thailand", "태국")),
    Country(iso2="ID", qid="Q252", aliases=("id", "idn", "indonesia", "인도네시아")),
    Country(iso2="MY", qid="Q833", aliases=("my", "mys", "malaysia", "말레이시아")),
    Country(iso2="SG", qid="Q334", aliases=("sg", "sgp", "singapore", "싱가포르")),
    Country(iso2="VN", qid="Q881", aliases=("vn", "vnm", "vietnam", "viet nam", "베트남")),
    Country(iso2="IN", qid="Q668", aliases=("in", "ind", "india", "인도")),
    Country(iso2="TW", qid="Q865", aliases=("tw", "twn", "taiwan", "대만")),
    Country(iso2="HK", qid="Q8646", aliases=("hk", "hkg", "hong kong", "홍콩")),
    Country(iso2="GB", qid="Q145",
            aliases=("gb", "uk", "gbr", "united kingdom", "britain", "영국")),
    Country(iso2="DE", qid="Q183", aliases=("de", "deu", "germany", "독일")),
    Country(iso2="FR", qid="Q142", aliases=("fr", "fra", "france", "프랑스")),
    Country(iso2="AU", qid="Q408", aliases=("au", "aus", "australia", "호주")),
    Country(iso2="CA", qid="Q16", aliases=("ca", "can", "canada", "캐나다")),
    Country(iso2="BR", qid="Q155", aliases=("br", "bra", "brazil", "브라질")),
)

# 별칭(소문자) → Country 역인덱스.
_INDEX: dict[str, Country] = {alias: c for c in _COUNTRIES for alias in c.aliases}


def resolve_country(name: str) -> Country | None:
    """세그먼트 국가 문자열을 표준 :class:`Country` 로 해석한다(미등록이면 None)."""
    return _INDEX.get((name or "").strip().lower())


def supported_countries() -> tuple[Country, ...]:
    """등록된 국가 목록(우선순위 순) — 국가 세그먼트 제너레이터의 출처."""
    return _COUNTRIES
