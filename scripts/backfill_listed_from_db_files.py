# 1회성(2026-08-21, git 미반영): Desktop\DB 파일 카테고리로 상장여부 결정적 백필.
"""원본 엑셀 파일명이 상장/비상장을 이미 확정한다 — LLM·크롤 없이 d.listed 를 채운다.

  · '상장사 리스트'·'상장 법인' 계열 → listed
  · '비상장' 계열(비상장사 리스트, 기업개황(비상장)) → unlisted
  · 그 외(VC·벤처기업·생명과학·해외 기업 리스트) → 판정 불가, unknown 유지
현재 unknown/빈값인 행만 갱신한다(기존 확정값 보존 — DART 등 등록처 판정 우선).

사용: python scripts/backfill_listed_from_db_files.py [--apply]
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text

from leadcrawler.config import Settings
from leadcrawler.dedup import canonical_key, normalize_domain
from leadcrawler.storage.db import get_sessionmaker

# fastpath 인제스트와 동일 리더 재사용(표준 12컬럼 판별 포함).
from ingest_db_excel_fastpath import iter_std_rows  # noqa: E402  (scripts/ 직접 실행 전제)


def file_verdict(fname: str) -> str | None:
    if "비상장" in fname:
        return "unlisted"
    if "상장사" in fname or "상장 법인" in fname:
        return "listed"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=r"C:\Users\WSCOPY\Desktop\DB")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    sm = get_sessionmaker(Settings())
    stats = {"listed": 0, "unlisted": 0, "skip_known": 0, "no_row": 0}
    with sm() as s:
        for path in sorted(glob.glob(os.path.join(args.dir, "*.xls[xm]"))):
            verdict = file_verdict(os.path.basename(path))
            if verdict is None:
                continue
            for r in iter_std_rows(path):
                dom = normalize_domain(r["site"])
                if not dom and not r["name"]:
                    continue
                try:
                    key = canonical_key(
                        domain=dom, name=r["name"], country=r["country"] or "KR"
                    )
                except ValueError:
                    continue
                row = s.execute(text(
                    "select listed from discovered_company where canonical_key = :k"
                ), {"k": key}).first()
                if row is None:
                    stats["no_row"] += 1
                    continue
                if (row.listed or "unknown") not in ("", "unknown"):
                    stats["skip_known"] += 1
                    continue
                s.execute(text(
                    "update discovered_company set listed = :v where canonical_key = :k"
                ), {"v": verdict, "k": key})
                stats[verdict] += 1
        if args.apply:
            s.commit()
            print("[listed-backfill] COMMIT")
        else:
            s.rollback()
            print("[listed-backfill] DRY-RUN(롤백)")
    print(f"[listed-backfill] {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
