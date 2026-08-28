"""등록처 대표전화 → contact(phone) 일회성 백필 (2026-08-27, PO 지시 "전화번호도 수집").

파이프라인은 크롤로 전화를 못 잡으면 discovered_company.phone(NPS·EDGAR·FSC·DART) 으로
폴백하도록 바뀌었다(pipeline/run._build_lead). 그 이전에 적재된 회사는 폴백을 못 받았으므로
같은 규칙으로 1회 채운다: **phone 연락처가 하나도 없는 회사**에만, 등록처 값 그대로
(extract_method='api', confidence=0.9 — 파이프라인 폴백과 동일 표기) 삽입한다. 기존 phone 은
건드리지 않는다.

기본은 리포트만(무변경). ``--apply`` 일 때만 INSERT. 전제: 크롤/백필 러너가 도는 중엔
실행하지 않는다(같은 회사를 파이프라인이 동시에 저장하면 phone 2건이 될 수 있음 — PK 충돌은
없고 큐 표시는 신뢰도 순으로 결정적).

사용:  python scripts/backfill_phone_from_registry.py [--apply]

ponytail: 일회성 백필이라 CLI 커맨드화 안 함(backfill_confirm_unlisted 과 같은 판단).
"""

from __future__ import annotations

import argparse

from sqlalchemy import exists, select

from leadcrawler.config import Settings
from leadcrawler.models import ContactType, ExtractMethod
from leadcrawler.schema import CompanyRow, ContactRow, DiscoveredCompanyRow
from leadcrawler.storage.db import get_sessionmaker
from leadcrawler.storage.repository import contact_id_for


def backfill(session, *, apply: bool) -> list[tuple[str, str, str]]:  # noqa: ANN001 (Session)
    """대상 (company_id, country, phone) 을 돌려주고, ``apply`` 면 contact 로 INSERT 한다."""
    has_phone = exists().where(
        ContactRow.company_id == CompanyRow.id, ContactRow.type == ContactType.PHONE.value
    )
    stmt = (
        select(CompanyRow.id, CompanyRow.country, DiscoveredCompanyRow.phone)
        .join(DiscoveredCompanyRow, DiscoveredCompanyRow.canonical_key == CompanyRow.canonical_key)
        .where(DiscoveredCompanyRow.phone.is_not(None), DiscoveredCompanyRow.phone != "")
        .where(~has_phone)
    )
    rows = [tuple(r) for r in session.execute(stmt).all()]
    if apply:
        for cid, _country, phone in rows:
            session.add(
                ContactRow(
                    id=contact_id_for(cid, ContactType.PHONE.value, phone),
                    company_id=cid,
                    type=ContactType.PHONE.value,
                    value=phone,
                    extract_method=ExtractMethod.API.value,
                    confidence=0.9,
                )
            )
        session.commit()
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="실제 INSERT(기본은 리포트만)")
    args = ap.parse_args()
    settings = Settings()
    if settings.dry_run:
        raise SystemExit("DRY_RUN=true — 백필 무의미. 중단(LEADCRAWLER_DRY_RUN=false 로 실행).")
    with get_sessionmaker(settings)() as session:
        rows = backfill(session, apply=args.apply)
    by_country: dict[str, int] = {}
    for _cid, country, _phone in rows:
        by_country[country or ""] = by_country.get(country or "", 0) + 1
    print(f"대상 {len(rows)}건 (국가별: {dict(sorted(by_country.items(), key=lambda kv: -kv[1]))})")
    print(f"적용 완료 {len(rows)}건" if args.apply else "리포트만 — 적용하려면 --apply")


if __name__ == "__main__":
    main()
