# 1회성(2026-08-21, git 미반영): country 표기 ISO2 정규화 — fastpath 인제스트 원문 유입분.
"""company·discovered_company 의 country 자유표기('대한민국'/'호주'…)를 ISO2 로 접는다.

해석은 :func:`resolve_country`(코드 단일 출처 — 큐 필터·집계와 동일 별칭) 그대로.
미등록 표기는 건드리지 않는다(원문 보존 — 무리한 추측 매핑 금지).

사용: python scripts/normalize_country_values.py [--apply]
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from leadcrawler.config import Settings
from leadcrawler.sources.countries import resolve_country
from leadcrawler.storage.db import get_sessionmaker

# 등록 지원국 밖 표기 보강 — 표준 ISO 3166-1 alpha-2 사실 매핑만(추측·집계명 금지:
# '유럽'/'아시아'/숫자 등 판별 불가 값은 원문 유지).
_KO_ISO: dict[str, str] = {
    "뉴질랜드": "NZ", "튀르키예": "TR", "터키": "TR", "멕시코": "MX", "이탈리아": "IT",
    "핀란드": "FI", "스페인": "ES", "네덜란드": "NL", "벨기에": "BE", "폴란드": "PL",
    "체코": "CZ", "스위스": "CH", "노르웨이": "NO", "스웨덴": "SE", "헝가리": "HU",
    "몽골": "MN", "덴마크": "DK", "아일랜드": "IE", "오스트리아": "AT", "방글라데시": "BD",
    "사우디아라비아": "SA", "사우디": "SA", "아랍에미리트": "AE", "쿠웨이트": "KW",
    "바레인": "BH", "카타르": "QA", "요르단": "JO", "오만": "OM", "포르투갈": "PT",
    "이집트": "EG", "레바논": "LB", "에스토니아": "EE", "팔레스타인": "PS", "그리스": "GR",
    "이스라엘": "IL", "리투아니아": "LT", "이라크": "IQ", "루마니아": "RO", "세르비아": "RS",
    "캄보디아": "KH", "룩셈부르크": "LU", "슬로베니아": "SI", "아이슬란드": "IS",
    "크로아티아": "HR", "라트비아": "LV", "브루나이": "BN", "몰타": "MT", "미얀마": "MM",
    "앙골라": "AO", "동티모르": "TL", "키프로스": "CY", "슬로바키아": "SK",
    "우크라이나": "UA", "카자흐스탄": "KZ",
}


def _resolve_iso2(value: str) -> str | None:
    r = resolve_country(value)
    if r is not None:
        return r.iso2
    return _KO_ISO.get(value.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    sm = get_sessionmaker(Settings())
    with sm() as s:
        total = 0
        unresolved: list[tuple[str, int]] = []
        for table in ("company", "discovered_company"):
            vals = s.execute(text(
                f"select country, count(*) from {table}"
                " where coalesce(country,'') <> '' group by 1"
            )).all()
            for v, n in vals:
                iso = _resolve_iso2(v)
                if iso is None:
                    if table == "company":
                        unresolved.append((v, n))
                    continue
                if v == iso:
                    continue
                s.execute(text(
                    f"update {table} set country = :iso where country = :v"
                ), {"iso": iso, "v": v})
                total += n
                print(f"  {table}: {v!r} -> {iso} ({n})")
        if args.apply:
            s.commit()
            print(f"[country-norm] COMMIT — {total}행 갱신")
        else:
            s.rollback()
            print(f"[country-norm] DRY-RUN(롤백) — 갱신 예정 {total}행")
        if unresolved:
            top = sorted(unresolved, key=lambda kv: -kv[1])[:15]
            print(f"[country-norm] 미해석 표기(원문 유지) {len(unresolved)}종, 상위: {top}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
