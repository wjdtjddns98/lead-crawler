"""골든레코드 survivorship(C3) — 확정 중복 클러스터에서 대표 1건 + 캐노니컬 라벨 선정.

C1(near_dup)·C2(llm_judge)가 **확정**한 중복 쌍(auto 티어 또는 LLM same=True)을 받아
union-find 로 클러스터를 만들고, 각 클러스터에서 생존 레코드 1건과 캐노니컬(법인 정식)
이름·도메인을 결정적으로 고른다(survivorship). 그 뒤 흡수된 행에 ``duplicate_of`` +
머지 audit 를 적어 가역적으로 합친다.

survivorship 원칙(canonical_key 권위 우선순위 registry_id>domain>name 를 미러링):
- **생존자**: 등록처(reg) 보유 > 도메인 보유 > 이름 정보량(토큰수) > key 사전순(결정적).
- **캐노니컬명**: 등록처 보유 멤버(=법인 정식명) 우선 > 토큰수 > 현지어(비ASCII) > key.
- **캐노니컬 도메인**: 권위소스(등록처 멤버) 우선 > key 사전순. 정규화(eTLD+1) 후 선택.
- 자동제거는 **최상위(auto) 티어만**(제약② 리드손실 방지) — LLM/사람 확정은 호출부가 결정.
- 전부 **순수함수·결정적**(네트워크 없음, 같은 입력=같은 출력). 머지 적용만 DB 를 만진다.

이메일 IR>contact survivorship 은 discovered_company 가 아닌 다운스트림(연락처/엑셀
export)의 role 랭킹이 담당한다 — 이 모듈은 발견 원장의 골든레코드(이름·도메인·생존자)만.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Callable

from pydantic import BaseModel

from ..dedup import normalize_domain, tokenize_name
from ..logging import get_logger

log = get_logger("dedup.golden")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ClusterMember(BaseModel):
    """클러스터 구성 레코드 1건 — survivorship 판단에 필요한 최소 식별 정보."""

    key: str  # canonical_key (고유)
    name: str
    country: str = ""
    domain: str | None = None
    registry: str | None = None
    registry_id: str | None = None
    source: str = ""


class GoldenRecord(BaseModel):
    """클러스터 1개의 골든레코드 — 생존자 + 캐노니컬 라벨 + 흡수 대상."""

    survivor_key: str  # 살아남는 레코드 key
    canonical_name: str  # 법인 정식명 라벨(survivorship 결과)
    canonical_domain: str | None  # 권위 도메인(정규화 eTLD+1) 또는 None
    country: str
    absorbed_keys: list[str]  # 이 생존자로 흡수되는 중복 key(사전순). 빈 목록=단일
    reason: str  # survivorship 근거(감사용)


def _has_registry(m: ClusterMember) -> bool:
    return bool(m.registry and m.registry_id)


def _survivor_sort_key(m: ClusterMember) -> tuple[bool, bool, int, str]:
    """생존자 정렬 키 — 작을수록 우선(권위 高 → 음수/부정으로 뒤집어 앞으로)."""
    # not 으로 뒤집어: 등록처 있음(False<True) → 앞, 도메인 있음 → 앞, 토큰 많음(-n) → 앞.
    return (not _has_registry(m), normalize_domain(m.domain) is None, -len(tokenize_name(m.name)), m.key)


def _pick_canonical_name(members: list[ClusterMember]) -> str:
    """캐노니컬명 — 등록처 보유(법인명) > 토큰수 > 현지어(비ASCII) > key 사전순."""

    def rank(m: ClusterMember) -> tuple[bool, int, bool, str]:
        non_ascii = any(ord(c) > 127 for c in m.name)
        return (not _has_registry(m), -len(tokenize_name(m.name)), not non_ascii, m.key)

    return min(members, key=rank).name.strip()


def _pick_canonical_domain(members: list[ClusterMember]) -> str | None:
    """캐노니컬 도메인 — 등록처 멤버 우선, 그다음 key 사전순. 정규화 후 None 제외."""
    cands = [
        (not _has_registry(m), m.key, normalize_domain(m.domain))
        for m in members
        if normalize_domain(m.domain) is not None
    ]
    if not cands:
        return None
    cands.sort(key=lambda t: (t[0], t[1]))
    return cands[0][2]


def build_clusters(pairs: Iterable[tuple[str, str]]) -> list[set[str]]:
    """확정 중복 쌍을 union-find 로 묶어 클러스터(연결요소) 목록을 만든다(결정적 정렬).

    이행적 병합을 처리한다 — (A,B)·(B,C) 면 {A,B,C} 한 클러스터. 단일 key 만 있는
    그룹은 만들지 않는다(쌍에 등장한 key 만 대상). 결과는 최소 key 기준 정렬.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # 경로 압축
            parent[x], x = root, parent[x]
        return root

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    groups: dict[str, set[str]] = defaultdict(set)
    for k in parent:
        groups[find(k)].add(k)
    return sorted((g for g in groups.values()), key=lambda g: min(g))


