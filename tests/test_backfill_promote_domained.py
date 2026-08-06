"""도메인 보유 미승격 회수 백필 스크립트의 SQL 이 실제 스키마에서 실행되는지 검증.

회귀 가드(2026-07-31 실사고): 커서를 ``d.id`` 로 썼는데 ``discovered_company`` 의 PK 는
``canonical_key`` 라 id 컬럼이 자체가 없다 — 라이브에서 첫 쿼리부터 ProgrammingError 로
죽었다. ruff·구문검사는 SQL 컬럼명을 못 잡으므로, 쿼리를 실제 스키마에 **실행**해야 한다.

scripts/ 는 패키지가 아니라 importlib 로 파일에서 직접 로드한다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from leadcrawler.config import Settings
from leadcrawler.schema import CompanyRow, DiscoveredCompanyRow
from leadcrawler.storage.db import get_sessionmaker, init_db
from leadcrawler.storage.repository import company_id_for

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_promote_domained.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_promote_domained", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_target_and_count_sql_run_against_real_schema(tmp_path) -> None:
    """대상/카운트 쿼리가 실제 스키마에서 실행되고, 도메인보유·미승격만 잡는다."""
    mod = _load()
    s = Settings(database_url=f"sqlite:///{tmp_path}/pd.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)

    with sm() as session:
        # ① 대상: 도메인 있고 company 없음.
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:target.co.kr", name="대상사", country="KR",
            industry="화학·석유화학", source="nps", domain="target.co.kr",
        ))
        # ② 비대상: 도메인 없음.
        session.add(DiscoveredCompanyRow(
            canonical_key="name:kr:무도메인", name="무도메인", country="KR",
            industry="화학·석유화학", source="nps",
        ))
        # ③ 비대상: 도메인 있으나 이미 승격됨.
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:already.co.kr", name="승격사", country="KR",
            industry="화학·석유화학", source="nps", domain="already.co.kr",
        ))
        session.commit()  # FK 순서 — 발견행 먼저.
        session.add(CompanyRow(
            id=company_id_for("dom:kr:already.co.kr"), canonical_key="dom:kr:already.co.kr",
            name="승격사", country="KR", industry="화학·석유화학", site_alive=True,
        ))
        session.commit()

    cnt_stmt, cnt_params = mod._scoped(mod._COUNT_SQL, None)
    tgt_stmt, tgt_params = mod._scoped(mod._TARGET_SQL, None)
    with sm() as session:
        assert int(session.execute(cnt_stmt, cnt_params).scalar() or 0) == 1
        rows = session.execute(tgt_stmt, {**tgt_params, "limit": 10, "after": ""}).all()
        assert [r.canonical_key for r in rows] == ["dom:kr:target.co.kr"]
        # 커서 컬럼이 결과에 있어야 다음 배치를 이어갈 수 있다(구코드는 여기서 죽었다).
        assert rows[-1].canonical_key == "dom:kr:target.co.kr"

        # 커서가 마지막 키 뒤부터 → 같은 행을 다시 주지 않는다(고착 방지).
        again = session.execute(
            tgt_stmt, {**tgt_params, "limit": 10, "after": "dom:kr:target.co.kr"}
        ).all()
        assert again == []


def test_country_scope_filters_targets(tmp_path) -> None:
    """국가 스코프 지정 시 그 국가 대상만 잡는다(미지정=전세계 — 기존 동작 유지)."""
    mod = _load()
    s = Settings(database_url=f"sqlite:///{tmp_path}/pd2.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)

    with sm() as session:
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:target.co.kr", name="대상사", country="KR",
            industry="화학·석유화학", source="nps", domain="target.co.kr",
        ))
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:us:target.com", name="USTarget", country="US",
            industry="화학·석유화학", source="gleif", domain="target.com",
        ))
        # 국가 자유표기('대한민국')도 별칭 집합으로 같은 KR 로 접힌다.
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:hangul.co.kr", name="한글국가사", country="대한민국",
            industry="화학·석유화학", source="nps", domain="hangul.co.kr",
        ))
        session.commit()

    with sm() as session:
        cnt_all, p_all = mod._scoped(mod._COUNT_SQL, None)
        assert int(session.execute(cnt_all, p_all).scalar() or 0) == 3

        cnt_kr, p_kr = mod._scoped(mod._COUNT_SQL, ["KR"])
        assert int(session.execute(cnt_kr, p_kr).scalar() or 0) == 2

        tgt_kr, tp_kr = mod._scoped(mod._TARGET_SQL, ["KR"])
        rows = session.execute(tgt_kr, {**tp_kr, "limit": 10, "after": ""}).all()
        assert sorted(r.canonical_key for r in rows) == [
            "dom:kr:hangul.co.kr", "dom:kr:target.co.kr",
        ]
