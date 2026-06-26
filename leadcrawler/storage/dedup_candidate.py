"""중복후보 워크벤치(C4) storage — 적재(populate)·조회(list)·결정(merge/separate).

near_dup(C1)/LLM(C2) 리포트의 경계 후보를 ``dedup_candidate`` 에 멱등 적재하고, 사람의
동일확정(merge)·분리(separate) 결정을 영속한다. 동일확정은 기존 골든레코드 엔진
(:mod:`leadcrawler.dedup_resolve.golden`)을 재사용해 ``duplicate_of`` 를 가역·감사
가능하게 기록한다. 네트워크·과금 없음(전부 DB 로컬 연산).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..dedup_resolve.golden import apply_golden, load_cluster_members, resolve_golden
from ..dedup_resolve.llm_judge import JUDGE_TIERS
from ..dedup_resolve.report import DuplicateReport
from ..logging import get_logger
from ..schema import DedupCandidateRow, DiscoveredCompanyRow

log = get_logger("dedup.workbench")

PENDING = "pending"
MERGED = "merged"
SEPARATED = "separated"
_VALID_STATUSES = frozenset({PENDING, MERGED, SEPARATED})

# 워크벤치에 노출하는 경계 티어 — 사다리가 못 가른 것만(auto·keep_both 제외).
WORKBENCH_TIERS = JUDGE_TIERS


class DedupConflict(Exception):
    """이미 결정됐거나 원장에서 사라져 적용할 수 없는 후보(동시성·재실행 백스톱)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def pair_id(key_a: str, key_b: str) -> str:
    """정렬된 쌍의 결정적 PK — 입력 순서와 무관하게 같은 쌍은 같은 id(멱등 upsert)."""
    a, b = sorted((key_a, key_b))
    return hashlib.sha1(f"{a}\x00{b}".encode()).hexdigest()  # sha1 hexdigest = 정확히 40자


def populate_candidates(session: Session, report: DuplicateReport) -> dict[str, int]:
    """리포트의 경계 후보를 ``dedup_candidate`` 에 멱등 적재한다.

    이미 결정(merged/separated)된 쌍은 건드리지 않아 사람 결정이 재적재로 부활하지 않는다.
    pending 행은 최신 티어·점수·LLM 판정으로 갱신한다. ``{created, updated, skipped}`` 반환.
    """
    judged_by_pair: dict[str, object] = {}
    for jp in report.judged:
        c = jp.candidate
        judged_by_pair[pair_id(c.key_a, c.key_b)] = jp.verdict

    created = updated = skipped = 0
    for cand in report.candidates:
        if cand.tier not in WORKBENCH_TIERS:
            continue  # auto(자동해소)·keep_both(결론)는 워크벤치 대상 아님.
        a, b = sorted((cand.key_a, cand.key_b))
        cid = pair_id(a, b)
        row = session.get(DedupCandidateRow, cid)
        if row is not None and row.status != PENDING:
            skipped += 1  # 사람 결정 보존 — 부활 금지.
            continue
        verdict = judged_by_pair.get(cid)
        if row is None:
            row = DedupCandidateRow(id=cid, key_a=a, key_b=b)
            session.add(row)
            created += 1
        else:
            updated += 1
        row.tier = cand.tier
        row.name_score = cand.name_score
        row.reason = cand.reason
        if verdict is not None:
            row.llm_same = verdict.same
            row.llm_confidence = verdict.confidence
            row.llm_reason = verdict.reason
            row.llm_model = verdict.model
    session.flush()
    return {"created": created, "updated": updated, "skipped": skipped}


def _company_info(session: Session, keys: list[str]) -> dict[str, dict]:
    """주어진 canonical_key 들의 표시 정보(이름·국가·도메인·머지상태)를 한 번에 적재."""
    if not keys:
        return {}
    rows = session.execute(
        select(
            DiscoveredCompanyRow.canonical_key,
            DiscoveredCompanyRow.name,
            DiscoveredCompanyRow.country,
            DiscoveredCompanyRow.domain,
            DiscoveredCompanyRow.duplicate_of,
        ).where(DiscoveredCompanyRow.canonical_key.in_(keys))
    ).all()
    return {
        key: {"name": name, "country": country or "", "domain": domain, "duplicate_of": dup}
        for key, name, country, domain, dup in rows
    }


