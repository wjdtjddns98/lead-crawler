"""마이그레이션 f1a9c3d7b2e4 — name: 티어 국가표기 리키 테스트.

코드(:func:`leadcrawler.dedup.normalize_country`)와 짝을 이뤄, 이미 적재된
``name:%`` 행을 ISO2 로 리키하면서 **모든 참조**(FK: duplicate_of·company.canonical_key,
파생: company.id, 비-FK 값: dedup_candidate.key_a/key_b/survivor_key)를 정합 유지하는지
검증한다. conftest 가 DATABASE_URL 을 격리 SQLite·FK ON 으로 잡는다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from leadcrawler.config import get_settings
from leadcrawler.schema import (
    CompanyRow,
    ContactRow,
    DedupCandidateRow,
    DiscoveredCompanyRow,
    ReviewQueueRow,
)
from leadcrawler.storage.db import get_engine, init_db, session_scope
from leadcrawler.storage.dedup_candidate import pair_id
from leadcrawler.storage.repository import company_id_for

# 마이그레이션 모듈을 파일 경로로 로드한다(alembic/versions 는 패키지가 아니라 직접 import).
_MIG_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic" / "versions" / "f1a9c3d7b2e4_normalize_name_tier_country.py"
)
_spec = importlib.util.spec_from_file_location("_mig_country_rekey", _MIG_PATH)
assert _spec and _spec.loader
_mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mig)


def _run() -> int:
    with get_engine(get_settings()).begin() as conn:
        return _mig._apply(conn)


def test_rekey_normalizes_keys_and_repoints_company_with_derived_id() -> None:
    init_db(get_settings())
    with session_scope(get_settings()) as s:
        # A: 변경대상(대한민국→kr) — company(파생 id) + 자식(contact/review_queue) + duplicate_of.
        s.add(DiscoveredCompanyRow(canonical_key="name:대한민국:삼성", name="삼성", country="대한민국"))
        s.add(DiscoveredCompanyRow(canonical_key="name:kr:현대", name="현대", country="KR"))  # 무변경
        s.add(DiscoveredCompanyRow(canonical_key="name::노네임", name="노네임", country=""))  # 무변경
        s.add(DiscoveredCompanyRow(canonical_key="name:미국:acme", name="acme", country="미국"))  # us
        s.add(DiscoveredCompanyRow(
            canonical_key="dom:x.com", name="X", country="대한민국", domain="x.com"))  # name: 아님
        s.flush()
        old_cid = company_id_for("name:대한민국:삼성")
        s.add(CompanyRow(id=old_cid, canonical_key="name:대한민국:삼성", name="삼성"))
        s.add(ContactRow(id="ct1", company_id=old_cid, type="email", value="ir@s.com"))
        s.add(ReviewQueueRow(id="rq1", company_id=old_cid, field="email"))
        # duplicate_of(무변경 행 C)가 변경대상 A 를 가리킴.
        s.get(DiscoveredCompanyRow, "name::노네임").duplicate_of = "name:대한민국:삼성"

    assert _run() == 2  # A, D 만 변경.

    new_cid = company_id_for("name:kr:삼성")
    with session_scope(get_settings()) as s:
        keys = {r.canonical_key for r in s.query(DiscoveredCompanyRow).all()}
        assert "name:kr:삼성" in keys and "name:대한민국:삼성" not in keys
        assert "name:us:acme" in keys and "name:미국:acme" not in keys
        assert {"name:kr:현대", "name::노네임", "dom:x.com"} <= keys
        # duplicate_of repoint.
        assert s.get(DiscoveredCompanyRow, "name::노네임").duplicate_of == "name:kr:삼성"
        # company: 새 파생 id 로 재생성(불변식 보존), 옛 id 제거.
        assert s.get(CompanyRow, old_cid) is None
        comp = s.get(CompanyRow, new_cid)
        assert comp is not None and comp.canonical_key == "name:kr:삼성"
        assert new_cid == company_id_for(comp.canonical_key)  # id=해시(key) 정합.
        # 자식 FK 가 새 company.id 로 repoint(고아 0).
        assert s.get(ContactRow, "ct1").company_id == new_cid
        assert s.get(ReviewQueueRow, "rq1").company_id == new_cid


def test_rekey_merge_path_when_new_key_already_exists() -> None:
    # 변경대상(name:대한민국:삼성)의 새 key 가 이미 존재(name:kr:삼성) → INSERT 건너뛰고
    # 자식 repoint 후 옛 행만 삭제(머지). 데이터 손실 0, 새 행 1개만 잔존.
    init_db(get_settings())
    with session_scope(get_settings()) as s:
        s.add(DiscoveredCompanyRow(canonical_key="name:kr:삼성", name="삼성-기존", country="KR"))
        s.add(DiscoveredCompanyRow(canonical_key="name:대한민국:삼성", name="삼성-옛", country="대한민국"))
        s.flush()
        old_cid = company_id_for("name:대한민국:삼성")
        s.add(CompanyRow(id=old_cid, canonical_key="name:대한민국:삼성", name="삼성-옛"))

    assert _run() == 1
    with session_scope(get_settings()) as s:
        rows = [r.canonical_key for r in s.query(DiscoveredCompanyRow).all()]
        assert rows == ["name:kr:삼성"]  # 옛 행 삭제, 기존 새 행 유지(머지).
        # company 가 기존 새 key 로 repoint(파생 id 정합).
        assert s.get(CompanyRow, company_id_for("name:대한민국:삼성")) is None
        assert s.get(CompanyRow, company_id_for("name:kr:삼성")) is not None


def test_rekey_chained_duplicate_of_processing_order_safe() -> None:
    # 변경대상 A 의 duplicate_of 가 또 다른 변경대상 B 를 가리킴 — 처리 순서 무관하게 정합.
    init_db(get_settings())
    with session_scope(get_settings()) as s:
        s.add(DiscoveredCompanyRow(canonical_key="name:미국:비", name="비", country="미국"))
        s.flush()
        a = DiscoveredCompanyRow(canonical_key="name:대한민국:에이", name="에이", country="대한민국")
        a.duplicate_of = "name:미국:비"
        s.add(a)

    assert _run() == 2
    with session_scope(get_settings()) as s:
        keys = {r.canonical_key for r in s.query(DiscoveredCompanyRow).all()}
        assert keys == {"name:kr:에이", "name:us:비"}
        assert s.get(DiscoveredCompanyRow, "name:kr:에이").duplicate_of == "name:us:비"


def test_rekey_repoints_dedup_candidate() -> None:
    # dedup_candidate.key_a/key_b/survivor_key(비-FK 값 참조)가 새 key 로 repoint + id 재계산.
    init_db(get_settings())
    old_a, old_b = sorted(("name:대한민국:삼성", "name:kr:현대"))
    with session_scope(get_settings()) as s:
        s.add(DiscoveredCompanyRow(canonical_key="name:대한민국:삼성", name="삼성", country="대한민국"))
        s.add(DiscoveredCompanyRow(canonical_key="name:kr:현대", name="현대", country="KR"))
        s.add(DedupCandidateRow(
            id=pair_id(old_a, old_b), key_a=old_a, key_b=old_b, tier="lexical",
            survivor_key="name:대한민국:삼성"))

    assert _run() == 1
    new_a, new_b = sorted(("name:kr:삼성", "name:kr:현대"))
    with session_scope(get_settings()) as s:
        cands = s.query(DedupCandidateRow).all()
        assert len(cands) == 1
        c = cands[0]
        assert {c.key_a, c.key_b} == {"name:kr:삼성", "name:kr:현대"}
        assert c.survivor_key == "name:kr:삼성"
        assert c.id == pair_id(new_a, new_b)  # 파생 id 재계산.


def test_rekey_is_idempotent() -> None:
    init_db(get_settings())
    with session_scope(get_settings()) as s:
        s.add(DiscoveredCompanyRow(canonical_key="name:대한민국:삼성", name="삼성", country="대한민국"))
        s.add(DiscoveredCompanyRow(canonical_key="name:미국:acme", name="acme", country="미국"))
    assert _run() == 2
    assert _run() == 0  # 재실행 — 이미 정규화돼 변경 0.
