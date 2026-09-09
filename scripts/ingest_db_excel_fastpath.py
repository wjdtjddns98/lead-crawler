# 1회성(2026-08-21, git 미반영): Desktop\DB 원본 엑셀(사람 검증분) 패스트패스 승격.
"""사람이 검증한 원본 엑셀을 재검증 크롤 없이 직접 승격한다 — PO 지시(2026-08-21).

원장 시드(import-existing)는 이메일 등을 버렸으므로 원본을 다시 읽어 통째로 옮긴다:
  · K열(사이트 실존)='X' → 승격 제외(원장 잔류 — 사람이 죽었다고 판정한 사이트)
  · 도메인 없음 → 제외(resolve 트랙 소관)
  · 이메일 있음 + J열(이메일 실존)='O' → contact + 큐 confirmed(확정분)
  · 이메일 있음 + J≠O → contact + 큐 pending(대기)
  · 이메일 없음 → 승격만(대기 — A-트랙 fill-emails 가 후속 채움)
멱등: 기존 company/contact/큐 행은 건너뛴다. 제약①은 canonical_key 로 그대로.

사용: python scripts/ingest_db_excel_fastpath.py [--dir "C:\\Users\\WSCOPY\\Desktop\\DB"] [--apply]
      (--apply 없으면 집계만 출력하는 드라이런)
"""

from __future__ import annotations

import argparse
import glob
import os

from openpyxl import load_workbook

from leadcrawler.config import Settings
from leadcrawler.dedup import canonical_key, normalize_domain
from leadcrawler.schema import CompanyRow, ContactRow, DiscoveredCompanyRow
from leadcrawler.storage.db import get_sessionmaker
from leadcrawler.storage.repository import company_id_for, contact_id_for
from leadcrawler.storage.review import CONFIRMED, PENDING, enqueue_email_review

# 표준 12컬럼 서식 헤더(앞 6개로 판별). 이 서식이 아닌 파일은 건너뛰고 이름을 보고한다.
_STD_HEAD = ("국가", "업체명", "연락처", "이메일")


def _norm(v) -> str:  # noqa: ANN001
    return str(v).strip() if v is not None else ""


def iter_std_rows(path: str):
    wb = load_workbook(path, read_only=True)
    try:
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            try:
                head = [_norm(c) for c in next(rows)]
            except StopIteration:
                continue
            if tuple(head[:4]) != _STD_HEAD:
                continue
            blanks = 0
            for r in rows:
                vals = [_norm(c) for c in r[:12]] + [""] * max(0, 12 - len(r))
                if not any(vals):
                    blanks += 1
                    if blanks > 200:  # importer 와 동일한 빈행 폭주 가드.
                        break
                    continue
                blanks = 0
                yield {
                    "country": vals[0], "name": vals[1], "phone": vals[2],
                    "email": vals[3], "form": vals[4], "site": vals[5],
                    "email_ok": vals[9], "site_ok": vals[10],
                }
    finally:
        wb.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=r"C:\Users\WSCOPY\Desktop\DB")
    ap.add_argument("--apply", action="store_true", help="미지정=드라이런(집계만)")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.dir, "*.xls[xm]")))
    sm = get_sessionmaker(Settings())
    st = {k: 0 for k in (
        "rows", "no_domain", "site_dead", "already_promoted", "promoted",
        "contact_email", "queue_confirmed", "queue_pending", "ledger_seeded",
    )}
    skipped_files: list[str] = []
    with sm() as s:
        for path in files:
            n_before = st["rows"]
            for r in iter_std_rows(path):
                st["rows"] += 1
                if not r["name"] and not r["site"]:
                    continue
                if r["site_ok"].upper() == "X":
                    st["site_dead"] += 1
                    continue
                dom = normalize_domain(r["site"])
                if not dom:
                    st["no_domain"] += 1
                    continue
                key = canonical_key(domain=dom, name=r["name"], country=r["country"] or "KR")
                if s.get(DiscoveredCompanyRow, key) is None:
                    # 시드 누락분(파일 추가분 등)도 원장 정합 유지 — 제약① 표식.
                    s.add(DiscoveredCompanyRow(
                        canonical_key=key, name=r["name"][:255], country=r["country"][:8],
                        source="import", domain=dom, listed="unknown",
                    ))
                    st["ledger_seeded"] += 1
                cid = company_id_for(key)
                if s.get(CompanyRow, cid) is not None:
                    st["already_promoted"] += 1
                    continue
                s.add(CompanyRow(
                    id=cid, canonical_key=key, name=r["name"][:255] or dom,
                    country=r["country"][:8], industry="",
                    homepage=f"https://{dom}", is_active=True,
                    existence_confidence=0.9,  # 사람 검증분 신뢰(패스트패스 근거).
                    site_alive=True,
                ))
                st["promoted"] += 1
                email = r["email"].lower()
                if email and "@" in email:
                    ct_id = contact_id_for(cid, "email", email)
                    if s.get(ContactRow, ct_id) is None:
                        s.add(ContactRow(
                            id=ct_id, company_id=cid, type="email", value=email,
                            role="ir", extract_method="existing_import", confidence=0.9,
                        ))
                        st["contact_email"] += 1
                    s.flush()
                    rid = enqueue_email_review(s, cid, [email])
                    from leadcrawler.schema import ReviewQueueRow

                    rq = s.get(ReviewQueueRow, rid)
                    if r["email_ok"].upper() == "O":
                        rq.status = CONFIRMED  # 확정분 — 사람이 실존 O 판정.
                        rq.selected = email
                        rq.selected_by_human = True
                        st["queue_confirmed"] += 1
                    else:
                        rq.status = PENDING  # 대기 — 미확정은 큐에서 사람이 본다.
                        st["queue_pending"] += 1
                if st["promoted"] % 500 == 0:
                    s.flush()
            if st["rows"] == n_before:
                skipped_files.append(os.path.basename(path))
        if args.apply:
            s.commit()
            print("[fastpath] COMMIT")
        else:
            s.rollback()
            print("[fastpath] DRY-RUN(롤백) — --apply 로 실제 반영")
    print(f"[fastpath] {st}")
    if skipped_files:
        print(f"[fastpath] 비표준 서식 스킵 파일 {len(skipped_files)}: {skipped_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
