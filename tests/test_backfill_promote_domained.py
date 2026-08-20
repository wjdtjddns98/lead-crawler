"""도메인 보유 미승격 회수 백필 스크립트의 SQL 이 실제 스키마에서 실행되는지 검증.

회귀 가드(2026-07-31 실사고): 커서를 ``d.id`` 로 썼는데 ``discovered_company`` 의 PK 는
``canonical_key`` 라 id 컬럼이 자체가 없다 — 라이브에서 첫 쿼리부터 ProgrammingError 로
죽었다. ruff·구문검사는 SQL 컬럼명을 못 잡으므로, 쿼리를 실제 스키마에 **실행**해야 한다.

scripts/ 는 패키지가 아니라 importlib 로 파일에서 직접 로드한다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

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


def test_scoped_industry_include_runs_on_promote_sql(tmp_path) -> None:
    """--industry include 필터가 promote 대상/카운트 SQL 에서도 실행된다(P1, 스키마 실행 가드)."""
    mod = _load()
    s = Settings(database_url=f"sqlite:///{tmp_path}/pd2.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)
    with sm() as session:
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:sec.co.kr", name="보안사", country="KR",
            industry="정보보안", source="nps", domain="sec.co.kr",
        ))
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:game.co.kr", name="게임사", country="KR",
            industry="게임", source="nps", domain="game.co.kr",
        ))
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:listed.co.kr", name="상장사", country="KR",
            industry="은행", source="dart", domain="listed.co.kr", listed="listed",
        ))
        session.commit()

        cnt_stmt, cnt_params = mod._scoped(
            mod._COUNT_SQL, ["KR"], industries=["정보보안"], exclude_listed=True
        )
        assert int(session.execute(cnt_stmt, cnt_params).scalar() or 0) == 1
        tgt_stmt, tgt_params = mod._scoped(mod._TARGET_SQL, ["KR"], industries=["정보보안"])
        rows = session.execute(tgt_stmt, {**tgt_params, "limit": 10, "after": ""}).all()
        assert [r.canonical_key for r in rows] == ["dom:kr:sec.co.kr"]

        # --listed(only_listed): 상장 확정 행만 대상 — 상장사 세그먼트 타겟 보충.
        lst_stmt, lst_params = mod._scoped(mod._TARGET_SQL, ["KR"], only_listed=True)
        rows = session.execute(lst_stmt, {**lst_params, "limit": 10, "after": ""}).all()
        assert [r.canonical_key for r in rows] == ["dom:kr:listed.co.kr"]

        # 모순 조합(exclude+only)은 조용한 0건 no-op 대신 즉시 거부.
        with pytest.raises(ValueError):
            mod._scoped(mod._TARGET_SQL, ["KR"], exclude_listed=True, only_listed=True)


def test_split_multi_flattens_commas() -> None:
    """반복 지정 + 쉼표 병기 평탄화 — CLI --exclude-industry 관례와 동일."""
    mod = _load()
    assert mod._split_multi(["정보보안,게임", "은행"]) == ["정보보안", "게임", "은행"]
    assert mod._split_multi(None) is None
    assert mod._split_multi([" , "]) is None


def test_cursor_file_only_advances_after_batch_persisted(tmp_path, monkeypatch) -> None:
    """--cursor-file 은 배치 persist 완료 후에만 기록된다(리뷰 HIGH 회귀 가드).

    중간에 죽으면(persist 예외) 커서 파일이 없어야 재기동이 같은 배치를 다시 훑는다 —
    선기록이면 미처리 행이 커서 뒤로 영구 스킵된다.
    """
    from types import SimpleNamespace

    mod = _load()
    s = Settings(database_url=f"sqlite:///{tmp_path}/pd3.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)
    with sm() as session:
        for key, dom in [("dom:kr:a1.co.kr", "a1.co.kr"), ("dom:kr:b2.co.kr", "b2.co.kr")]:
            session.add(DiscoveredCompanyRow(
                canonical_key=key, name=dom, country="KR",
                industry="게임", source="nps", domain=dom,
            ))
        session.commit()

    cursor = tmp_path / "cursor.txt"
    monkeypatch.setattr(mod, "Settings", lambda: s)
    monkeypatch.setattr(mod, "CostLedger", lambda settings, persist=True: object())
    monkeypatch.setattr(mod, "build_registry_checker", lambda settings: None)
    monkeypatch.setattr(mod, "build_classifier", lambda settings, ledger=None: None)
    for cls in ("Enricher", "ExistenceVerifier", "EmailValidator"):
        monkeypatch.setattr(mod, cls, lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(mod, "_build_lead", lambda dc, **kw: SimpleNamespace(
        company=SimpleNamespace(is_active=True), email=None,
    ))
    import sys
    monkeypatch.setattr(sys, "argv", [
        "backfill_promote_domained.py", "--workers", "1", "--batch", "10",
        "--cursor-file", str(cursor),
    ])

    # ① 배치 중간 사망 시뮬레이션 — persist 가 터지면 커서 파일이 없어야 한다.
    def boom(ws, dc, lead):
        raise RuntimeError("mid-batch death")

    monkeypatch.setattr(mod, "_persist_lead", boom)
    import pytest
    with pytest.raises(RuntimeError):
        mod.main()
    assert not cursor.exists()

    # ② 정상 완주 — 커서 파일에 마지막 키가 남는다.
    monkeypatch.setattr(mod, "_persist_lead", lambda ws, dc, lead: None)
    assert mod.main() == 0
    assert cursor.read_text(encoding="utf-8") == "dom:kr:b2.co.kr"


def test_domain_guards_seed_taken_and_overshared(tmp_path) -> None:
    """도메인 dedup 가드 시드 — 기존 company 점유 + 원장 과공유 도메인을 잡아낸다.

    2026-08-10 사고 가드: 이 가드가 없어 해석기가 오채택한 디렉터리 도메인
    (nicebizinfo 등)이 수천 건씩 무차별 승격돼 company 2만여 건이 오염됐다.
    """
    mod = _load()
    s = Settings(database_url=f"sqlite:///{tmp_path}/pd3.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)

    with sm() as session:
        # 과공유 도메인 — 캡(3)만큼의 발견행이 같은 도메인을 공유.
        for i in range(3):
            session.add(DiscoveredCompanyRow(
                canonical_key=f"name:kr:피해{i}", name=f"피해{i}", country="KR",
                industry="화학·석유화학", source="nps", domain="directoryshare.com",
            ))
        # 정상 — 단독 도메인, 이미 승격됨(homepage 점유).
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:solo.co.kr", name="단독사", country="KR",
            industry="화학·석유화학", source="nps", domain="solo.co.kr",
        ))
        session.commit()
        session.add(CompanyRow(
            id=company_id_for("dom:kr:solo.co.kr"), canonical_key="dom:kr:solo.co.kr",
            name="단독사", country="KR", industry="화학·석유화학", site_alive=True,
            homepage="https://solo.co.kr",
        ))
        session.commit()

    with sm() as session:
        taken, overshared = mod._load_domain_guards(session)
    assert taken == {"solo.co.kr"}  # homepage 정규화 도메인.
    assert overshared == {"directoryshare.com"}  # 캡 도달 도메인만.


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
