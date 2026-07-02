"""렉시컬 near-dup 후보 탐지 — 중복해소 사다리의 무료·결정적 단계(C1).

흐름: 블로킹(국가+도메인root / 국가+이름prefix)으로 비교쌍을 줄인 뒤, 각 쌍을
rapidfuzz 토큰셋 유사도 + 도메인root 일치로 **티어 분류**한다. 네트워크·과금 없음,
입력이 같으면 출력도 같다(결정적).

티어(자동제거는 최상위 2개만, 나머지는 LLM/사람 쇼트리스트 — 제약② 리드손실 방지):
- ``reg_no``    같은 국가 + 현지 등록번호(사업자번호 등) 일치 → **확정** 중복(점수 무관).
- ``auto``      이름 高 + 도메인root 일치 → 자동제거 후보(가역).
- ``domain``    도메인root 일치·이름 낮음 → 같은 기업 가능(쇼트리스트).
- ``lexical``   이름 高·도메인 불명(한쪽이라도 없음) → 쇼트리스트.
- ``shortlist`` 이름 中(경계) → LLM/사람 판정.
- ``keep_both`` 이름 高이나 도메인 명백히 상이 → 동명이인 가능, **둘 다 유지**(정보용).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from itertools import combinations

from pydantic import BaseModel
from rapidfuzz import fuzz

from ..dedup import normalize_domain, normalize_reg_no, tokenize_name
from ..logging import get_logger

log = get_logger("dedup_resolve")

# ── 임계값(결정적 분류용). 운영 보정은 CLI 옵션으로 덮어쓴다.
NAME_STRONG = 90.0  # 이상이면 이름 高(자동/keep_both 경계)
NAME_MEDIUM = 84.0  # 이상이면 쇼트리스트 하한(LLM/사람 판정 대상)
# 이름 블로킹 prefix 길이(같은 prefix 끼리만 비교). 길수록 블록이 잘게 쪼개져
# 대기업 prefix 군집(예: 한국 재벌)의 블록 폭발을 줄인다(짧은 이름 recall 은 소폭 손해).
NAME_PREFIX_BLOCK = 6
# 한 블록 내 비교쌍 폭발 방지 캡. 블록 크기가 이 값을 넘으면 그 블록의 쌍 비교를
# **생략**하고 skipped_blocks 로 보고한다(공유 호스팅·플레이스홀더 도메인, 동일 prefix
# 군집이 O(n²) 로 폭발하는 것을 막음 — M1/M2). 캡으로 생략된 블록은 절대 조용히 버리지
# 않고 리포트에 남겨, 운영자가 --max-block 을 높여 완전 재실행할 수 있게 한다.
MAX_BLOCK_SIZE = 1000

# 리포트 정렬·요약용 티어 우선순위(작을수록 먼저).
_TIER_ORDER = {"reg_no": 0, "auto": 1, "domain": 2, "lexical": 3, "shortlist": 4, "keep_both": 5}

# 확정(자동머지 가능) 티어 — cli dedup-merge·리포트 auto_removable 이 공유하는 단일 출처.
CONFIRMED_TIERS: frozenset[str] = frozenset({"reg_no", "auto"})


class CompanyRecord(BaseModel):
    """비교 입력 — 발견 원장 1행에서 필요한 최소 식별 정보."""

    key: str  # canonical_key (고유)
    name: str
    country: str = ""
    domain: str | None = None
    reg_no: str | None = None  # 현지 등록번호(사업자번호 등) — 일치 시 확정 티어


class DuplicateCandidate(BaseModel):
    """중복 후보 쌍 1건 — 사람/LLM 검토 및 자동제거 입력."""

    key_a: str
    key_b: str
    name_a: str
    name_b: str
    country: str
    domain_a: str | None
    domain_b: str | None
    name_score: float  # 0~100 토큰셋 유사도
    tier: str  # reg_no | auto | domain | lexical | shortlist | keep_both
    reason: str


class SkippedBlock(BaseModel):
    """크기 초과로 쌍 비교를 생략한 블록 — 커버리지 공백을 명시적으로 남긴다."""

    kind: str  # 'reg'(등록번호) | 'dom'(도메인root) | 'name'(이름prefix)
    country: str
    bucket: str  # 도메인 root 또는 이름 prefix
    size: int


class MatchResult(BaseModel):
    """매칭 결과 — 후보 목록 + 생략된 블록(공백 보고)."""

    candidates: list[DuplicateCandidate]
    skipped_blocks: list[SkippedBlock]


def _name_score(tokens_a: list[str], tokens_b: list[str]) -> float:
    """토큰셋 유사도(0~100). 토큰 순서·중복·부분집합에 강건(rapidfuzz)."""
    return round(fuzz.token_set_ratio(" ".join(tokens_a), " ".join(tokens_b)), 1)


def _classify(
    rec_a: CompanyRecord,
    rec_b: CompanyRecord,
    tokens: dict[str, list[str]],
    domains: dict[str, str | None],
    regs: dict[str, str | None],
    *,
    name_strong: float,
    name_medium: float,
) -> DuplicateCandidate | None:
    """두 레코드를 비교해 티어를 매긴다(후보 아니면 None).

    토큰·정규화 도메인·등록번호는 레코드당 1회만 산정해 ``tokens``/``domains``/``regs`` 로
    넘긴다(쌍마다 재계산하면 블록당 O(n²) 호출로 폭증 — 전건 배치에서 파싱이 지배비용이 됨).
    """
    score = _name_score(tokens[rec_a.key], tokens[rec_b.key])
    dom_a = domains[rec_a.key]
    dom_b = domains[rec_b.key]
    both_dom = dom_a is not None and dom_b is not None
    dom_equal = both_dom and dom_a == dom_b
    reg_a = regs[rec_a.key]
    reg_b = regs[rec_b.key]

    # 확정 티어: 같은 국가 + 등록번호 일치는 이름·도메인과 무관하게 동일 기업
    # (등록처가 발급한 고유번호라 표기 차이로 갈라진 행을 점수 없이 접는다).
    # 역방향도 확정적이다: 둘 다 번호가 있는데 다르면 **별개 법인** — 이름·도메인이
    # 겹쳐도(계열사가 그룹 도메인 공유 등) 자동머지(auto) 대상에서 제외한다.
    both_reg = reg_a is not None and reg_b is not None and rec_a.country == rec_b.country
    if both_reg and reg_a == reg_b:
        tier, reason = "reg_no", "현지 등록번호 일치(확정)"
    elif both_reg:  # 번호 상이 = 별개 법인 확정
        if score >= name_strong or dom_equal:
            tier, reason = "keep_both", "등록번호 상이(별개 법인 → 둘 다 유지)"
        else:
            return None
    elif dom_equal:
        if score >= name_strong:
            tier, reason = "auto", "이름 高 + 도메인root 일치"
        else:
            tier, reason = "domain", "도메인root 일치·이름 상이(쇼트리스트)"
    elif both_dom:  # 둘 다 도메인 있는데 root 가 다름
        if score >= name_strong:
            tier, reason = "keep_both", "이름 유사·도메인 상이(동명이인 가능 → 둘 다 유지)"
        else:
            return None  # 이름도 낮고 도메인도 다름 → 같은 기업 근거 없음
    else:  # 한쪽 이상 도메인 불명
        if score >= name_strong:
            tier, reason = "lexical", "이름 高·도메인 불명(쇼트리스트)"
        elif score >= name_medium:
            tier, reason = "shortlist", "이름 중간 유사(쇼트리스트)"
        else:
            return None

    # 출력 순서를 결정적으로 — key 사전순으로 a<b 고정.
    if rec_a.key > rec_b.key:
        rec_a, rec_b, dom_a, dom_b = rec_b, rec_a, dom_b, dom_a
    return DuplicateCandidate(
        key_a=rec_a.key,
        key_b=rec_b.key,
        name_a=rec_a.name,
        name_b=rec_b.name,
        country=rec_a.country,
        domain_a=dom_a,
        domain_b=dom_b,
        name_score=score,
        tier=tier,
        reason=reason,
    )


def _blocks(
    records: list[CompanyRecord],
    tokens: dict[str, list[str]],
    domains: dict[str, str | None],
    regs: dict[str, str | None],
) -> dict[tuple[str, str, str], list[CompanyRecord]]:
    """비교쌍 폭발을 막는 블로킹 — 같은 (국가, 등록번호/도메인root/이름prefix) 끼리 묶는다.

    한 레코드가 여러 블록에 들어갈 수 있다(서로 다른 후보를 잡기 위함).
    토큰·도메인·등록번호는 미리 산정한 dict 를 재사용한다(레코드당 1회).
    """
    by_block: dict[tuple[str, str, str], list[CompanyRecord]] = defaultdict(list)
    for rec in records:
        country = rec.country or ""
        reg = regs[rec.key]
        if reg:
            by_block[("reg", country, reg)].append(rec)
        dom = domains[rec.key]
        if dom:
            by_block[("dom", country, dom)].append(rec)
        name_key = "".join(tokens[rec.key])
        if name_key:
            by_block[("name", country, name_key[:NAME_PREFIX_BLOCK])].append(rec)
    return by_block


def match_records(
    records: Iterable[CompanyRecord],
    *,
    name_strong: float = NAME_STRONG,
    name_medium: float = NAME_MEDIUM,
    max_block_size: int = MAX_BLOCK_SIZE,
) -> MatchResult:
    """발견 레코드에서 중복 후보 쌍 + 생략된 블록을 찾는다(결정적·무료).

    블로킹 후 블록 내 모든 쌍을 분류한다. 같은 쌍이 여러 블록에서 나와도 한 번만
    남긴다 — 이는 :func:`_classify` 가 블록과 무관한 **순수 함수**(도메인을 블록 키가
    아니라 레코드에서 재계산)이기 때문에 안전하다(first-wins 가 순서무관). 크기가
    ``max_block_size`` 를 넘는 블록은 O(n²) 폭발을 막기 위해 비교를 생략하고
    :class:`SkippedBlock` 으로 보고한다(조용히 버리지 않음 — 운영자가 캡을 높여 재실행 가능).
    결과 후보는 (티어, -점수, key) 순으로 결정적 정렬한다.
    """
    recs = list(records)
    # 레코드당 1회만 토큰화·도메인/등록번호 정규화(쌍마다 재계산 회피 — 전건 배치 핫패스).
    tokens = {r.key: tokenize_name(r.name) for r in recs}
    domains = {r.key: normalize_domain(r.domain) for r in recs}
    regs = {r.key: normalize_reg_no(r.reg_no) for r in recs}
    found: dict[tuple[str, str], DuplicateCandidate] = {}
    skipped: list[SkippedBlock] = []
    for (kind, country, bucket), block in _blocks(recs, tokens, domains, regs).items():
        if len(block) < 2:
            continue
        if len(block) > max_block_size:
            skipped.append(SkippedBlock(kind=kind, country=country, bucket=bucket, size=len(block)))
            log.warning(
                "dedup.block_skipped",
                kind=kind,
                country=country,
                bucket=bucket,
                size=len(block),
                cap=max_block_size,
            )
            continue
        for a, b in combinations(block, 2):
            if a.key == b.key:
                continue  # 같은 행이 블록에 중복 적재된 경우 방어
            pair = (a.key, b.key) if a.key < b.key else (b.key, a.key)
            if pair in found:
                continue  # first-wins — _classify 가 블록무관 순수함수라 안전(m3)
            cand = _classify(
                a, b, tokens, domains, regs, name_strong=name_strong, name_medium=name_medium
            )
            if cand is not None:
                found[pair] = cand
    candidates = sorted(
        found.values(),
        key=lambda c: (_TIER_ORDER.get(c.tier, 9), -c.name_score, c.key_a, c.key_b),
    )
    skipped.sort(key=lambda s: (-s.size, s.kind, s.country, s.bucket))
    return MatchResult(candidates=candidates, skipped_blocks=skipped)


def find_duplicate_candidates(
    records: Iterable[CompanyRecord],
    *,
    name_strong: float = NAME_STRONG,
    name_medium: float = NAME_MEDIUM,
    max_block_size: int = MAX_BLOCK_SIZE,
) -> list[DuplicateCandidate]:
    """:func:`match_records` 의 후보 목록만 반환하는 얇은 래퍼(생략 블록 무시)."""
    return match_records(
        records,
        name_strong=name_strong,
        name_medium=name_medium,
        max_block_size=max_block_size,
    ).candidates
