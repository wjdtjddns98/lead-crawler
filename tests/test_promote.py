"""leadcrawler.pipeline.promote — 승격 이관(세그먼트 작업 큐 설계 §4·§6 PR1) 검증.

대상/카운트 SQL·도메인가드·필터(국가/업종/listed tri-state/regions)가 실제 스키마에서
동작하는지, ``promote_batch`` 가 실패를 격리하며 커서를 전진시키는지 검증한다.
회귀 가드(2026-07-31 실사고 계승): 커서는 ``d.canonical_key`` 다(``discovered_company``
는 id 컬럼이 없다).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from leadcrawler.config import Settings
from leadcrawler.pipeline import promote as pmod
from leadcrawler.schema import CompanyRow, DiscoveredCompanyRow
from leadcrawler.storage.db import get_sessionmaker, init_db
from leadcrawler.storage.repository import company_id_for


def _mk_sm(tmp_path, name):
    s = Settings(database_url=f"sqlite:///{tmp_path}/{name}.db", dry_run=False)
    init_db(s)
    return s, get_sessionmaker(s)


def test_target_and_count_sql_run_against_real_schema(tmp_path) -> None:
    """대상/카운트 쿼리가 실제 스키마에서 실행되고, 도메인보유·미승격만 잡는다."""
    s, sm = _mk_sm(tmp_path, "pd")

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

    assert pmod.count_promote_targets(sm, None) == 1
    cnt_stmt, cnt_params = pmod._scoped(pmod._COUNT_SQL, None)
    tgt_stmt, tgt_params = pmod._scoped(pmod._TARGET_SQL, None)
    with sm() as session:
        assert int(session.execute(cnt_stmt, cnt_params).scalar() or 0) == 1
        rows = session.execute(tgt_stmt, {**tgt_params, "limit": 10, "after": ""}).all()
        assert [r.canonical_key for r in rows] == ["dom:kr:target.co.kr"]

        # 커서가 마지막 키 뒤부터 → 같은 행을 다시 주지 않는다(고착 방지).
        again = session.execute(
            tgt_stmt, {**tgt_params, "limit": 10, "after": "dom:kr:target.co.kr"}
        ).all()
        assert again == []


def test_listed_tri_state_mapping(tmp_path) -> None:
    """listed="listed"/"unlisted"/"unknown" 이 각각 only_listed/exclude_listed/무필터로 매핑."""
    s, sm = _mk_sm(tmp_path, "pd_listed")
    with sm() as session:
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:listed.co.kr", name="상장사", country="KR",
            industry="은행", source="dart", domain="listed.co.kr", listed="listed",
        ))
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:unknown.co.kr", name="미상사", country="KR",
            industry="은행", source="nps", domain="unknown.co.kr",
        ))
        session.commit()

    assert pmod.count_promote_targets(sm, ["KR"], listed="unknown") == 2
    assert pmod.count_promote_targets(sm, ["KR"], listed="listed") == 1
    assert pmod.count_promote_targets(sm, ["KR"], listed="unlisted") == 1


def test_regions_filter_runs_on_sqlite(tmp_path) -> None:
    """regions 필터(discovered_company.region 정확 일치)가 SQLite 에서 동작한다."""
    s, sm = _mk_sm(tmp_path, "pd_region")
    with sm() as session:
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:seoul.co.kr", name="서울사", country="KR",
            industry="게임", source="nps", domain="seoul.co.kr", region="서울",
        ))
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:busan.co.kr", name="부산사", country="KR",
            industry="게임", source="nps", domain="busan.co.kr", region="부산",
        ))
        session.commit()

    assert pmod.count_promote_targets(sm, ["KR"]) == 2
    assert pmod.count_promote_targets(sm, ["KR"], regions=["서울"]) == 1
    stmt, params = pmod._scoped(pmod._TARGET_SQL, ["KR"], regions=["서울"])
    with sm() as session:
        rows = session.execute(stmt, {**params, "limit": 10, "after": ""}).all()
    assert [r.canonical_key for r in rows] == ["dom:kr:seoul.co.kr"]


def test_load_domain_guards_seed_taken_and_overshared(tmp_path) -> None:
    """도메인 dedup 가드 시드 — 기존 company 점유 + 원장 과공유 도메인을 잡아낸다."""
    s, sm = _mk_sm(tmp_path, "pd_guard")
    with sm() as session:
        for i in range(3):
            session.add(DiscoveredCompanyRow(
                canonical_key=f"name:kr:피해{i}", name=f"피해{i}", country="KR",
                industry="화학·석유화학", source="nps", domain="directoryshare.com",
            ))
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
        taken, overshared = pmod._load_domain_guards(session)
    assert taken == {"solo.co.kr"}
    assert overshared == {"directoryshare.com"}


def test_split_multi_flattens_commas() -> None:
    assert pmod._split_multi(["정보보안,게임", "은행"]) == ["정보보안", "게임", "은행"]
    assert pmod._split_multi(None) is None
    assert pmod._split_multi([" , "]) is None


def test_promote_batch_isolates_single_company_failure(tmp_path, monkeypatch) -> None:
    """회사 1건 enrich 실패는 격리된다 — failed +1, 커서 전진, 나머지는 저장."""
    s, sm = _mk_sm(tmp_path, "pd_fail")
    with sm() as session:
        for key, dom in [
            ("dom:kr:ok1.co.kr", "ok1.co.kr"),
            ("dom:kr:bad.co.kr", "bad.co.kr"),
            ("dom:kr:ok2.co.kr", "ok2.co.kr"),
        ]:
            session.add(DiscoveredCompanyRow(
                canonical_key=key, name=dom, country="KR",
                industry="게임", source="nps", domain=dom,
            ))
        session.commit()

    def _fake_build_lead(dc, **kw):
        if dc.canonical_key == "dom:kr:bad.co.kr":
            raise RuntimeError("boom")
        return SimpleNamespace(
            company=SimpleNamespace(is_active=True), email=None,
        )

    monkeypatch.setattr(pmod, "_build_lead", _fake_build_lead)
    persisted: list[str] = []
    monkeypatch.setattr(
        pmod, "_persist_lead",
        lambda ws, dc, lead: persisted.append(dc.canonical_key) or True,
    )
    for cls in ("Enricher", "ExistenceVerifier", "EmailValidator"):
        monkeypatch.setattr(pmod, cls, lambda *a, **k: SimpleNamespace())
    run = _fake_run()

    rows, last_key, promoted, emails, failed = pmod.promote_batch(
        s, sm, run=run, after="", limit=10, workers=1, guards=(set(), set()),
    )
    assert rows == 3
    assert last_key == "dom:kr:ok2.co.kr"  # 마지막 키까지 전진 — 실패해도 배치는 완주.
    assert promoted == 2
    assert failed == 1
    # 성공 2건은 실제로 persist 경로를 탔고(스텁 no-op 이 아님), 실패 1건은 타지 않았다.
    assert persisted == ["dom:kr:ok1.co.kr", "dom:kr:ok2.co.kr"]

    # 대상 0 건이면 (0, after, 0, 0, 0) — 커서는 입력값 그대로.
    rows2, last_key2, *_ = pmod.promote_batch(
        s, sm, run=run, after=last_key, limit=10, workers=1, guards=(set(), set()),
    )
    assert rows2 == 0
    assert last_key2 == last_key


def _fake_run():
    """런 범위 컴포넌트 스텁 — 네트워크/LLM 없이 promote_batch 배선만 검증."""
    return pmod.PromoteRun(
        cost_ledger=SimpleNamespace(refresh=lambda: None), registry_checker=None, classifier=None
    )


def test_promote_batch_applies_domain_guards_and_still_advances(tmp_path, monkeypatch) -> None:
    """점유/과공유 도메인은 enrich 조차 안 타고, 전량 스킵돼도 커서는 전진한다.

    2026-08-10 디렉토리 도메인 대량 오염의 실제 방어선. 전량 스킵 배치가 커서를 안 옮기면
    호출자 while 루프가 영원히 같은 배치를 돈다(rows>0·커서 정지) — 그 회귀를 잡는다.
    """
    s, sm = _mk_sm(tmp_path, "pd_guard")
    with sm() as session:
        for key, dom in [
            ("dom:kr:a1.co.kr", "taken.co.kr"),
            ("dom:kr:a2.co.kr", "shared.co.kr"),
            ("dom:kr:a3.co.kr", "fresh.co.kr"),
        ]:
            session.add(DiscoveredCompanyRow(
                canonical_key=key, name=dom, country="KR",
                industry="게임", source="nps", domain=dom,
            ))
        session.commit()

    enriched: list[str] = []

    def _fake_build_lead(dc, **kw):
        enriched.append(dc.canonical_key)
        return SimpleNamespace(company=SimpleNamespace(is_active=True), email=None)

    monkeypatch.setattr(pmod, "_build_lead", _fake_build_lead)
    monkeypatch.setattr(pmod, "_persist_lead", lambda ws, dc, lead: True)
    for cls in ("Enricher", "ExistenceVerifier", "EmailValidator"):
        monkeypatch.setattr(pmod, cls, lambda *a, **k: SimpleNamespace())

    taken, overshared = {"taken.co.kr"}, {"shared.co.kr"}
    rows, last_key, promoted, _emails, failed = pmod.promote_batch(
        s, sm, run=_fake_run(), after="", limit=10, workers=1, guards=(taken, overshared),
    )
    assert rows == 3  # 스캔은 전량.
    assert enriched == ["dom:kr:a3.co.kr"]  # 가드 걸린 2건은 enrich 비용도 안 씀.
    assert promoted == 1 and failed == 0
    assert last_key == "dom:kr:a3.co.kr"
    assert "fresh.co.kr" in taken  # 신규 승격 도메인이 런 가드에 누적된다.

    # 전량 가드 스킵 배치 — 그래도 rows>0·커서 전진(호출자 루프 정지 방지).
    with sm() as session:
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:a4.co.kr", name="x", country="KR",
            industry="게임", source="nps", domain="taken.co.kr",
        ))
        session.commit()
    rows, last_key, promoted, _emails, _failed = pmod.promote_batch(
        s, sm, run=_fake_run(), after="dom:kr:a3.co.kr", limit=10, workers=1,
        guards=(taken, overshared),
    )
    assert (rows, promoted, last_key) == (1, 0, "dom:kr:a4.co.kr")


def test_listed_unknown_value_is_rejected() -> None:
    """미지의 listed 값은 조용한 무필터가 아니라 즉시 거부(전 우주 스캔 방지)."""
    for bad in ("Listed", "unlisted ", "", "all"):
        with pytest.raises(ValueError):
            pmod._listed_filter_flags(bad)


def test_count_promote_targets_matches_filters(tmp_path) -> None:
    """count_promote_targets 가 industries/exclude_industries 필터와 일치한다."""
    s, sm = _mk_sm(tmp_path, "pd_count")
    with sm() as session:
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:sec.co.kr", name="보안사", country="KR",
            industry="정보보안", source="nps", domain="sec.co.kr",
        ))
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:game.co.kr", name="게임사", country="KR",
            industry="게임", source="nps", domain="game.co.kr",
        ))
        # 국가 별칭 행 — ["KR"] 스코프가 별칭까지 접어 세는지(원 스크립트 테스트 계승).
        session.add(DiscoveredCompanyRow(
            canonical_key="dom:kr:alias.co.kr", name="별칭사", country="대한민국",
            industry="게임", source="nps", domain="alias.co.kr",
        ))
        session.commit()

    assert pmod.count_promote_targets(sm, ["KR"], industries=["정보보안"]) == 1
    assert pmod.count_promote_targets(sm, ["KR"], exclude_industries=["정보보안"]) == 2
    assert pmod.count_promote_targets(sm, ["KR"]) == 3

    # 모순 조합(exclude+only)은 조용한 0건 no-op 대신 즉시 거부(하위 _scoped 계약 그대로).
    with pytest.raises(ValueError):
        pmod._scoped(pmod._TARGET_SQL, ["KR"], exclude_listed=True, only_listed=True)


def test_promote_batch_counts_only_committed(tmp_path, monkeypatch) -> None:
    """_persist_lead 가 False(저장 실패)면 promoted/emails 미증가·failed +1, 커서는 전진."""
    s, sm = _mk_sm(tmp_path, "pd_persistfail")
    with sm() as session:
        for key, dom in [("dom:kr:p1.co.kr", "p1.co.kr"), ("dom:kr:p2.co.kr", "p2.co.kr")]:
            session.add(DiscoveredCompanyRow(
                canonical_key=key, name=dom, country="KR",
                industry="게임", source="nps", domain=dom,
            ))
        session.commit()
    monkeypatch.setattr(pmod, "_build_lead", lambda dc, **kw: SimpleNamespace(
        company=SimpleNamespace(is_active=True), email="x@y.kr",
    ))
    monkeypatch.setattr(
        pmod, "_persist_lead", lambda ws, dc, lead: dc.canonical_key.endswith("p2.co.kr")
    )
    for cls in ("Enricher", "ExistenceVerifier", "EmailValidator"):
        monkeypatch.setattr(pmod, cls, lambda *a, **k: SimpleNamespace())
    rows, last_key, promoted, emails, failed = pmod.promote_batch(
        s, sm, run=_fake_run(), after="", limit=10, workers=1, guards=(set(), set()),
    )
    assert (rows, promoted, emails, failed) == (2, 1, 1, 1)
    assert last_key == "dom:kr:p2.co.kr"
