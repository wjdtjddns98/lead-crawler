"""발견 소스 공통 인터페이스 + dry_run 더미 소스.

실 소스(EDGAR/DART/거래소/CH/디렉터리/검색API)는 이 Protocol 을 구현한다.
dry_run 에서는 :class:`DummySource` 가 네트워크 없이 결정적 후보를 만든다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from functools import partial
from typing import Protocol

from pydantic import BaseModel, Field

from ..dedup import canonical_key
from ..logging import get_logger
from ..region import region_from_address
from .countries import resolve_country
from .industry import resolve_industry_label

log = get_logger("sources.cursor")


class Segment(BaseModel):
    """수집 단위: 국가 × 업종 × 상장여부 (× 지역 — KR 검색 팬아웃 전용).

    ``region`` 이 있으면 검색 소스만 지역 키워드를 붙여 돈다(등록처·집계원은 주소로
    열거하지 않으므로 기본 세그먼트에서 1회 — :func:`registry.discover_segment` 가 게이팅).
    """

    country: str
    industry: str
    listed: str = "unknown"
    region: str | None = None

    @property
    def label(self) -> str:
        base = f"{self.country}/{self.industry}/{self.listed}"
        return f"{base}/{self.region}" if self.region else base


class DiscoveredCompany(BaseModel):
    """발견 단계 산출 — 식별 정보 + canonical_key.

    풍부필드(address~name_eng)는 등록처 응답이 이미 주는 값을 버리지 않고 담는
    optional 슬롯 — 소스마다 채워짐이 다르다(None=해당 소스 미제공).
    활용처: reg_no=dedup 확정키, region=지역 필터, ir_url·phone=연락처 수율,
    name_eng·ticker=검색 쿼리 확장.
    """

    canonical_key: str
    name: str
    country: str = ""
    industry: str = ""
    listed: str = "unknown"
    domain: str | None = None
    registry: str | None = None
    registry_id: str | None = None
    source: str = ""
    segment: str | None = None
    address: str | None = None
    region: str | None = None
    reg_no: str | None = None
    ticker: str | None = None
    phone: str | None = None
    ir_url: str | None = None
    name_eng: str | None = None
    # 상장 시장(보드) 세분화 — 예: KOSPI/KOSDAQ/KONEX(DART corp_cls)·NASDAQ/NYSE(EDGAR)·
    # PSE/SGX(거래소 소스). listed 3값(listed/unlisted/unknown)의 세부 라벨(None=미상).
    market: str | None = None
    # listed 가 **사실 조회로 확인된 값**인지(DART corp_cls·EDGAR 거래소 필드·거래소
    # 상장목록). False=크롤 스코프(segment.listed) 통과값 — 검색·집계원 다수가 여기 해당.
    # 원장 백필(save_discovered)은 True 인 값만 신뢰한다(스코프값 사실 고착 방지).
    listed_verified: bool = False


class DiscoverySource(Protocol):
    """벌크 발견 소스 인터페이스."""

    name: str

    def applies_to(self, segment: Segment) -> bool:
        """이 소스가 해당 세그먼트(국가·상장여부)에 적용 가능한지."""
        ...

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트에 해당하는 기업 목록을 반환한다."""
        ...


# 발견 청킹 seam(source-agnostic) — 큰 세그먼트(DART)를 순수 구간 워커 N개로 나눠 소스풀이
# 유휴 슬롯까지 동시에 점유하게 한다. 부작용(커서·쿼터·dedup)은 전부 오케스트레이터
# (registry.discover_segment 메인스레드)로 몰아 청크워커는 순수하게 유지한다(제약 ①).
Chunk = Callable[[], list["DiscoveredCompany"]]
ChunkFinalize = Callable[[list[list["DiscoveredCompany"]]], None]


def _noop_finalize(results: list[list[DiscoveredCompany]]) -> None:  # noqa: ARG001
    """기본 finalize — 비청킹 소스는 병합 뒤 후처리가 없다(커서/쿼터 훅 없음)."""


