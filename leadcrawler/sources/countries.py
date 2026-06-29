"""국가 식별 — 세그먼트의 자유형식 국가 문자열 → ISO 3166-1 alpha-2 + Wikidata QID.

글로벌 집계원 소스(GLEIF/Wikidata)는 표준 코드로 질의하므로, 세그먼트 국가명을
정규화해야 한다. 여기 등록된 국가에만 해당 소스가 ``applies_to`` 로 적용되고,
미등록 국가는 검색 소스(SearchSource)로 폴백한다.

향후 '국가 세그먼트 제너레이터'(글로벌 발견 구동)의 단일 출처이기도 하다 —
크롤 대상 국가 목록을 이 레지스트리에서 우선순위 순으로 뽑는다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel

_HANGUL = re.compile(r"[가-힣]")


class Country(BaseModel):
    """표준 국가 식별자."""

    iso2: str  # ISO 3166-1 alpha-2 (GLEIF legalAddress.country 필터)
    qid: str  # Wikidata 국가 엔티티 QID (SPARQL P17 매칭)
    aliases: tuple[str, ...]  # 소문자 별칭(ISO2/ISO3/영문/한글)


# 우선순위 순(SEA 포함 — 필리핀·태국 등 사용자 요구 반영). QID 는 Wikidata 안정 식별자.
# 별칭은 ISO2/ISO3 + 영문(축약·공식명) + 현지/한글 표기를 폭넓게 담는다 — import 엑셀의
# 자유표기('Republic of Korea', 'Deutschland', '中国' 등)와 크롤 세그먼트(ISO2)가 같은
# canonical_key 로 수렴하도록(제약①). 별칭은 전부 소문자·중복 없음(아래 _INDEX 무결성 +
# tests/test_countries.py 가 중복 별칭을 차단).
_COUNTRIES: tuple[Country, ...] = (
    Country(iso2="US", qid="Q30", aliases=(
        "us", "usa", "u.s.", "u.s.a.", "united states", "united states of america",
        "america", "미국")),
    Country(iso2="KR", qid="Q884", aliases=(
        "kr", "kor", "korea", "south korea", "republic of korea", "rok",
        "대한민국", "한국", "코리아", "남한")),
    Country(iso2="JP", qid="Q17", aliases=(
        "jp", "jpn", "japan", "nippon", "nihon", "일본", "日本")),
    Country(iso2="CN", qid="Q148", aliases=(
        "cn", "chn", "china", "prc", "people's republic of china", "mainland china",
        "중국", "中国", "中國")),
    Country(iso2="PH", qid="Q928", aliases=(
        "ph", "phl", "philippines", "republic of the philippines", "pilipinas", "필리핀")),
    Country(iso2="TH", qid="Q869", aliases=(
        "th", "tha", "thailand", "kingdom of thailand", "태국", "타이")),
    Country(iso2="ID", qid="Q252", aliases=(
        "id", "idn", "indonesia", "republic of indonesia", "인도네시아")),
    Country(iso2="MY", qid="Q833", aliases=("my", "mys", "malaysia", "말레이시아")),
    Country(iso2="SG", qid="Q334", aliases=(
        "sg", "sgp", "singapore", "republic of singapore", "싱가포르", "新加坡")),
    Country(iso2="VN", qid="Q881", aliases=(
        "vn", "vnm", "vietnam", "viet nam", "socialist republic of vietnam", "베트남", "越南")),
    Country(iso2="IN", qid="Q668", aliases=(
        "in", "ind", "india", "republic of india", "bharat", "인도")),
    Country(iso2="TW", qid="Q865", aliases=(
        "tw", "twn", "taiwan", "chinese taipei", "대만", "台灣", "台湾")),
    Country(iso2="HK", qid="Q8646", aliases=(
        "hk", "hkg", "hong kong", "hong kong sar", "hksar", "홍콩", "香港")),
    Country(iso2="GB", qid="Q145", aliases=(
        "gb", "uk", "u.k.", "gbr", "united kingdom",
        "united kingdom of great britain and northern ireland", "britain", "great britain",
        "영국")),
    Country(iso2="DE", qid="Q183", aliases=(
        "de", "deu", "germany", "deutschland", "federal republic of germany", "독일")),
    Country(iso2="FR", qid="Q142", aliases=(
        "fr", "fra", "france", "french republic", "république française", "프랑스")),
    Country(iso2="AU", qid="Q408", aliases=(
        "au", "aus", "australia", "commonwealth of australia", "호주", "오스트레일리아")),
    Country(iso2="CA", qid="Q16", aliases=("ca", "can", "canada", "캐나다")),
    Country(iso2="BR", qid="Q155", aliases=(
        "br", "bra", "brazil", "brasil", "federative republic of brazil", "브라질")),
)

def _build_index() -> dict[str, Country]:
    """별칭(소문자) → Country 역인덱스. 한 별칭이 두 국가에 걸리면 조용히 덮어써 잘못
    해석되므로, 중복을 import 시점에 즉시 드러낸다(fail-loud)."""
    index: dict[str, Country] = {}
    for country in _COUNTRIES:
        for alias in country.aliases:
            if alias in index:
                raise ValueError(
                    f"중복 국가 별칭 {alias!r}: {index[alias].iso2} vs {country.iso2}"
                )
            index[alias] = country
    return index


_INDEX: dict[str, Country] = _build_index()


def resolve_country(name: str) -> Country | None:
    """세그먼트 국가 문자열을 표준 :class:`Country` 로 해석한다(미등록이면 None)."""
    return _INDEX.get((name or "").strip().lower())


def supported_countries() -> tuple[Country, ...]:
    """등록된 국가 목록(우선순위 순) — 국가 세그먼트 제너레이터의 출처."""
    return _COUNTRIES


def korean_label(country: Country) -> str:
    """국가의 한글 표시명(별칭 중 한글) — UI 표시용. 한글 별칭이 없으면 ISO2."""
    for alias in country.aliases:
        if _HANGUL.search(alias):
            return alias
    return country.iso2


def country_match_set(tokens: Iterable[str]) -> set[str]:
    """국가 토큰들을 매칭용 **소문자** 집합으로 확장한다(별칭 포함, 'KR'↔'대한민국' 호환).

    저장된 country 표기가 ISO2('KR')든 한글('대한민국')이든 잡히도록 :func:`resolve_country`
    별칭을 모두 소문자로 펼친다. 엑셀 export·아웃리치 발송의 국가 필터 공용.
    """
    vals: set[str] = set()
    for token in tokens:
        t = (token or "").strip()
        if not t:
            continue
        vals.add(t.lower())
        country = resolve_country(t)
        if country is not None:
            vals.update(alias.lower() for alias in country.aliases)
    return vals
