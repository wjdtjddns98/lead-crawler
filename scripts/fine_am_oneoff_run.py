# 1회성(2026-09-01, git 미반영): FINE 자산운용사 적재분 해석→승격 러너 (KR/증권·자산운용 스코프).
"""``ingest_fine_am.py`` 가 넣은 발견행을 회사로 올린다 — 두 단계를 순서대로:
  ① resolve: 도메인 없는 KR/증권·자산운용 발견행 → 네이버/Serper 로 도메인 해석 + 실존이면 승격
     (``pipeline.fill.resolve_batch`` — CLI ``backfill-resolve-domains`` 와 같은 코어).
  ② promote: 도메인은 있는데 미승격인 KR/증권·자산운용 발견행 재승격 시도
     (``pipeline.promote.promote_batch`` — ``promote_jp_oneoff.py`` 미러, 커서 파일 영속).
CLI 대신 코어를 직접 부르는 이유: CLI 는 트랙 C 잠금을 잡는데 지금 도는 C 잡(KR, 증권·자산운용
**제외** 스코프)과 대상이 겹치지 않는다 — 과금 이중화 없음. 겹침은 아래서 backfill_job 로 확인.
사용: python scripts/fine_am_oneoff_run.py [batch] [workers] [--promote-only]
"""

from __future__ import annotations

import pathlib
import sys

from sqlalchemy import text

from leadcrawler.config import get_settings
from leadcrawler.logging import configure_logging
from leadcrawler.pipeline.fill import count_resolve_targets, resolve_batch
from leadcrawler.pipeline.promote import (
    PromoteRun,
    _load_domain_guards,
    count_promote_targets,
    promote_batch,
)
from leadcrawler.storage.db import get_sessionmaker

COUNTRY, LABEL = "KR", "증권·자산운용"


def _overlapping_jobs(sm) -> list[str]:  # noqa: ANN001
    """실행 중 backfill_job 중 KR/증권·자산운용 을 건드릴 수 있는 잡 id."""
    hits = []
    with sm() as s:
        rows = s.execute(text(
            "select id, track, countries, industries, exclude_industries "
            "from backfill_job where status='running'"
        )).all()
    for jid, track, countries, industries, excl in rows:
        cs = [c for c in (countries or "").split(",") if c]
        if cs and COUNTRY not in cs:
            continue
        inds = [i for i in (industries or "").split(",") if i]
        if inds and LABEL not in inds:
            continue
        if LABEL in (excl or "").split(","):
            continue
        hits.append(f"{jid}({track})")
    return hits


def main() -> int:
    configure_logging()
    promote_only = "--promote-only" in sys.argv  # 해석 패스를 이미 돌렸을 때 재실행용.
    nums = [a for a in sys.argv[1:] if not a.startswith("--")]
    batch = int(nums[0]) if len(nums) > 0 else 40
    workers = int(nums[1]) if len(nums) > 1 else 3
    cursor_path = pathlib.Path("logs/promote-cursor-fine-am.txt")
    settings = get_settings()
    if settings.dry_run:
        print("[fine-am] DRY_RUN=true — 중단", flush=True)
        return 2
    sm = get_sessionmaker(settings)
    overlap = _overlapping_jobs(sm)
    if overlap:
        print(f"[fine-am] 스코프 겹치는 실행 중 잡 {overlap} — 중복 과금 방지 위해 중단", flush=True)
        return 3

    run = PromoteRun.open(settings)
    try:
        # ① resolve
        pending = 0 if promote_only else count_resolve_targets(sm, [COUNTRY], industries=[LABEL])
        print(f"[fine-am] resolve 대상 {pending} (batch={batch} workers={workers})", flush=True)
        done = r_res = r_pro = 0
        # 미스 행은 domain 이 비어 있어 다음 배치 대상에 다시 잡힌다(시도 스탬프로 뒤로 밀릴 뿐)
        # → processed==0 은 영원히 안 온다. 대상 수만큼만 훑고 끝낸다(1패스).
        while done < pending:
            processed, resolved, promoted = resolve_batch(
                settings, sm, limit=min(batch, pending - done), workers=workers,
                countries=[COUNTRY], industries=[LABEL], stall_exit_s=300, run=run,
            )
            if processed == 0:
                break
            done += processed
            r_res += resolved
            r_pro += promoted
            print(f"[fine-am] resolve {done}/{pending} | 해석 {r_res} | 승격 {r_pro}", flush=True)
        print(f"[fine-am] resolve 완료 — 처리 {done} · 해석 {r_res} · 승격 {r_pro}", flush=True)

        # ② promote (도메인 보유 미승격 재시도)
        after = cursor_path.read_text().strip() if cursor_path.exists() else ""
        remaining = count_promote_targets(sm, [COUNTRY], industries=[LABEL])
        print(f"[fine-am] promote 대상 {remaining} (after={after!r})", flush=True)
        with sm() as s:
            guards = _load_domain_guards(s)
        p_done = tot_p = tot_e = tot_f = 0
        while True:
            rows, last_key, promoted, emails, failed = promote_batch(
                settings, sm, run=run, after=after, limit=batch, workers=workers,
                guards=guards, countries=[COUNTRY], industries=[LABEL], stall_exit_s=300,
            )
            if rows == 0:
                break
            after = last_key
            cursor_path.write_text(after)  # 반환 후 기록(선기록 금지 계약).
            p_done += rows
            tot_p += promoted
            tot_e += emails
            tot_f += failed
            print(f"[fine-am] promote {p_done}/{remaining} | 승격 {tot_p} | 이메일 {tot_e}"
                  f" | 실패 {tot_f}", flush=True)
    finally:
        run.close()
    print(f"[fine-am] 완료 — resolve 승격 {r_pro} · promote 승격 {tot_p} · 이메일 {tot_e}"
          f" · 실패 {tot_f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
