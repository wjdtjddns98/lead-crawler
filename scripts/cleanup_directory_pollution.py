"""디렉토리 도메인 오염 정리(2026-08-10 사고) — 일회성 복구 스크립트.

KR 도메인 백필이 기업정보 디렉토리(nicebizinfo·webify·sankun·38 등)를 공식 홈페이지로
오채택해 company 2만여 건 + discovered_company.domain 수만 건이 오염됐다(원인·수정은
같은 PR 의 fill/domain_resolver/promote 수정 참고). 이 스크립트는 이미 적재된 오염을
되돌린다 — 회사 식별정보(발견 원장)는 보존하고 오염 산출물만 걷어내 재해석 가능 상태로.

정리 규칙:
- 오염 도메인 = ① `_BLOCKLIST`(디렉토리·미디어·플랫폼 — 누구의 공식 도메인도 아님)
  ② 유효하지 않은 값('-', '없음', 점 없는 문자열) ③ **구조적 과공유** — 서로 다른
  회사 5곳 이상이 같은 homepage 도메인을 공유(공식 도메인은 구조적으로 공유 불가).
- company: ①②는 키 무관 전부 삭제(contact·review_queue 는 FK cascade). ③은
  **name: 키만** 삭제 — 해석 백필의 피해자(실측 95.8%). reg:/dom: 키는 정당한 도메인
  소유자일 수 있어 보존한다(잔여 오염은 리포트로만 표기).
- discovered_company: ①② 도메인은 dom: 키(디렉토리 사이트 자체가 회사로 발견된 행)면
  행 삭제, 그 외 키는 domain=NULL(재해석 대상으로 복귀). ③은 삭제된 name: 키 회사의
  발견행 + 미승격 name: 키 행의 domain=NULL.

기본은 dry-run(집계만 출력). `--apply` 시 백업 CSV 를 남기고 실제 삭제/NULL 처리한다.

사용:  python scripts/cleanup_directory_pollution.py [--apply] [--backup-dir logs/cleanup-백업]

ponytail: 일회성 복구라 CLI 커맨드화 안 함(backfill_promote_domained 와 같은 판단).
"""

from __future__ import annotations

import csv
import sys
import time
from collections import Counter
from pathlib import Path

from sqlalchemy import text

from leadcrawler.config import Settings
from leadcrawler.dedup import normalize_domain
from leadcrawler.sources.search import _BLOCKLIST
from leadcrawler.storage.db import get_sessionmaker

_SHARE_CAP = 5  # 같은 homepage 도메인을 공유하는 회사 수가 이 이상이면 구조적 오염.
_CHUNK = 1000


def _norm(homepage: str | None) -> str | None:
    return normalize_domain(homepage) if homepage else None


def _junk(dom: str | None, raw: str) -> bool:
    """정규화 실패·점 없는 값('-'·'없음' 등) — 도메인일 수 없는 쓰레기 값."""
    return dom is None or "." not in (dom or "") or "." not in raw