def _to_item(row: DedupCandidateRow, info: dict[str, dict]) -> dict:
    """후보 행 + 양쪽 회사정보 → API 응답용 dict."""
    a = info.get(row.key_a, {})
    b = info.get(row.key_b, {})
    return {
        "id": row.id,
        "key_a": row.key_a,
        "key_b": row.key_b,
        "name_a": a.get("name"),
        "name_b": b.get("name"),
        "country": a.get("country") or b.get("country") or "",
        "domain_a": a.get("domain"),
        "domain_b": b.get("domain"),
        "tier": row.tier,
        "name_score": row.name_score,
        "reason": row.reason,
        "llm_same": row.llm_same,
        "llm_confidence": row.llm_confidence,
        "llm_reason": row.llm_reason,
        "llm_model": row.llm_model,
        "status": row.status,
        "survivor_key": row.survivor_key,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        # 한쪽이라도 원장에서 사라졌으면 stale — UI 가 머지 비활성·새로고침 유도.
        "stale": row.key_a not in info or row.key_b not in info,
    }


def list_candidates(
    session: Session,
    *,
    status: str | None = PENDING,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """중복후보 목록(양쪽 회사정보 포함) + 총건수. ``status=None`` 이면 전체."""
    if status is not None and status not in _VALID_STATUSES:
        raise ValueError(f"허용되지 않은 상태: {status}")
    base = select(DedupCandidateRow)
    count_stmt = select(func.count()).select_from(DedupCandidateRow)
    if status is not None:
        base = base.where(DedupCandidateRow.status == status)
        count_stmt = count_stmt.where(DedupCandidateRow.status == status)
    total = session.execute(count_stmt).scalar_one()
    # 결정 우선순위: 점수 높은 순(가장 의심스러운 쌍 먼저), id 로 안정 정렬.
    rows = list(
        session.execute(
            base.order_by(DedupCandidateRow.name_score.desc(), DedupCandidateRow.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    keys = [k for r in rows for k in (r.key_a, r.key_b)]
    info = _company_info(session, keys)
    return {
        "items": [_to_item(r, info) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def summary(session: Session) -> dict:
    """상태별 후보 건수 요약(워크벤치 대시보드용)."""
    rows = session.execute(
        select(DedupCandidateRow.status, func.count()).group_by(DedupCandidateRow.status)
    ).all()
    by_status = {s: n for s, n in rows}
    return {
        "pending": by_status.get(PENDING, 0),
        "merged": by_status.get(MERGED, 0),
        "separated": by_status.get(SEPARATED, 0),
        "total": sum(by_status.values()),
    }


def _get_pending(session: Session, candidate_id: str) -> DedupCandidateRow | None:
    # 같은 후보의 동시 결정(merge↔separate)을 직렬화한다 — PG 행잠금으로 두 직원이 같은
    # 쌍을 동시에 처리해도 한쪽만 pending 을 보고 나머지는 status!=pending → DedupConflict.
    # (review.py·admin.py 와 동일 계약. SQLite 는 행잠금이 없어 무시되나 운영은 PG 필수.)
    row = session.execute(
        select(DedupCandidateRow)
        .where(DedupCandidateRow.id == candidate_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.status != PENDING:
        raise DedupConflict("이미 처리된 후보입니다. 새로고침 후 다시 확인하세요.")
    return row


def decide_merge(session: Session, candidate_id: str, *, decided_by: str) -> dict | None:
    """동일 확정 — 골든레코드 survivorship 으로 두 행을 머지(duplicate_of 기록)한다.

    없으면 None, 이미 결정됐으면 :class:`DedupConflict`. 한쪽이 원장에서 사라졌거나 이미
    다른 머지에 흡수돼 적용이 0건이면 :class:`DedupConflict`(상태 미변경 — 재적재가 정리).
    """
    row = _get_pending(session, candidate_id)
    if row is None:
        return None
    members = load_cluster_members(session, [row.key_a, row.key_b])
    if len(members) < 2:
        raise DedupConflict("원장에서 사라진 행이 있어 머지할 수 없습니다. 새로고침하세요.")
    golden = resolve_golden(members.values(), basis="human")
    applied = apply_golden(
        session,
        golden,
        merged_by=decided_by,
        merge_reason=f"워크벤치 사람확정({row.tier})",
    )
    if applied == 0:
        raise DedupConflict("이미 다른 머지에 흡수된 행입니다. 새로고침하세요.")
    row.status = MERGED
    row.survivor_key = golden.survivor_key
    row.decided_by = decided_by
    row.decided_at = _utcnow()
    session.flush()
    info = _company_info(session, [row.key_a, row.key_b])
    return _to_item(row, info)


def decide_separate(session: Session, candidate_id: str, *, decided_by: str) -> dict | None:
    """분리(둘 다 유지) — 동일기업 아님으로 영속 표시(재적재 시 부활 안 함)."""
    row = _get_pending(session, candidate_id)
    if row is None:
        return None
    row.status = SEPARATED
    row.decided_by = decided_by
    row.decided_at = _utcnow()
    session.flush()
    info = _company_info(session, [row.key_a, row.key_b])
    return _to_item(row, info)
