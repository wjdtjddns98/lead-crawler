"""기존 import (엑셀/CSV → dedup 시드) 테스트."""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from leadcrawler.excel_format import HEADERS
from leadcrawler.importer import ExistingImporter


def test_import_xlsx_round_trip(tmp_path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(HEADERS)
    # 국가, 업체명, ..., 사이트(F)
    row = ["KR", "이미찾은기업", "", "ir@found.com", "O", "found.com",
           "", "", "건설", "O", "O", ""]
    ws.append(row)
    path = tmp_path / "existing.xlsx"
    wb.save(path)

    rows = ExistingImporter().read(path)
    assert len(rows) == 1
    assert rows[0].canonical_key == "dom:found.com"
    assert rows[0].name == "이미찾은기업"


def test_import_csv(tmp_path: Path) -> None:
    path = tmp_path / "existing.csv"
    path.write_text("업체명,국가,사이트\nACME,US,acme.com\n", encoding="utf-8")
    rows = ExistingImporter().read(path)
    assert len(rows) == 1
    assert rows[0].canonical_key == "dom:acme.com"


def test_blank_rows_skipped(tmp_path: Path) -> None:
    path = tmp_path / "e.csv"
    path.write_text("업체명,사이트\n,\nACME,acme.com\n", encoding="utf-8")
    assert len(ExistingImporter().read(path)) == 1
