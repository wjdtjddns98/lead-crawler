"""미상(unknown) 상장여부 → 비상장(unlisted) 확정 백필 — P0.5 (PO 승인 2026-08-19).

세그먼트 필터 재고 확충: 작업자가 '비상장' 필터를 걸면 unknown 재고(수천 건)가 통째로
안 보인다. NPS(국민연금) 유입 기업 중 상장 근거 대조에 안 잡히는 기업만 보수적으로
unlisted 확정한다(2026-07-13 상장여부 오염 사고 전례 — 반대 방향 오기입 방지가 요건).

보수 판정(전부 만족해야 확정):
  ① d.source = 'nps' (NPS 출처만 — 타 소스 유입은 손대지 않는다)
  ② d.listed = 'unknown'
  ③ 정규화 이름이 상장군에 미매치:
     - dart_corp_cache 의 corp_cls ∈ {Y,K,N}(유가·코스닥·코넥스) 행의 name_norm
     - discovered_company 의 listed='listed' 행(거래소/DART 유입 상장행) 이름
     (동명이인 매치는 unknown 잔류 방향으로만 작용 — 오확정이 아니라 미확정으로 오차)
  ④ 정규화 이름이 비어 있지 않다(빈 이름은 판정 불가 — 잔류)

기본은 리포트만(무변경). ``--apply`` 일 때만 UPDATE 하고, 확정한 canonical_key 를
감사 CSV 로 남긴다(되돌리기 = 그 키들만 unknown 으로 역업데이트).

사용:  python scripts/backfill_confirm_unlisted.py [--apply] [--audit-file logs/....csv]

ponytail: 일회성 백필이라 CLI 커맨드화 안 함(backfill_promote_domained 과 같은 판단).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select, update

from leadcrawler.config import Settings
from leadcrawler.dedup import normalize_name
from leadcrawler.schema import DartCorpCacheRow, DiscoveredCompanyRow
from leadcrawler.storage.db import get_sessionmaker

# DART corp_cls → 상장군(sources/dart._LISTED_CLS 와 동일 해석 — Y 유가·K 코스닥·N 코넥스).
_LISTED_CORP_CLS = {"Y", "K", "N"}


def _listed_name_norms(session) -> set[str]:  # noqa: ANN001 (Session)
    """상장군 정규화 이름 집합 — DART 캐시 상장 corp + 원장 상장행."""
    norms: set[str] = set()
    for name_norm, info in session.execute(
        select(DartCorpCacheRow.name_norm, DartCorpCacheRow.info).where(
            DartCorpCacheRow.info.is_not(None),
            DartCorpCacheRow.name_norm.is_not(None),
            DartCorpCacheRow.status == "000",  # 정상건만(비정상 status 에 info 없음 불변식 방어).
        )
    ):
        try:
            cls = str(json.loads(info).get("corp_cls", ""))
        except (ValueError, TypeError):
            continue  # 원문 파손 행 — 상장 근거로 못 쓴다(보수: 매치 집합에서 제외).
        if cls in _LISTED_CORP_CLS:
            norms.add(name_norm)
    for (name,) in session.execute(
        select(DiscoveredCompanyRow.name).where(DiscoveredCompanyRow.listed == "listed")
    ):
        n = normalize_name(name or "")
        if n:
            norms.add(n[:255])
    return norms


def main() -> int:
    ap = argparse.ArgumentParser(description="unknown→unlisted 확정 백필(보수 판정, P0.5)")
    ap.add_argument("--apply", action="store_true", help="실제 UPDATE 수행(기본=리포트만)")
    ap.add_argument("--audit-file", default="",
                    help="확정 키 감사 CSV 경로(기본 logs/confirm-unlisted-<n>건.csv)")
    args = ap.parse_args()

    settings = Settings()
    if settings.dry_run:
        print("DRY_RUN=true — 백필 무의미. 중단.", flush=True)
        return 1
    sm = get_sessionmaker(settings)

    with sm() as s:
        listed_norms = _listed_name_norms(s)
        print(f"[confirm-unlisted] 상장군 이름 {len(listed_norms):,}개 적재", flush=True)

        rows = s.execute(
            select(DiscoveredCompanyRow.canonical_key, DiscoveredCompanyRow.name).where(
                DiscoveredCompanyRow.listed == "unknown",
                DiscoveredCompanyRow.source == "nps",
            )
        ).all()
        confirm: list[str] = []
        held_match = held_noname = 0
        for key, name in rows:
            n = normalize_name(name or "")[:255]
            if not n:
                held_noname += 1
            elif n in listed_norms:
                held_match += 1
            else:
                confirm.append(key)
        print(
            f"[confirm-unlisted] 후보(unknown×nps) {len(rows):,} → 확정 {len(confirm):,}"
            f" | 잔류: 상장군 동명 {held_match:,} · 이름없음 {held_noname:,}",
            flush=True,
        )
        if not args.apply:
            print("[confirm-unlisted] 리포트만(무변경) — 적용은 --apply", flush=True)
            return 0

        applied: list[str] = []
        for i in range(0, len(confirm), 1000):
            chunk = confirm[i : i + 1000]
            # returning 으로 **실제 적용된** 키만 감사에 남긴다 — 경합으로 재확인 조건에서
            # 빠진 행이 감사에 섞이면 되돌리기가 무관 변경을 덮는다(리뷰 MED).
            applied.extend(
                s.execute(
                    update(DiscoveredCompanyRow)
                    .where(
                        DiscoveredCompanyRow.canonical_key.in_(chunk),
                        DiscoveredCompanyRow.listed == "unknown",  # 재확인 — 경합 안전.
                    )
                    .values(listed="unlisted")
                    .execution_options(synchronize_session=False)
                    .returning(DiscoveredCompanyRow.canonical_key)
                ).scalars()
            )
        s.commit()  # 단일 트랜잭션 — 중간 사망 시 전체 롤백이라 감사 파일도 불필요.
        audit = Path(args.audit_file or f"logs/confirm-unlisted-{len(applied)}건.csv")
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.write_text("\n".join(["canonical_key", *applied]) + "\n", encoding="utf-8")
        print(f"[confirm-unlisted] 확정 {len(applied):,}건 적용 — 감사: {audit}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
