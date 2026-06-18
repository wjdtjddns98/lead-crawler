"""기존 검색분 import — 엑셀/CSV → 중복제거 시드.

제약 ①(이미 검색한 기업 재추출 금지)의 선행 단계. 직원들이 만든 동일 12컬럼
서식(또는 회사명·국가·사이트·이메일 헤더를 가진 CSV)을 읽어 canonical_key 를
산정한 ``ImportedCompany`` 목록을 돌려준다.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

from .dedup import canonical_key

# 다양한 헤더 표기를 표준 필드로 흡수하기 위한 매핑.
_HEADER_ALIASES = {
    "국가": "country",
    "업체명": "name",
    "회사명": "name",
    "사이트": "domain",
    "홈페이지": "domain",
    "site": "domain",
    "도메인": "domain",
    "이메일": "email",
    "email": "email",
}


class ImportedCompany(BaseModel):
    """기존 데이터 한 행 — 시드용 최소 정보 + canonical_key."""

    canonical_key: str
    name: str
    country: str = ""
    domain: str | None = None
    email: str | None = None


def _normalize_headers(headers: list[str]) -> list[str]:
    return [_HEADER_ALIASES.get((h or "").strip(), (h or "").strip()) for h in headers]


def _row_to_company(row: dict[str, str]) -> ImportedCompany | None:
    name = (row.get("name") or "").strip()
    domain = (row.get("domain") or "").strip() or None
    country = (row.get("country") or "").strip()
    email = (row.get("email") or "").strip() or None
    if not name and not domain:
        return None
    key = canonical_key(domain=domain, name=name, country=country)
    return ImportedCompany(
        canonical_key=key, name=name, country=country, domain=domain, email=email
    )


def _iter_csv(path: Path) -> Iterator[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        return
    headers = _normalize_headers(rows[0])
    for raw in rows[1:]:
        yield {headers[i]: raw[i] for i in range(min(len(headers), len(raw)))}


def _iter_xlsx(path: Path) -> Iterator[dict[str, str]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows)
    except StopIteration:
        return
    headers = _normalize_headers([str(h) if h is not None else "" for h in header_row])
    for raw in rows:
        cells = ["" if v is None else str(v) for v in raw]
        yield {headers[i]: cells[i] for i in range(min(len(headers), len(cells)))}


class ExistingImporter:
    """엑셀/CSV 기존 데이터를 시드용 ``ImportedCompany`` 목록으로 읽는다."""

    def read(self, path: str | Path) -> list[ImportedCompany]:
        """파일 확장자에 따라 .xlsx/.csv 를 파싱한다(빈/식별불가 행은 건너뜀)."""
        p = Path(path)
        if p.suffix.lower() in {".xlsx", ".xlsm"}:
            rows = _iter_xlsx(p)
        else:
            rows = _iter_csv(p)
        out: list[ImportedCompany] = []
        for row in rows:
            company = _row_to_company(row)
            if company is not None:
                out.append(company)
        return out