def discovery_chunks(src: DiscoverySource, segment: Segment) -> tuple[list[Chunk], ChunkFinalize]:
    """소스의 발견을 (청크 콜러블 목록, finalize 훅)으로 얻는다.

    소스가 ``discover_chunks`` 를 구현하면 위임하고(DART 만 window 를 N구간으로 분할),
    아니면 기본=[전체 1청크]·finalize no-op 을 반환한다 — 비청킹 소스 회귀 0. registry 는
    전 소스 청크를 flatten 해 소스풀에 동시 제출하고, 수집 후 메인스레드에서 소스별
    finalize(자기 청크결과)를 호출한다(커서/쿼터 확정).
    """
    override = getattr(src, "discover_chunks", None)
    if override is not None:
        return override(segment)
    return [partial(src.discover, segment)], _noop_finalize


class SupportsCursorStore(Protocol):
    """등록처 발견 커서 저장소 — 런 간 스캔 위치 영속(구현: storage.discovery_cursor).

    커서는 최적화일 뿐 정확성 불변: 잃어도 다음 런이 같은 구간을 재스캔하고
    dedup(제약 ①)이 걸러낸다. 구현은 실패를 삼키고 get 은 0 폴백해야 한다.
    """

    def get(self, source: str, key: str) -> int:
        """마지막으로 영속된 다음 스캔 위치(없으면 0)."""
        ...

    def advance(self, source: str, key: str, position: int) -> None:
        """다음 런이 시작할 위치를 영속한다."""
        ...


def cursor_offset(
    store: SupportsCursorStore | None, source: str, segment: Segment, total: int
) -> int:
    """영속 커서에서 이번 런의 시작 offset 을 읽는다(store 없음·범위 밖이면 0).

    모집단 목록이 런 사이에 줄어 offset 이 끝을 넘으면 0 으로 되감는다(재검증 재개).
    """
    if store is None or total <= 0:
        return 0
    offset = store.get(source, segment.label)
    return offset if 0 <= offset < total else 0


def advance_cursor(
    store: SupportsCursorStore | None,
    source: str,
    segment: Segment,
    position: int,
    total: int,
) -> None:
    """다음 런 시작 위치를 영속한다. 모집단 끝 도달 시 0 리셋 + exhausted 로그."""
    if store is None or total <= 0:
        return
    if position >= total:
        log.info("cursor.exhausted", source=source, segment=segment.label, total=total)
        position = 0
    store.advance(source, segment.label, position)


def is_country(segment: Segment, names: AbstractSet[str]) -> bool:
    """세그먼트 국가가 별칭 집합(소문자) 중 하나인지 판정한다(set/frozenset 모두 허용)."""
    return segment.country.strip().lower() in names


