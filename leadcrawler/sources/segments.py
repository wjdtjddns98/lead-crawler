"""세그먼트 제너레이터 — 다국가 발견 구동.

글로벌 발견은 국가 × 업종 × 상장여부 세그먼트를 enumerate 해야 하고, 국가 목록의
단일 출처는 :mod:`countries` 의 ``supported_countries`` 다. CLI/러너는 이 함수로
세그먼트 배치를 만들어 :func:`run_pipeline` 에 넣는다 — 이것이 "한 번에 다국가"를
가능케 하는 마지막 조각이다(이전엔 호출부가 세그먼트를 수동 구성해야 했음).
"""

from __future__ import annotations

from collections.abc import Iterable

from .base import Segment
from .countries import supported_countries


def generate_segments(
    industries: Iterable[str],
    *,
    countries: Iterable[str] | None = None,
    listed: Iterable[str] | None = None,
) -> list[Segment]:
    """국가 × 업종 (× 상장여부) 곱집합으로 세그먼트 목록을 만든다.

    - ``countries=None`` → 등록된 전체 지원국(ISO2, 우선순위 순).
    - ``listed=None`` → ``"unknown"`` 한 가지(상장여부 미지정 — 소스가 라이브에서 정제).
    빈/공백 항목은 제거하고, 입력 순서(국가→업종→상장)를 보존한다.
    """
    inds = [i.strip() for i in industries if i and i.strip()]
    if countries is None:
        ctys = [c.iso2 for c in supported_countries()]
    else:
        ctys = [c.strip() for c in countries if c and c.strip()]
    states = [s.strip() for s in listed if s and s.strip()] if listed else ["unknown"]

    out: list[Segment] = []
    for country in ctys:
        for industry in inds:
            for state in states:
                out.append(Segment(country=country, industry=industry, listed=state))
    return out