def resolve_golden(cluster: Iterable[ClusterMember], *, basis: str = "auto") -> GoldenRecord:
    """클러스터 1개에서 골든레코드(생존자+캐노니컬 라벨+흡수대상)를 결정적으로 고른다."""
    members = list(cluster)
    if not members:
        raise ValueError("빈 클러스터로는 골든레코드를 만들 수 없습니다")
    survivor = min(members, key=_survivor_sort_key)
    absorbed = sorted(m.key for m in members if m.key != survivor.key)
    return GoldenRecord(
        survivor_key=survivor.key,
        canonical_name=_pick_canonical_name(members),
        canonical_domain=_pick_canonical_domain(members),
        country=survivor.country,
        absorbed_keys=absorbed,
        reason=("단일(중복없음)" if not absorbed else f"survivorship({basis})"),
    )


def resolve_all(
    members_by_key: dict[str, ClusterMember],
    pairs: Iterable[tuple[str, str]],
    *,
    basis: str = "auto",
) -> list[GoldenRecord]:
    """확정 쌍으로 클러스터를 만들고 멤버 정보로 각 골든레코드를 산정한다(생존자 key 정렬).

    ``pairs`` 에 등장하는 key 중 ``members_by_key`` 에 없는 것은 조용히 버리지 않고 경고만
    남기고 제외한다(원장에서 사라진 key 방어). 멤버가 0이 된 클러스터는 건너뛴다.
    """
    out: list[GoldenRecord] = []
    for cluster in build_clusters(pairs):
        members = []
        for k in cluster:
            m = members_by_key.get(k)
            if m is None:
                log.warning("dedup.golden.missing_member", key=k)
                continue
            members.append(m)
        if len(members) < 2:
            continue  # 멤버 소실로 클러스터가 깨지면 머지하지 않음(제약② 보수)
        out.append(resolve_golden(members, basis=basis))
    return sorted(out, key=lambda g: g.survivor_key)


# ── DB 적용(가역) — 발견 원장에 골든레코드를 반영한다 ─────────────────────────────


def load_cluster_members(session, keys: Iterable[str]) -> dict[str, "ClusterMember"]:
    """발견 원장에서 주어진 key 들의 클러스터 멤버 정보를 적재한다(key→멤버)."""
    from sqlalchemy import select

    from ..schema import DiscoveredCompanyRow

    key_list = list(keys)
    if not key_list:
        return {}
    rows = session.execute(
        select(
            DiscoveredCompanyRow.canonical_key,
            DiscoveredCompanyRow.name,
            DiscoveredCompanyRow.country,
            DiscoveredCompanyRow.domain,
            DiscoveredCompanyRow.registry,
            DiscoveredCompanyRow.registry_id,
            DiscoveredCompanyRow.source,
        ).where(DiscoveredCompanyRow.canonical_key.in_(key_list))
    ).all()
    return {
        key: ClusterMember(
            key=key, name=name, country=country or "", domain=domain,
            registry=registry, registry_id=registry_id, source=source or "",
        )
        for key, name, country, domain, registry, registry_id, source in rows
    }


def apply_golden(
    session,
    golden: GoldenRecord,
    *,
    merged_by: str = "auto",
    merge_reason: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """골든레코드를 원장에 반영한다 — 생존자에 canonical_name, 흡수행에 duplicate_of+audit.

    가역적(전부 audit 컬럼 기록)이고, 이미 머지된 행(``duplicate_of`` 채워짐)은 건드리지
    않아 재실행 안전(idempotent). flush 만 하고 commit 은 호출부 트랜잭션에 맡긴다.
    흡수한 행 수를 반환한다(0=적용 없음).
    """
    from ..schema import DiscoveredCompanyRow

    _now = now or _utcnow
    survivor = session.get(DiscoveredCompanyRow, golden.survivor_key)
    if survivor is None:
        log.warning("dedup.golden.survivor_gone", key=golden.survivor_key)
        return 0
    survivor.canonical_name = golden.canonical_name
    if golden.canonical_domain and not survivor.domain:
        survivor.domain = golden.canonical_domain  # 생존자 도메인 비었으면 권위 도메인 채움

    stamp = _now()
    reason = merge_reason or golden.reason
    absorbed = 0
    for key in golden.absorbed_keys:
        row = session.get(DiscoveredCompanyRow, key)
        if row is None or row.duplicate_of is not None:
            continue  # 사라졌거나 이미 머지됨 → 재실행 안전(덮어쓰지 않음)
        if key == golden.survivor_key:
            continue  # 자기참조 방어
        row.duplicate_of = golden.survivor_key
        row.merged_at = stamp
        row.merged_by = merged_by
        row.merge_reason = reason
        absorbed += 1
    session.flush()
    log.info(
        "dedup.golden.applied",
        survivor=golden.survivor_key,
        absorbed=absorbed,
        merged_by=merged_by,
    )
    return absorbed
