# 1회성(2026-08-21, git 미반영): 직접 리서치한 KR 금융기관 시드를 발견 원장에 투입.
"""리서치 JSON({name, domain, note})을 discovered_company 에 넣는다 — PO 지시(수동 큐레이션).

제약① 준수: canonical_key 기존재·도메인 기점유(원장/company)면 스킵(재추출 금지).
제약②는 건드리지 않는다 — 실존 검증·승격은 promote 백필이 그대로 수행.

사용: python scripts/ingest_research_seed.py <industry라벨> <json경로> [<json경로>...]
"""

from __future__ import annotations

import json
import sys

from sqlalchemy import select, text

from leadcrawler.config import Settings
from leadcrawler.dedup import canonical_key, normalize_domain
from leadcrawler.schema import DiscoveredCompanyRow
from leadcrawler.sources.taxonomy import is_taxonomy_label
from leadcrawler.storage.db import get_sessionmaker


def main() -> int:
    label, paths = sys.argv[1], sys.argv[2:]
    if not is_taxonomy_label(label):
        print(f"택소노미 밖 라벨: {label}", flush=True)
        return 1
    rows: list[dict] = []
    for p in paths:
        rows.extend(json.load(open(p, encoding="utf-8")))

    sm = get_sessionmaker(Settings())
    inserted = skipped_key = skipped_domain = no_domain = 0
    with sm() as s:
        for r in rows:
            name = (r.get("name") or "").strip()
            dom = normalize_domain(r.get("domain"))
            if not dom:
                no_domain += 1  # 도메인 미확인 행은 안 넣는다(resolve 경로로도 안 보냄 — 소량이라).
                continue
            key = canonical_key(domain=dom, name=name, country="KR")
            if s.get(DiscoveredCompanyRow, key) is not None:
                skipped_key += 1
                continue
            taken = s.execute(
                select(DiscoveredCompanyRow.canonical_key).where(
                    DiscoveredCompanyRow.domain == dom
                )
            ).first() or s.execute(
                text("select id from company where homepage like :d"), {"d": f"%{dom}%"}
            ).first()
            if taken:
                skipped_domain += 1
                continue
            s.add(DiscoveredCompanyRow(
                canonical_key=key, name=name, country="KR", industry=label,
                source="import", domain=dom, listed="unknown",
            ))
            inserted += 1
        s.commit()
    print(
        f"[ingest] 라벨={label} 투입 {inserted} · 키중복 {skipped_key}"
        f" · 도메인기점유 {skipped_domain} · 도메인없음 {no_domain}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
