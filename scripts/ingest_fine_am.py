# 1회성(2026-09-01, git 미반영): 금감원 FINE 자산운용사 명부(512곳) 대조·적재.
"""FINE(fine.fss.or.kr 제도권 금융회사 조회 › 자산운용사) 스크레이프 CSV 를 DB 와 대조해
필요한 것만 들고 온다. 기본은 리포트만(무변경), ``--apply`` 일 때만 쓴다.

행별 처리(이름 정규화 / 도메인 / 영문명 순 매칭):
  ① company 매칭 → 업종을 '증권·자산운용'으로 교정(등록처 사실 — 추측 아님) +
     전화 없으면 FINE 대표전화를 contact(api, 0.9)로(파이프라인 등록처 폴백과 동일 표기) +
     FINE 이메일이 있으면 role 분류(HR·언론 배제)·검증 후 contact+email_validation+큐 후보 추가.
  ② discovered_company 만 있음 → 업종 교정 + 빈 domain/phone/address/name_eng 채움
     (도메인이 생기면 promote 대상, 없으면 backfill-resolve-domains 대상이 된다).
  ③ 부재 → discovered_company 신규(source=import, registry=fine_fss). 도메인 없는 행도 넣는다
     (ingest_research_seed 와 달리 — 이번엔 해석 러너를 뒤이어 돌린다).
제약①: 기존 canonical_key/도메인 점유 행은 재추출하지 않는다(매칭되면 갱신만).
제약②: company 는 직접 만들지 않는다 — 실존 게이트는 resolve/promote 가 그대로 수행.

사용: python scripts/ingest_fine_am.py <fine_am_detail.csv> [--apply]
ponytail: 일회성이라 CLI 커맨드화 안 함. 이름 정규화는 법인 접미사 제거+비문자 제거 수준.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter

from sqlalchemy import select

from leadcrawler.config import Settings
from leadcrawler.dedup import canonical_key, normalize_domain
from leadcrawler.emailrules import classify_role, is_accepted
from leadcrawler.models import ContactType, ExtractMethod
from leadcrawler.region import region_from_address
from leadcrawler.schema import CompanyRow, ContactRow, DiscoveredCompanyRow, EmailValidationRow
from leadcrawler.storage.db import get_sessionmaker
from leadcrawler.storage.repository import contact_id_for
from leadcrawler.storage.review import (
    ReviewQueueRow,
    candidate_values_of,
    enqueue_email_review,
    review_id_for,
)
from leadcrawler.verify.email_validator import EmailValidator

LABEL = "증권·자산운용"
_SUFFIX = re.compile(r"주식회사|유한회사|유한책임회사|\(주\)|㈜|co\.?,?\s*ltd\.?|inc\.?|limited|corporation|corp\.?")
_NONWORD = re.compile(r"[\s\W_]+")


def nname(s: str | None) -> str:
    return _NONWORD.sub("", _SUFFIX.sub("", (s or "").lower()))


def clean_phone(raw: str) -> str:
    # "02-6324-6881~5" / "02-1234-5678, 02-…" → 첫 번호만.
    return re.split(r"[~,/]", raw.strip())[0].strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    settings = Settings()
    if settings.dry_run:
        raise SystemExit("DRY_RUN=true — 라이브 DB 작업 아님. 중단.")
    fine = list(csv.DictReader(open(args.csv, encoding="utf-8")))
    sm = get_sessionmaker(settings)
    validator = EmailValidator(settings)
    stats: Counter[str] = Counter()
    with sm() as s:
        cos = s.execute(select(CompanyRow).where(CompanyRow.country == "KR")).scalars().all()
        dcs = s.execute(
            select(DiscoveredCompanyRow).where(DiscoveredCompanyRow.country == "KR")
        ).scalars().all()
        co_by_name: dict[str, CompanyRow] = {}
        co_by_dom: dict[str, CompanyRow] = {}
        for c in cos:
            co_by_name.setdefault(nname(c.name), c)
            d = normalize_domain(c.homepage)
            if d:
                co_by_dom.setdefault(d, c)
        dc_by_key = {d.canonical_key: d for d in dcs}
        dc_by_name: dict[str, DiscoveredCompanyRow] = {}
        dc_by_dom: dict[str, DiscoveredCompanyRow] = {}
        for d in dcs:
            dc_by_name.setdefault(nname(d.name), d)
            nd = normalize_domain(d.domain)
            if nd:
                dc_by_dom.setdefault(nd, d)

        for f in fine:
            name = f["name"].strip()
            key = nname(name)
            dom = normalize_domain(f["homepage"]) if f["homepage"] else None
            phone = clean_phone(f["phone"]) if f["phone"] else ""
            emails = [e.strip() for e in re.split(r"[,;\s]+", f["email"] or "") if "@" in e]
            address = (f.get("address") or "").strip() or None
            name_eng = (f.get("name_eng") or "").strip() or None

            co = co_by_name.get(key) or (co_by_dom.get(dom) if dom else None)
            if co is None and name_eng:
                co = co_by_name.get(nname(name_eng))
            dc = (
                dc_by_key.get(co.canonical_key) if co is not None else None
            ) or dc_by_name.get(key) or (dc_by_dom.get(dom) if dom else None)

            # --- discovered_company 갱신/신규 (①②③ 공통: 원장은 항상 채운다)
            if dc is None:
                stats["missing"] += 1
                dc = DiscoveredCompanyRow(
                    canonical_key=canonical_key(domain=dom, name=name, country="KR"),
                    name=name, country="KR", industry=LABEL, listed="unknown",
                    registry="fine_fss", registry_id=f["fin_co_slno"], domain=dom,
                    source="import", phone=phone or None, address=address,
                    region=region_from_address("KR", address), name_eng=name_eng,
                )
                if dc.canonical_key in dc_by_key:
                    stats["missing_key_collision"] += 1  # 이름키 충돌 — 재추출 금지(제약①).
                    continue
                s.add(dc)
                dc_by_key[dc.canonical_key] = dc
                stats["dc_inserted"] += 1
                if dom:
                    stats["dc_inserted_with_domain"] += 1
            else:
                stats["company" if co is not None else "discovered_only"] += 1
                if dc.industry != LABEL:
                    dc.industry = LABEL
                    stats["dc_relabeled"] += 1
                if not dc.domain and dom and dom not in dc_by_dom and dom not in co_by_dom:
                    dc.domain = dom
                    dc_by_dom[dom] = dc
                    stats["dc_domain_filled"] += 1
                if not dc.phone and phone:
                    dc.phone = phone
                    stats["dc_phone_filled"] += 1
                if not dc.address and address:
                    dc.address = address
                    dc.region = dc.region or region_from_address("KR", address)
                    stats["dc_address_filled"] += 1
                if not dc.name_eng and name_eng:
                    dc.name_eng = name_eng
                    stats["dc_name_eng_filled"] += 1
                if not dc.registry:
                    dc.registry, dc.registry_id = "fine_fss", f["fin_co_slno"]

            if co is None:
                continue

            # --- ① company 교정·보강
            if co.industry != LABEL:
                stats[f"co_relabeled_from:{co.industry or '(빈값)'}"] += 1
                stats["co_relabeled"] += 1
                co.industry = LABEL
            contacts = s.execute(
                select(ContactRow).where(ContactRow.company_id == co.id)
            ).scalars().all()
            have_phone = any(c.type == ContactType.PHONE.value for c in contacts)
            have_emails = {c.value.lower() for c in contacts if c.type == ContactType.EMAIL.value}
            if phone and not have_phone:
                s.add(ContactRow(
                    id=contact_id_for(co.id, ContactType.PHONE.value, phone), company_id=co.id,
                    type=ContactType.PHONE.value, value=phone,
                    extract_method=ExtractMethod.API.value, confidence=0.9,
                ))
                stats["co_phone_added"] += 1
            new_emails: list[str] = []
            for em in emails:
                if em.lower() in have_emails:
                    stats["email_already"] += 1
                    continue
                role = classify_role(em)
                if not is_accepted(role):
                    stats[f"email_excluded:{role.value}"] += 1
                    continue
                v = validator.validate(em, co.homepage)
                stats[f"email_status:{v.status.value}"] += 1
                if v.status.value == "invalid":
                    continue
                cid = contact_id_for(co.id, ContactType.EMAIL.value, em)
                s.add(ContactRow(
                    id=cid, company_id=co.id, type=ContactType.EMAIL.value, value=em,
                    role=role.value, extract_method=ExtractMethod.API.value, confidence=0.9,
                ))
                s.flush()
                s.add(EmailValidationRow(
                    contact_id=cid, status=v.status.value, mx=v.mx, domain_match=v.domain_match,
                    smtp=v.smtp, provider=v.provider, checked_at=v.checked_at,
                ))
                new_emails.append(em)
                stats["co_email_added"] += 1
            if new_emails:
                rq = s.get(ReviewQueueRow, review_id_for(co.id, "email"))
                if rq is None or rq.status == "pending":
                    cands = (candidate_values_of(rq) if rq else []) + new_emails
                    enqueue_email_review(
                        s, co.id, cands, selected_default=(rq.selected if rq else None)
                    )
                    stats["queue_candidates_added"] += 1
                else:
                    stats[f"queue_left_alone:{rq.status}"] += 1

        if args.apply:
            s.commit()
        else:
            s.rollback()
    for k, v in sorted(stats.items()):
        print(f"{k}: {v}")
    print("적용 완료" if args.apply else "리포트만 — 적용하려면 --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
