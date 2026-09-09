# 1회성(2026-08-26, git 미반영): JP 상장사(EDINET+Wikidata 도메인 백필분) 승격.
"""퇴역한 backfill_promote_domained.py 의 pipeline.promote 판 — cli segment-run 루프 미러.

트랙 S 활성 잡이 있으면 중단(중복 과금 방지 — 정식 잠금 대신 사전 점검).
커서는 파일로 영속(promote_batch 반환 후에만 기록 — 선기록 금지 계약 준수).

사용: python scripts/promote_jp_oneoff.py [커서파일] [batch] [workers]
"""

from __future__ import annotations

import pathlib
import sys

from sqlalchemy import text

from leadcrawler.config import get_settings
from leadcrawler.logging import configure_logging
from leadcrawler.pipeline.promote import (
    PromoteRun,
    _load_domain_guards,
    count_promote_targets,
    promote_batch,
)
from leadcrawler.storage.db import get_sessionmaker


def main() -> int:
    configure_logging()
    cursor_path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "logs/promote-cursor-jp.txt")
    batch = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    settings = get_settings()
    sm = get_sessionmaker(settings)

    with sm() as s:
        active = s.execute(
            text("select count(*) from backfill_job where track='S' and status='running'")
        ).scalar()
    if active:
        print("[jp-oneoff] 활성 트랙 S 잡 존재 — 중복 과금 방지 위해 중단", flush=True)
        return 3

    after = cursor_path.read_text().strip() if cursor_path.exists() else ""
    remaining = count_promote_targets(sm, ["JP"], listed="listed")
    print(f"[jp-oneoff] 대상 {remaining} (batch={batch} workers={workers} after={after!r})",
          flush=True)

    run = PromoteRun.open(settings)
    try:
        with sm() as s:
            guards = _load_domain_guards(s)
        done = tot_p = tot_e = tot_f = 0
        while True:
            rows, last_key, promoted, emails, failed = promote_batch(
                settings, sm, run=run, after=after, limit=batch, workers=workers,
                guards=guards, countries=["JP"], listed="listed", stall_exit_s=900,
            )
            if rows == 0:
                break
            after = last_key
            cursor_path.write_text(after)  # 반환 후 기록(선기록 금지 계약).
            done += rows
            tot_p += promoted
            tot_e += emails
            tot_f += failed
            print(f"[jp-oneoff] {done}/{remaining} | 승격 {tot_p} | 이메일 {tot_e}"
                  f" | 실패 {tot_f}", flush=True)
    finally:
        run.close()
    print(f"[jp-oneoff] 완료 — 승격 {tot_p} · 이메일 {tot_e} · 실패 {tot_f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
