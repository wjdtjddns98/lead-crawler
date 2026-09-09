# 1회성(2026-08-21, git 미반영): 국가 빈값·쓰레기값 백필 — 결정적 신호만(추측 금지).
"""국가가 비었거나 판별불가 쓰레기값(숫자·'??')인 행을 채운다.

신호 우선순위(전부 결정적):
  ① 상호보완 — 같은 canonical_key 의 counterpart(company↔discovered_company) 국가.
  ② 도메인 ccTLD — .kr→KR, .co.jp→JP 등 국가코드 TLD 사실 매핑(.com 등 일반 TLD 는 스킵).
남는 행은 그대로 둔다(빈값 유지 — 대시보드 '미상'으로 노출, LLM 추측 배제).

사용: python scripts/backfill_country_blank.py [--apply]
"""

from __future__ import annotations

import argparse
import re

from sqlalchemy import text

from leadcrawler.config import Settings
from leadcrawler.storage.db import get_sessionmaker

_JUNK = re.compile(r"^[0-9?]+$")

# ccTLD → ISO2. ccTLD 는 국가와 1:1인 사실 매핑(예외적 범용화 ccTLD 는 제외:
# .io/.co/.ai/.vc/.ac/.tv/.me 등은 국가 신호로 안 씀).
_CCTLD = {
    "kr": "KR", "jp": "JP", "cn": "CN", "tw": "TW", "hk": "HK", "sg": "SG", "my": "MY",
    "th": "TH", "vn": "VN", "id": "ID", "in": "IN", "ph": "PH", "au": "AU", "nz": "NZ",
    "uk": "GB", "de": "DE", "fr": "FR", "it": "IT", "es": "ES", "nl": "NL", "be": "BE",
    "pl": "PL", "cz": "CZ", "ch": "CH", "no": "NO", "se": "SE", "dk": "DK", "fi": "FI",
    "ie": "IE", "at": "AT", "hu": "HU", "pt": "PT", "gr": "GR", "tr": "TR", "mx": "MX",
    "br": "BR", "ca": "CA", "us": "US", "il": "IL", "sa": "SA", "ae": "AE", "qa": "QA",
    "kw": "KW", "bh": "BH", "om": "OM", "jo": "JO", "eg": "EG", "lb": "LB", "ee": "EE",
    "lt": "LT", "lv": "LV", "ro": "RO", "rs": "RS", "hr": "HR", "si": "SI", "sk": "SK",
    "is": "IS", "lu": "LU", "mt": "MT", "cy": "CY", "ua": "UA", "kz": "KZ", "mn": "MN",
    "bd": "BD", "kh": "KH", "mm": "MM", "bn": "BN", "ao": "AO", "tl": "TL",
}


def _cctld(domain: str | None) -> str | None:
    if not domain or "." not in domain:
        return None
    return _CCTLD.get(domain.rsplit(".", 1)[-1].lower())


def _blank(v: str | None) -> bool:
    v = (v or "").strip()
    return not v or bool(_JUNK.match(v))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    sm = get_sessionmaker(Settings())
    st = {"counterpart": 0, "cctld": 0, "left": 0}
    with sm() as s:
        rows = s.execute(text(
            "select d.canonical_key, d.country d_country, d.domain,"
            "       co.id co_id, co.country co_country"
            " from discovered_company d"
            " left join company co on co.canonical_key = d.canonical_key"
            " where coalesce(d.country,'') = '' or d.country ~ '^[0-9?]+$'"
            "    or (co.id is not null and (coalesce(co.country,'') = ''"
            "        or co.country ~ '^[0-9?]+$'))"
        )).all()
        for r in rows:
            d_blank, c_blank = _blank(r.d_country), r.co_id is not None and _blank(r.co_country)
            fill = None
            if d_blank and r.co_id is not None and not _blank(r.co_country):
                fill, why = r.co_country.strip(), "counterpart"
            elif c_blank and not _blank(r.d_country):
                fill, why = r.d_country.strip(), "counterpart"
            else:
                iso = _cctld(r.domain)
                if iso:
                    fill, why = iso, "cctld"
            if fill is None:
                st["left"] += 1
                continue
            if d_blank:
                s.execute(text(
                    "update discovered_company set country=:c where canonical_key=:k"
                ), {"c": fill, "k": r.canonical_key})
            if c_blank:
                s.execute(text("update company set country=:c where id=:i"),
                          {"c": fill, "i": r.co_id})
            st[why] += 1
        if args.apply:
            s.commit()
            print("[country-blank] COMMIT")
        else:
            s.rollback()
            print("[country-blank] DRY-RUN(롤백)")
    print(f"[country-blank] 대상 {len(rows)} → 상호보완 {st['counterpart']}"
          f" · ccTLD {st['cctld']} · 잔여(빈값 유지) {st['left']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
