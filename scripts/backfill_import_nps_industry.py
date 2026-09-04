"""기존 임포트 행 업종 백필 ③ — NPS 스냅샷 이름 매칭(무과금, 2026-08-20 PO 지시).

②(엑셀 구분)·①(홈페이지 LLM)를 거치고도 남는 미승격·미분류 import 행은 홈페이지가
없거나 죽어 LLM 재료가 없다. 대부분 KR 기업이라 국민연금 스냅샷(nps_workplace)에 같은
회사가 업종코드와 함께 있을 확률이 높다 — 정규화 이름으로 조인해 업종코드 →
ksic_industry_map(코드→라벨, LLM 기구축) 라벨을 무과금으로 부착한다.

보수 규칙(오라벨 방지 — 2026-07-14 코드 오라벨 사고 계보):
  · 동명 사업장이 여러 업종코드로 갈리면, 그 코드들의 라벨이 **전부 같을 때만** 채택
    (하나라도 다르거나 미매핑이면 스킵 — 동명이인 방향 오차는 미분류 잔류로만)
  · 라벨이 '미분류'면 부착 안 함(정보 없음)
  · UPDATE 시 industry 가 여전히 빈값/미분류인지 재확인(동시 가동 중인 ①과 경합 안전)
멱등: 라벨이 붙은 행은 다음 실행 대상에서 빠진다.

사용:  python scripts/backfill_import_nps_industry.py [--report] [--target import|company]
  --target company (2026-08-26): 홈페이지 LLM 백필을 두 번 거치고도 남은 company 미분류
  (본문 확보 불가 사이트)에 같은 규칙을 적용 — KR·활성·업종이 빈값/미분류/'기타 제조'인 행.
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from sqlalchemy import text

from leadcrawler.config import Settings
from leadcrawler.dedup import normalize_name
from leadcrawler.sources.taxonomy import AMBIGUOUS_LABELS, UNCLASSIFIED
from leadcrawler.storage.db import get_sessionmaker


def main() -> int:
    ap = argparse.ArgumentParser(description="import 행 업종 백필 — NPS 이름 매칭(무과금)")
    ap.add_argument("--report", action="store_true", help="집계만(무변경)")
    ap.add_argument("--target", choices=("import", "company"), default="import",
                    help="import=미승격 import 행(기본) / company=KR 활성 company 미분류 행")
    args = ap.parse_args()
    ambiguous = ["", *sorted(AMBIGUOUS_LABELS)]  # 빈값·미분류·기타 제조 — company 대상 판정용.

    settings = Settings()
    if settings.dry_run:
        print("DRY_RUN=true — 라이브 원장 백필 무의미. 중단.", flush=True)
        return 1
    sm = get_sessionmaker(settings)
    with sm() as s:
        code_label = {
            c: lb for c, lb in s.execute(text(
                "select industry_code, taxonomy_label from ksic_industry_map"
            ))
        }
        # 정규화 이름 → 라벨 후보 집합(코드를 라벨로 접은 뒤 판정 — 같은 라벨로 모이면 채택).
        name_labels: dict[str, set[str]] = defaultdict(set)
        for name, code in s.execute(text(
            "select name, industry_code from nps_workplace"
            " where pending = false and coalesce(industry_code,'') <> ''"
        )):
            n = normalize_name(name or "")
            if n:
                name_labels[n].add(code_label.get(code, ""))
        resolved = {
            n: labels.pop() for n, labels in name_labels.items()
            if len(labels) == 1 and "" not in labels and UNCLASSIFIED not in labels
        }
        print(f"[nps-match] NPS 이름 {len(name_labels):,} 중 단일 라벨 확정 {len(resolved):,}",
              flush=True)

        if args.target == "company":
            rows = s.execute(text(
                "select id, name from company where is_active and country='KR'"
                " and coalesce(industry,'') = any(:amb)"
            ), {"amb": ambiguous}).all()
        else:
            rows = s.execute(text(
                "select d.canonical_key, d.name from discovered_company d"
                " left join company co on co.canonical_key = d.canonical_key"
                " where d.source='import' and co.id is null"
                " and coalesce(d.industry,'') in ('', :unc)"
            ), {"unc": UNCLASSIFIED}).all()
        by_label: dict[str, list[str]] = defaultdict(list)
        for key, name in rows:
            label = resolved.get(normalize_name(name or ""))
            # company 대상은 catch-all('기타 제조') 부착 금지 — 정보량 없이 라벨만 바뀌고
            # 다음 실행에서 또 대상으로 잡힌다(멱등 약속 위반).
            if label and not (args.target == "company" and label in AMBIGUOUS_LABELS):
                by_label[label].append(key)
        planned = sum(len(v) for v in by_label.values())
        print(f"[nps-match] 대상 {len(rows):,} 중 매칭 {planned:,}", flush=True)
        for lb, keys in sorted(by_label.items(), key=lambda kv: -len(kv[1]))[:12]:
            print(f"  {lb}: {len(keys):,}", flush=True)
        if args.report:
            print("[nps-match] 리포트만(무변경)", flush=True)
            return 0

        applied = 0
        for lb, keys in by_label.items():
            if args.target == "company":
                res = s.execute(text(
                    "update company set industry = :lb where id = any(:keys)"
                    " and coalesce(industry,'') = any(:amb) and industry is distinct from :lb"
                ), {"lb": lb, "keys": keys, "amb": ambiguous})  # LLM 백필과 동시 가동 경합 안전.
            else:
                res = s.execute(text(
                    "update discovered_company set industry = :lb"
                    " where canonical_key = any(:keys)"
                    " and coalesce(industry,'') in ('', :unc)"  # ①과 동시 가동 경합 안전.
                ), {"lb": lb, "keys": keys, "unc": UNCLASSIFIED})
            applied += res.rowcount or 0
        s.commit()
        print(f"[nps-match] 적용 {applied:,}행 완료", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
