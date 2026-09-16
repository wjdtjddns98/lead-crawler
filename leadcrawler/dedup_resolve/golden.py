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

import json
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
    duplicate_of: str | None = None  # 이미 흡수된 행이면 생존자 key(체인 방지 판단용)


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

    원장에 없는 key 가 낀 **쌍 자체를 버린 뒤** 살아남은 쌍으로만 클러스터를 재구성한다 —
    허브 멤버가 사라져 그 멤버로만 연결돼 있던 두 행이 직접 중복근거 없이 잘못 합쳐지는
    것을 막는다(제약② 리드손실 방지). 끊긴 부분그래프는 자연히 분리돼 단독이면 제외된다.
    """
    pair_list = list(pairs)
    missing = sorted({k for a, b in pair_list for k in (a, b) if k not in members_by_key})
    for k in missing:
        log.warning("dedup.golden.missing_member", key=k)
    # 양 끝이 모두 원장에 있는 쌍만 사용 → 연결성 보존(허브 소실 시 컴포넌트 자연 분리).
    live_pairs = [(a, b) for a, b in pair_list if a in members_by_key and b in members_by_key]
    out: list[GoldenRecord] = []
    for cluster in build_clusters(live_pairs):
        members = [members_by_key[k] for k in cluster]
        if len(members) < 2:
            continue
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
            DiscoveredCompanyRow.duplicate_of,
        ).where(DiscoveredCompanyRow.canonical_key.in_(key_list))
    ).all()
    return {
        key: ClusterMember(
            key=key, name=name, country=country or "", domain=domain,
            registry=registry, registry_id=registry_id, source=source or "",
            duplicate_of=duplicate_of,
        )
        for key, name, country, domain, registry, registry_id, source, duplicate_of in rows
    }


def _merge_company_rows(session, survivor_key: str, absorbed_key: str) -> str | None:
    """흡수행의 ``company`` 본체를 생존자로 합친다 — 큐·엑셀에 남던 중복 해소.

    원장 ``duplicate_of`` 만 적던 종전 머지는 이미 승격된 두 회사 행을 그대로 둬 검증큐·
    엑셀에 같은 회사가 2건씩 남았다(2026-09-16 실측 3,133쌍). 9/1 자산운용 1회성 병합
    규칙을 공식 경로로 이식:
    - 생존자 쪽 company 가 없으면 **키만 이관**(본체·연락처·큐·감사 이력 전부 보존).
    - 둘 다 있으면 연락처를 생존자 id 로 복제(contact id 는 (company,type,value) 해시라
      UPDATE 불가)·이메일 검증결과 복제·큐 후보 합집합·**사람 확정 판단 승계**(흡수행이
      confirmed 이고 생존자가 미확정이면 그 판단을 가져온다 — 리드손실 방지. 둘 다 확정이면
      생존자 것 유지)·감사 이력을 생존자 큐 행으로 재지정한 뒤 흡수 company 행을 지운다.
      이 삭제는 비가역(원장 링크는 가역) — 확정 티어(reg_no/auto)만 여기 도달한다.
    반환 ``"repointed"`` | ``"merged"`` | ``None``(흡수행에 company 없음).
    """
    from sqlalchemy import delete, select, update

    from ..schema import CompanyRow, ContactRow, EmailValidationRow, ReviewAuditRow
    from ..storage.repository import contact_id_for
    from ..storage.review import (
        ReviewQueueRow,
        candidate_values_of,
        enqueue_email_review,
        review_id_for,
    )

    absorbed = session.scalars(
        select(CompanyRow).where(CompanyRow.canonical_key == absorbed_key)
    ).first()
    if absorbed is None:
        return None
    survivor = session.scalars(
        select(CompanyRow).where(CompanyRow.canonical_key == survivor_key)
    ).first()
    if survivor is None:
        absorbed.canonical_key = survivor_key
        return "repointed"

    for col in ("homepage", "industry"):  # 생존자 공란만 채움(기존 값 불변).
        if not getattr(survivor, col) and getattr(absorbed, col):
            setattr(survivor, col, getattr(absorbed, col))

    have = {
        (c.type, c.value.lower())
        for c in session.scalars(select(ContactRow).where(ContactRow.company_id == survivor.id))
    }
    srq = session.get(ReviewQueueRow, review_id_for(survivor.id, "email"))
    arq = session.get(ReviewQueueRow, review_id_for(absorbed.id, "email"))
    cands = candidate_values_of(srq) if srq is not None else []
    absorbed_contacts = session.scalars(
        select(ContactRow).where(ContactRow.company_id == absorbed.id)
    ).all()
    for c in absorbed_contacts:
        if (c.type, c.value.lower()) in have:
            continue
        nid = contact_id_for(survivor.id, c.type, c.value)
        session.add(ContactRow(
            id=nid, company_id=survivor.id, type=c.type, value=c.value, role=c.role,
            extract_method=c.extract_method, confidence=c.confidence,
        ))
        session.flush()  # FK 순서: 복제 contact 가 검증결과보다 먼저.
        ev = session.get(EmailValidationRow, c.id)
        if ev is not None:
            session.add(EmailValidationRow(
                contact_id=nid, status=ev.status, mx=ev.mx, domain_match=ev.domain_match,
                smtp=ev.smtp, provider=ev.provider, checked_at=ev.checked_at,
            ))
        have.add((c.type, c.value.lower()))
        if c.type == "email" and c.value not in cands:
            cands.append(c.value)

    if arq is not None:
        cands += [v for v in candidate_values_of(arq) if v not in cands]
        if srq is None:
            enqueue_email_review(session, survivor.id, cands)
            srq = session.get(ReviewQueueRow, review_id_for(survivor.id, "email"))
        if arq.status == "confirmed" and srq.status != "confirmed":
            for col in (
                "status", "assignee", "assignee_id", "reviewed_at", "selected",
                "selected_by_human", "note", "has_attachment", "manager",
            ):
                setattr(srq, col, getattr(arq, col))
        # 흡수 큐 행의 처리 이력은 CASCADE 로 사라지므로 생존자 큐 행으로 재지정(책임추적 보존).
        session.execute(
            update(ReviewAuditRow)
            .where(ReviewAuditRow.review_id == arq.id)
            .values(review_id=srq.id),
            execution_options={"synchronize_session": False},
        )
    if srq is not None:
        srq.candidates = json.dumps(cands, ensure_ascii=False)
        if srq.selected not in cands:
            srq.selected = cands[0] if cands else None
            srq.selected_by_human = False

    # 흡수 본체 삭제 — PG 는 CASCADE 지만 명시 삭제로 백엔드 무관하게 같은 결과(SQLite 테스트).
    absorbed_ids = [c.id for c in absorbed_contacts]
    if absorbed_ids:
        session.execute(delete(EmailValidationRow).where(EmailValidationRow.contact_id.in_(absorbed_ids)))
        session.execute(delete(ContactRow).where(ContactRow.company_id == absorbed.id))
    session.execute(delete(ReviewQueueRow).where(ReviewQueueRow.company_id == absorbed.id))
    session.delete(absorbed)
    session.flush()
    log.info("dedup.golden.company_merged", survivor=survivor.id, absorbed=absorbed.id)
    return "merged"


def apply_golden(
    session,
    golden: GoldenRecord,
    *,
    merged_by: str = "auto",
    merge_reason: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """골든레코드를 원장에 반영한다 — 생존자에 canonical_name, 흡수행에 duplicate_of+audit.

    가역적(전부 audit 컬럼 기록)이고 재실행 안전(idempotent — 이미 머지된 행은 안 건드림).
    flush 만 하고 commit 은 호출부 트랜잭션에 맡긴다. 흡수한 행 수를 반환한다(0=적용 없음).

    **체인 방지(가역성 불변)**: ``duplicate_of`` 는 항상 살아있는 root 를 가리켜야 한다.
    ① 생존 후보가 이미 흡수된 비-root 면(다른 행의 자식) 머지를 거부한다(제약② 보수 — 비-root
       로 흡수하면 b→a→root 체인이 생겨 단일 조인이 손자를 놓침). ② 어떤 행을 흡수할 때 그
       행이 과거 다른 행들의 생존자였다면(자식 보유) 그 자식들을 **새 생존자로 재지정**해
       체인을 평탄화한다(고아 방지). 생존자 도메인은 비어 있을 때만 권위 도메인으로 채운다
       (이미 값이 있으면 덮지 않음 — key 안정성·기존 값 보존).
    """
    from sqlalchemy import update

    from ..schema import DiscoveredCompanyRow

    _now = now or _utcnow
    survivor = session.get(DiscoveredCompanyRow, golden.survivor_key)
    if survivor is None:
        log.warning("dedup.golden.survivor_gone", key=golden.survivor_key)
        return 0
    if survivor.duplicate_of is not None:
        # 생존 후보가 이미 흡수된 비-root → 체인 생성 방지로 머지 거부(워크벤치/재리포트 위임).
        log.warning(
            "dedup.golden.survivor_already_absorbed",
            key=golden.survivor_key,
            root=survivor.duplicate_of,
        )
        return 0
    survivor.canonical_name = golden.canonical_name
    if golden.canonical_domain and not survivor.domain:
        survivor.domain = golden.canonical_domain  # 생존자 도메인 비었을 때만 권위 도메인 채움

    stamp = _now()
    reason = merge_reason or golden.reason
    absorbed = 0
    for key in golden.absorbed_keys:
        if key == golden.survivor_key:
            continue  # 자기참조 방어
        row = session.get(DiscoveredCompanyRow, key)
        if row is None or row.duplicate_of is not None:
            continue  # 사라졌거나 이미 머지됨 → 재실행 안전(덮어쓰지 않음)
        # 이 행이 과거 생존자라 자식을 보유했다면 자식들을 새 생존자로 재지정(체인 평탄화).
        session.execute(
            update(DiscoveredCompanyRow)
            .where(DiscoveredCompanyRow.duplicate_of == key)
            .values(
                duplicate_of=golden.survivor_key,
                merged_at=stamp,
                merged_by=merged_by,
                merge_reason=f"{reason} (rechained)",
            ),
            execution_options={"synchronize_session": False},
        )
        row.duplicate_of = golden.survivor_key
        row.merged_at = stamp
        row.merged_by = merged_by
        row.merge_reason = reason
        absorbed += 1
        # 승격된 company 본체까지 합친다(큐·엑셀 중복 해소). 원장 링크 다음에 실행해야
        # 실패 시 링크만 남고 본체는 다음 재실행(idempotent)에서 다시 시도된다.
        _merge_company_rows(session, golden.survivor_key, key)
    session.flush()
    session.expire_all()  # 벌크 update 로 갱신된 자식 행의 ORM 캐시 무효화(이후 조회 정합)
    log.info(
        "dedup.golden.applied",
        survivor=golden.survivor_key,
        absorbed=absorbed,
        merged_by=merged_by,
    )
    return absorbed
