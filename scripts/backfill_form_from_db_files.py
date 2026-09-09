# 1회성(2026-08-21, git 미반영): import 원본 엑셀의 문의폼 유무 기입 + 폼 확정 처리.
"""원본 엑셀 '홈페이지 문의'(E열 O/X)를 승격 회사에 소급 기입한다 — PO 지시.

관례(PO 확인): 사람 검증분은 문의폼 유무만으로도 확정. 따라서
  · 폼 O(또는 J열 '사이트 내 문의 폼') → contact(type='form', value=홈페이지 URL) 기입
    (엑셀엔 폼 URL 이 없어 홈페이지 URL 로 기입 — 12컬럼 E열 클릭 이동 계약 충족)
  · 그중 이메일 없는 회사 → 큐 행(후보 0) confirmed 로 등록(확정분 집계 포함)
멱등: 기존 form contact·큐 상태는 건너뛰거나 보존.

사용: python scripts/backfill_form_from_db_files.py [--apply]
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text

from ingest_db_excel_fastpath import iter_std_rows
from leadcrawler.config import Settings
from leadcrawler.dedup import canonical_key, normalize_domain
from leadcrawler.schema import ContactRow, ReviewQueueRow
from leadcrawler.storage.db import get_sessionmaker
from leadcrawler.storage.repository import company_id_for, contact_id_for
from leadcrawler.storage.review import CONFIRMED, enqueue_email_review


def _has_form(r: dict) -> bool:
    return r["form"].strip().upper() == "O" or "문의" in r["email_ok"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=r"C:\Users\WSCOPY\Desktop\DB")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    sm = get_sessionmaker(Settings())
    st = {"form_rows": 0, "contact_added": 0, "queue_confirmed": 0,
          "not_promoted": 0, "already": 0}
    with sm() as s:
        for path in sorted(glob.glob(os.path.join(args.dir, "*.xls[xm]"))):
            for r in iter_std_rows(path):
                if not _has_form(r):
                    continue
                dom = normalize_domain(r["site"])
                if not dom and not r["name"]:
                    continue
                try:
                    key = canonical_key(domain=dom, name=r["name"], country=r["country"] or "KR")
                except ValueError:
                    continue
                st["form_rows"] += 1
                cid = company_id_for(key)
                co = s.execute(text(
                    "select id, homepage from company where id = :i"), {"i": cid}).first()
                if co is None:
                    st["not_promoted"] += 1  # K=X 등으로 승격 제외된 행 — 폼 기입도 스킵.
                    continue
                form_url = co.homepage or (f"https://{dom}" if dom else "")
                if not form_url:
                    continue
                ct_id = contact_id_for(cid, "form", form_url)
                if s.get(ContactRow, ct_id) is None:
                    s.add(ContactRow(
                        id=ct_id, company_id=cid, type="form", value=form_url,
                        role="unknown", extract_method="existing_import", confidence=0.9,
                    ))
                    st["contact_added"] += 1
                else:
                    st["already"] += 1
                # 이메일 없는 회사만 폼 근거 확정 — 이메일 있는 행은 이미 confirmed 처리됨.
                has_email = s.execute(text(
                    "select 1 from contact where company_id=:i and type='email' limit 1"
                ), {"i": cid}).first() is not None
                if not has_email:
                    s.flush()
                    rid = enqueue_email_review(s, cid, [])
                    rq = s.get(ReviewQueueRow, rid)
                    if rq.status != CONFIRMED:
                        rq.status = CONFIRMED  # 확정 근거 = 사람 검증 문의폼(관례).
                        rq.selected = None
                        rq.selected_by_human = True
                        st["queue_confirmed"] += 1
        if args.apply:
            s.commit()
            print("[form-backfill] COMMIT")
        else:
            s.rollback()
            print("[form-backfill] DRY-RUN(롤백)")
    print(f"[form-backfill] {st}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
