"""P0.5 unknown→unlisted 확정 백필의 보수 판정을 실제 스키마 실행으로 검증.

핵심 계약(PO 승인 2026-08-19): NPS 출처 + unknown + 상장군 이름 미매치일 때**만**
unlisted 확정. 매치·타소스·이름없음은 전부 잔류(오확정 방지 방향).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from leadcrawler.config import Settings
from leadcrawler.dedup import normalize_name
from leadcrawler.schema import DartCorpCacheRow, DiscoveredCompanyRow
from leadcrawler.storage.db import get_sessionmaker, init_db

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_confirm_unlisted.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_confirm_unlisted", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _dc(key: str, name: str, *, source: str = "nps", listed: str = "unknown"):
    return DiscoveredCompanyRow(
        canonical_key=key, name=name, country="KR", industry="게임",
        source=source, listed=listed,
    )


def test_conservative_confirm_only_unmatched_nps(tmp_path, monkeypatch) -> None:
    mod = _load()
    s = Settings(database_url=f"sqlite:///{tmp_path}/cu.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)
    with sm() as session:
        # 상장군 근거 ①: DART 캐시 상장 corp(Y) / 기타법인(E — 상장군 아님).
        session.add(DartCorpCacheRow(
            corp_code="00000001", corp_name="삼성전자", status="000",
            name_norm=normalize_name("삼성전자"), info=json.dumps({"corp_cls": "Y"}),
        ))
        session.add(DartCorpCacheRow(
            corp_code="00000002", corp_name="중소상사", status="000",
            name_norm=normalize_name("중소상사"), info=json.dumps({"corp_cls": "E"}),
        ))
        # 파손 캐시 행 — 상장 근거로 못 쓴다(무예외 스킵, 보수: 매치 집합 제외).
        session.add(DartCorpCacheRow(
            corp_code="00000003", corp_name="파손상사", status="000",
            name_norm=normalize_name("파손상사"), info="not json",
        ))
        # 상장군 근거 ②: 원장 상장행(거래소 유입).
        session.add(_dc("dom:kr:hyundai.com", "현대차", source="exchanges", listed="listed"))
        # 후보들.
        session.add(_dc("name:kr:삼성전자", "삼성전자"))          # 잔류 — DART 상장 동명.
        session.add(_dc("name:kr:현대차", "(주)현대차"))          # 잔류 — 원장 상장 동명.
        session.add(_dc("name:kr:중소상사", "중소상사"))          # 확정 — E 는 상장군 아님.
        session.add(_dc("name:kr:굶는상사", "굶는상사"))          # 확정 — 미매치.
        session.add(_dc("name:kr:파손상사", "파손상사"))          # 확정 — 파손 캐시는 근거 아님.
        session.add(_dc("name:kr:이름없음", ""))                 # 잔류 — 판정 불가.
        session.add(_dc("name:kr:타소스", "타소스상사", source="naver_local"))  # 잔류 — 출처.
        session.commit()

    monkeypatch.setattr(mod, "Settings", lambda: s)
    audit = tmp_path / "audit.csv"
    monkeypatch.setattr(sys, "argv", [
        "backfill_confirm_unlisted.py", "--apply", "--audit-file", str(audit),
    ])
    assert mod.main() == 0

    with sm() as session:
        got = {
            k: li for k, li in session.execute(
                select(DiscoveredCompanyRow.canonical_key, DiscoveredCompanyRow.listed)
            )
        }
    assert got["name:kr:중소상사"] == "unlisted"
    assert got["name:kr:굶는상사"] == "unlisted"
    assert got["name:kr:파손상사"] == "unlisted"  # 파손 info 는 무예외로 근거 제외.
    assert got["name:kr:삼성전자"] == "unknown"
    assert got["name:kr:현대차"] == "unknown"
    assert got["name:kr:이름없음"] == "unknown"
    assert got["name:kr:타소스"] == "unknown"
    assert got["dom:kr:hyundai.com"] == "listed"  # 상장행은 손대지 않는다.
    keys = audit.read_text(encoding="utf-8").strip().splitlines()
    assert keys[0] == "canonical_key"
    assert set(keys[1:]) == {"name:kr:중소상사", "name:kr:굶는상사", "name:kr:파손상사"}


def test_report_mode_changes_nothing(tmp_path, monkeypatch) -> None:
    mod = _load()
    s = Settings(database_url=f"sqlite:///{tmp_path}/cu2.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)
    with sm() as session:
        session.add(_dc("name:kr:굶는상사", "굶는상사"))
        session.commit()
    monkeypatch.setattr(mod, "Settings", lambda: s)
    monkeypatch.setattr(sys, "argv", ["backfill_confirm_unlisted.py"])  # --apply 없음.
    assert mod.main() == 0
    with sm() as session:
        row = session.execute(select(DiscoveredCompanyRow.listed)).scalar_one()
    assert row == "unknown"


def test_dry_run_refuses(tmp_path, monkeypatch) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "Settings", lambda: SimpleNamespace(dry_run=True))
    monkeypatch.setattr(sys, "argv", ["backfill_confirm_unlisted.py", "--apply"])
    assert mod.main() == 1