def opt_str(value: object) -> str | None:
    """API 응답 값을 optional 문자열로 정규화한다('' 나 비문자열은 None)."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


# 원어(비라틴 문자) 표시명 판정 — 가나·CJK·한글·태국·키릴·그리스·히브리·아랍·데바나가리
# 문자가 하나라도 있으면 '원어'. ponytail: 관측 표본 기준 주요 문자권만 — 누락 문자권은 그 나라
# 소스가 생길 때 추가.
_NON_LATIN = re.compile(r"[぀-ヿ㐀-鿿가-힯฀-๿Ѐ-ӿͰ-Ϳ֐-׿؀-ۿऀ-ॿ]")
# 영문 표시명으로 받아들이는 닫힌 형식 — 라틴 문자·숫자·상호에 흔한 구두점만(도메인·URL 은
# 호출부에서 별도 기각). 알파벳 3연속이 없으면 상호로 안 본다.
_LATIN_NAME = re.compile(r"^[A-Za-z0-9 .,&'()\-/+]{3,120}$")


def is_non_latin_name(name: str | None) -> bool:
    """표시명이 원어(비라틴 문자 포함)인지 — 영문 우선 규약의 적용 대상 판정."""
    return bool(name) and _NON_LATIN.search(name) is not None


def latin_name_or_none(value: str | None) -> str | None:
    """영문 표시명 후보를 닫힌 형식으로 검증해 돌려준다(부적합=None)."""
    e = (value or "").strip().strip('"').strip()
    if not e or not _LATIN_NAME.match(e) or not re.search(r"[A-Za-z]{3}", e):
        return None
    return e


def needs_english_name(name: str | None, country: str) -> bool:
    """영문 표시명 교체 대상인지 — 원어 표시명이고 **KR 이 아닌** 회사(PO 2026-09-04:
    한국은 한국어 유지, 그 외 전부 영문)."""
    c = resolve_country(country)
    return is_non_latin_name(name) and not (c is not None and c.iso2 == "KR")


def english_display(local: str, eng: str | None, country: str) -> tuple[str, str | None]:
    """표시명 영문 우선(#412 규약, KR 제외): 원어 상호에 소스가 준 영문명이 있으면
    ``(name=영문, name_eng=원어)``, 아니면 ``(원어, None)``. 이미 라틴 표시명이면 그대로."""
    if not needs_english_name(local, country):
        return local, None
    e = latin_name_or_none(eng)
    if e is None:
        return local, None
    return e, local


def join_address(*parts: object) -> str | None:
    """주소 조각들을 ', ' 로 합친다(빈 값 제거, 전부 비면 None)."""
    cleaned = [s for p in parts if (s := opt_str(p)) is not None]
    return ", ".join(cleaned) if cleaned else None


def build_company(
    *,
    source: str,
    segment: Segment,
    name: str,
    domain: str | None = None,
    registry: str | None = None,
    registry_id: str | None = None,
    industry_code_label: str | None = None,
    address: str | None = None,
    region: str | None = None,
    reg_no: str | None = None,
    ticker: str | None = None,
    phone: str | None = None,
    ir_url: str | None = None,
    name_eng: str | None = None,
    market: str | None = None,
    listed_verified: bool = False,
) -> DiscoveredCompany:
    """식별 정보로 ``canonical_key`` 를 산정해 :class:`DiscoveredCompany` 를 만든다.

    ``industry`` 는 :func:`resolve_industry_label` 로 정한다: 구체 업종 검색이면 세그먼트
    업종 그대로, broad('전체' 등)면 등록처 코드에서 복원한 ``industry_code_label``(명확
    단일매치, 등록처 소스만 전달)을 쓰고 없으면 '미분류'(파이프라인이 이후 LLM 배치 시도).
    ``segment`` 라벨(provenance)은 원래 세그먼트 업종을 유지한다 — 구분 컬럼과 별개.

    풍부필드는 소스가 받은 만큼만 전달한다. ``region`` 미전달 시 주소 원문에서
    :func:`region_from_address` 로 파생을 시도한다(현재 KR 만).
    """
    key = canonical_key(
        registry=registry,
        registry_id=registry_id,
        domain=domain,
        name=name,
        country=segment.country,
    )
    return DiscoveredCompany(
        canonical_key=key,
        name=name,
        country=segment.country,
        industry=resolve_industry_label(segment.industry, code_label=industry_code_label),
        listed=segment.listed,
        domain=domain,
        registry=registry,
        registry_id=registry_id,
        source=source,
        segment=segment.label,
        address=address,
        region=region or region_from_address(segment.country, address),
        reg_no=reg_no,
        ticker=ticker,
        phone=phone,
        ir_url=ir_url,
        name_eng=name_eng,
        market=market,
        listed_verified=listed_verified,
    )


class DummySource(BaseModel):
    """dry_run 용 결정적 더미 소스(네트워크 없음)."""

    name: str = "dummy"
    count: int = Field(default=3)

    def applies_to(self, segment: Segment) -> bool:  # noqa: ARG002 — 모든 세그먼트 적용
        """더미 소스는 모든 세그먼트에 적용된다."""
        return True

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트당 ``count`` 개의 결정적 더미 기업을 만든다."""
        out: list[DiscoveredCompany] = []
        for i in range(self.count):
            # 등록도메인(eTLD+1)이 i 마다 달라지도록 구성(서브도메인 축약 회피).
            domain = f"{segment.country.lower()}-firm{i}.com"
            out.append(
                DiscoveredCompany(
                    canonical_key=canonical_key(domain=domain),
                    name=f"{segment.industry} 더미기업 {i}",
                    country=segment.country,
                    industry=resolve_industry_label(segment.industry),
                    domain=domain,
                    source=self.name,
                    segment=segment.label,
                )
            )
        return out