def _chunks(seq: list, n: int = _CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _dump_csv(path: Path, header: list[str], rows: list) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def main() -> int:
    apply = "--apply" in sys.argv
    bdir = Path(
        sys.argv[sys.argv.index("--backup-dir") + 1]
        if "--backup-dir" in sys.argv
        else f"logs/cleanup-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    settings = Settings()
    sm = get_sessionmaker(settings)

    with sm() as s:
        companies = s.execute(text(
            "select id, canonical_key, name, country, homepage from company"
            " where coalesce(homepage,'') <> ''"
        )).all()

        share = Counter()
        norm_by_id: dict[str, str | None] = {}
        for r in companies:
            d = _norm(r.homepage)
            norm_by_id[r.id] = d
            if d is not None:
                share[d] += 1
        shared = {d for d, n in share.items() if n >= _SHARE_CAP}

        bad_ids: list[str] = []
        bad_keys: list[str] = []
        reason: Counter = Counter()
        kept_residue = 0  # 구조적 오염 도메인 위의 reg:/dom: 키(보존 — 리포트만).
        for r in companies:
            d = norm_by_id[r.id]
            if d in _BLOCKLIST or _junk(d, r.homepage):
                bad_ids.append(r.id)
                bad_keys.append(r.canonical_key)
                reason["blocklist/junk"] += 1
            elif d in shared:
                if r.canonical_key.startswith("name:"):
                    bad_ids.append(r.id)
                    bad_keys.append(r.canonical_key)
                    reason["overshared(name:)"] += 1
                else:
                    kept_residue += 1

        # discovered_company 오염 — ①② 도메인 보유 행(승격 여부 무관).
        disc = s.execute(text(
            "select canonical_key, domain from discovered_company"
            " where coalesce(domain,'') <> ''"
        )).all()
        disc_null: list[str] = []  # domain=NULL 대상 키.
        disc_del: list[str] = []  # 행 삭제 대상 키(dom: 키 + 블록리스트 도메인).
        for r in disc:
            d = normalize_domain(r.domain)
            if d in _BLOCKLIST or _junk(d, r.domain):
                if r.canonical_key.startswith("dom:"):
                    disc_del.append(r.canonical_key)
                else:
                    disc_null.append(r.canonical_key)
            elif d in shared and r.canonical_key.startswith("name:"):
                disc_null.append(r.canonical_key)

        bad_set = set(bad_ids)
        rq = s.execute(text(
            "select status, count(*) from review_queue rq"
            " where rq.company_id = any(:ids) group by status"
        ), {"ids": bad_ids}).all() if bad_ids else []
        n_contact = s.execute(text(
            "select count(*) from contact where company_id = any(:ids)"
        ), {"ids": bad_ids}).scalar() if bad_ids else 0

        print(f"[cleanup] 오염 company {len(bad_ids)}건 (사유: {dict(reason)})")
        print(f"[cleanup]   ㄴ 삭제될 contact {n_contact}건, review_queue {dict(rq)}")
        print(f"[cleanup]   ㄴ 보존 잔여(reg:/dom: 키, 공유도메인 위): {kept_residue}건")
        print(f"[cleanup] discovered domain=NULL {len(disc_null)}건, 행 삭제 {len(disc_del)}건")
        if not apply:
            print("[cleanup] dry-run — 적용하려면 --apply")
            return 0

        # ── 백업(CSV) — 삭제 전 스냅샷 ─────────────────────────────────────
        bdir.mkdir(parents=True, exist_ok=True)
        _dump_csv(bdir / "company.csv", ["id", "canonical_key", "name", "country", "homepage"],
                  [(r.id, r.canonical_key, r.name, r.country, r.homepage)
                   for r in companies if r.id in bad_set])
        con_rows = []
        for chunk in _chunks(bad_ids):
            con_rows += s.execute(text(
                "select id, company_id, type, value, role from contact"
                " where company_id = any(:ids)"), {"ids": chunk}).all()
        _dump_csv(bdir / "contact.csv", ["id", "company_id", "type", "value", "role"], con_rows)
        rq_rows = []
        for chunk in _chunks(bad_ids):
            rq_rows += s.execute(text(
                "select id, company_id, field, status, assignee, selected, claimed_by"
                " from review_queue where company_id = any(:ids)"), {"ids": chunk}).all()
        _dump_csv(bdir / "review_queue.csv",
                  ["id", "company_id", "field", "status", "assignee", "selected", "claimed_by"],
                  rq_rows)
        _dump_csv(bdir / "discovered_nulled.csv", ["canonical_key"], [(k,) for k in disc_null])
        _dump_csv(bdir / "discovered_deleted.csv", ["canonical_key"], [(k,) for k in disc_del])
        print(f"[cleanup] 백업 완료 → {bdir}")

        # ── 적용 — company 삭제(cascade) → discovered 정리 ──────────────────
        for chunk in _chunks(bad_ids):
            s.execute(text("delete from company where id = any(:ids)"), {"ids": chunk})
        for chunk in _chunks(disc_null):
            s.execute(text(
                "update discovered_company set domain = null where canonical_key = any(:ks)"
            ), {"ks": chunk})
        for chunk in _chunks(disc_del):
            # dom: 키 디렉토리 행 — 아직 company 가 남아 참조하면 삭제하지 않는다(안전).
            s.execute(text(
                "delete from discovered_company d where d.canonical_key = any(:ks)"
                " and not exists (select 1 from company c where c.canonical_key = d.canonical_key)"
            ), {"ks": chunk})
        s.commit()
        print(f"[cleanup] 적용 완료 — company {len(bad_ids)}건 삭제,"
              f" discovered NULL {len(disc_null)}건, 삭제 {len(disc_del)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
