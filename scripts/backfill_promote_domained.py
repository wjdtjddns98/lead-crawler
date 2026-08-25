"""도메인 보유 미승격 발견행 승격 백필 (일회성 회수 잡) — CLI 얇은 래퍼.

승격 로직(대상 SQL·도메인가드·워커별 enrich/existence/validate·_build_lead/_persist_lead)은
``leadcrawler.pipeline.promote`` 로 이관됐다(세그먼트 작업 큐 설계 §4·§6 PR1,
``docs/segment-jobs-design.md``). 이 스크립트는 그 API(``count_promote_targets``/
``promote_batch``)를 CLI 인자로 호출하는 배선·커서파일·정체감시 러너다.

사용:  python scripts/backfill_promote_domained.py [--workers 3] [--batch 200]
       [--country KR ...] [--industry '정보보안,게임'] [--exclude-industry ...]
       [--exclude-listed | --listed] [--cursor-file logs/promote-cursor.txt]
       [--stall-exit-secs 900]
       (workers 기본 3 — A/C 백필과 동시 구동 시 헤드리스 메모리 경합을 피하려 보수적.
        --industry 는 굶는 세그먼트 타겟 보충용 — 2026-08-19 P1, '미분류'=라벨 빈값 행.
        --cursor-file 지정 시 배치마다 마지막 키를 기록해 재기동(정체 종료 포함)이
        이어받는다. 정체 감시는 promote_batch 호출마다(배치 1회분) 새로 무장한다. 트랙 락은 아직
        없음 — PR2 예정, 그때까지 관리형 승격과 동시 실행 금지.)

ponytail: 세그먼트 잡 트랙 S(설계 §3)가 도입되면 이 스크립트는 1릴리스 유예 후 퇴역
(트랙 락은 아직 없음 — PR2 예정). 그전까지는 운영 러너(run-promote-*.ps1/.bat)가 계속 쓴다.
"""

from __future__ import annotations

import argparse
import sys
import time

from leadcrawler.config import Settings
from leadcrawler.pipeline.promote import (
    PromoteRun,
    _load_domain_guards,
    _split_multi,
    count_promote_targets,
    promote_batch,
)
from leadcrawler.storage.db import get_sessionmaker


def _listed_of(exclude_listed: bool, only_listed: bool) -> str:
    if only_listed:
        return "listed"
    if exclude_listed:
        return "unlisted"
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description="도메인 보유 미승격 발견행 승격 백필")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--country", action="append", default=None,
                    help="이 국가만(반복 지정) — 미지정=전세계")
    ap.add_argument("--industry", action="append", default=None,
                    help="이 업종만(반복·쉼표 병기 — 굶는 세그먼트 타겟 보충, '미분류'=빈값)")
    ap.add_argument("--exclude-industry", action="append", default=None,
                    help="이 업종 제외(반복·쉼표 병기)")
    listed_group = ap.add_mutually_exclusive_group()
    listed_group.add_argument("--exclude-listed", action="store_true",
                              help="상장 확정(listed='listed') 제외 — unknown 유지")
    listed_group.add_argument("--listed", action="store_true",
                              help="상장 확정(listed='listed')만 대상 — 상장사 세그먼트 타겟 보충")
    ap.add_argument("--cursor-file", default="",
                    help="배치 커서 파일 — 재기동이 이어받는다(빈값=미사용, 처음부터)")
    ap.add_argument("--stall-exit-secs", type=float, default=900.0,
                    help="무진행 이 초 경과 시 프로세스 종료(0=끔) — Playwright 행 복구")
    args = ap.parse_args()
    workers, batch = args.workers, args.batch
    countries = args.country or None
    filters = {
        "countries": countries,
        "industries": _split_multi(args.industry),
        "exclude_industries": _split_multi(args.exclude_industry),
        "listed": _listed_of(bool(args.exclude_listed), bool(args.listed)),
    }

    settings = Settings()
    if settings.dry_run:
        print("DRY_RUN=true — 백필 무의미. 중단.", flush=True)
        return 1
    sm = get_sessionmaker(settings)

    remaining = count_promote_targets(
        sm, countries, industries=filters["industries"],
        exclude_industries=filters["exclude_industries"], listed=filters["listed"],
    )
    with sm() as s:
        taken, overshared = _load_domain_guards(s)
    print(
        f"[promote] 대상 {remaining} 곳 (workers={workers} batch={batch}"
        f" country={countries or '전세계'} industry={filters['industries'] or '전체'}"
        f" exclude_industry={filters['exclude_industries'] or '없음'}"
        f" listed={filters['listed']}"
        f" | 도메인점유 {len(taken)} 과공유 {len(overshared)})",
        flush=True,
    )

    done = promoted = emails = failed = 0
    after = ""  # 커서 — 파일 지정 시 재기동(정체 종료 포함)이 이어받는다.
    if args.cursor_file:
        try:
            with open(args.cursor_file, encoding="utf-8") as f:
                after = f.read().strip()
        except OSError:
            pass
        if after:
            print(f"[promote] 파일 커서 재개: {after!r} 뒤부터", flush=True)
    t0 = time.monotonic()
    run = PromoteRun.open(settings)  # 런당 1회 — LLM 호출 상한·registry 체커를 배치 간 공유.
    try:
        while True:
            rows, after, promoted_b, emails_b, failed_b = promote_batch(
                settings, sm, run=run, after=after, limit=batch, workers=workers,
                guards=(taken, overshared), countries=countries,
                industries=filters["industries"],
                exclude_industries=filters["exclude_industries"], listed=filters["listed"],
                stall_exit_s=args.stall_exit_secs,
            )
            if rows == 0:
                break
            done += rows
            promoted += promoted_b
            emails += emails_b
            failed += failed_b
            if args.cursor_file:
                # 배치 persist 완료 후(promote_batch 반환 후)에만 기록 — 중간에 죽으면 같은
                # 배치를 다시 훑는다(승격분은 co.id is null 로 빠져 멱등). 선기록이면 미처리
                # 행이 커서 뒤로 영구 스킵된다(리뷰 HIGH).
                with open(args.cursor_file, "w", encoding="utf-8") as f:
                    f.write(after)
            el = time.monotonic() - t0
            rate = done / el if el else 0
            eta = (remaining - done) / rate / 60 if rate else 0
            print(
                f"[promote] {done}/{remaining} | 승격 {promoted} | 이메일 {emails} | "
                f"실패 {failed} | {rate:.1f}/s | ETA {eta:.0f}분",
                flush=True,
            )
    finally:
        run.close()
    print(f"[promote] 완료 — 처리 {done}, 승격 {promoted}, 이메일 {emails}, 실패 {failed}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
