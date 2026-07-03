"""세그먼트 제너레이터 — 다국가 발견 구동.

글로벌 발견은 국가 × 업종 × 상장여부 세그먼트를 enumerate 해야 하고, 국가 목록의
단일 출처는 :mod:`countries` 의 ``supported_countries`` 다. CLI/러너는 이 함수로
세그먼트 배치를 만들어 :func:`run_pipeline` 에 넣는다 — 이것이 "한 번에 다국가"를
가능케 하는 마지막 조각이다(이전엔 호출부가 세그먼트를 수동 구성해야 했음).

지역(regions) 팬아웃: KR 한정으로 기본 세그먼트에 더해 지역별 세그먼트를 추가한다 —
검색 소스가 지역 키워드 쿼리("서울 …")를 던져 롱테일 기업을 발굴하는 축이다.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..region import KR_REGIONS
from .base import Segment
from .countries import supported_countries


def parse_regions(raw: str) -> list[str] | None:
    """쉼표구분 지역 입력을 파싱한다 — 'all'=KR 전 지역(17개 시/도), 빈값=None(팬아웃 없음).

    표준 축약형 외 자유 텍스트("판교" 등)도 허용한다 — 검색 쿼리 키워드로만 쓰이므로
    오타는 헛쿼리일 뿐 정확성에 영향이 없다.
    """
    vals = [s.strip() for s in raw.split(",") if s.strip()]
    if not vals:
        return None
    if any(v.lower() == "all" for v in vals):
        return list(KR_REGIONS)
    return vals


def generate_segments(
    industries: Iterable[str],
    *,
    countries: Iterable[str] | None = None,
    listed: Iterable[str] | None = None,
    regions: Iterable[str] | None = None,
) -> list[Segment]:
    """국가 × 업종 (× 상장여부) 곱집합으로 세그먼트 목록을 만든다.

    - ``countries=None`` → 등록된 전체 지원국(ISO2, 우선순위 순).
    - ``listed=None`` → ``"unknown"`` 한 가지(상장여부 미지정 — 소스가 라이브에서 정제).
    - ``regions`` 가 주어지면 **KR 세그먼트에 한해** 기본 세그먼트 뒤에 지역별 세그먼트를
      덧붙인다(등록처는 기본에서 1회, 검색은 기본+지역별 — 다른 국가는 무시: 주소 체계
      정규화가 KR 만 구현돼 있고 지역 키워드도 한국어다).
    빈/공백 항목은 제거하고, 입력 순서(국가→업종→상장→지역)를 보존한다.
    """
    inds = [i.strip() for i in industries if i and i.strip()]
    if countries is None:
        ctys = [c.iso2 for c in supported_countries()]
    else:
        ctys = [c.strip() for c in countries if c and c.strip()]
    states = [s.strip() for s in listed if s and s.strip()] if listed else ["unknown"]
    regs = [r.strip() for r in regions if r and r.strip()] if regions else []

    out: list[Segment] = []
    for country in ctys:
        for industry in inds:
            for state in states:
                out.append(Segment(country=country, industry=industry, listed=state))
                if regs and country.upper() == "KR":
                    out.extend(
                        Segment(country=country, industry=industry, listed=state, region=r)
                        for r in regs
                    )
    return out
