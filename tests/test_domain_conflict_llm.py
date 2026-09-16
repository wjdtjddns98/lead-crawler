"""domain-conflicts LLM 관계 게이트·소유 근거 강도·2글자 title (PR-c)."""

from __future__ import annotations

from leadcrawler.dedup_resolve.domain_conflict import (
    AUTO_WRONG,
    OWNER,
    RELATED,
    REVIEW,
    SAME_ENTITY,
    StubRelationJudge,
    classify_group,
)


def _row(cid, key, name, *, host="sebang.com", reg_no=None, name_eng=None, country="KR"):
    return {
        "company_id": cid, "canonical_key": key, "name": name, "country": country, "host": host,
        "homepage": f"https://{host}", "site_alive": True, "ledger_domain": host,
        "reg_no": reg_no, "name_eng": name_eng,
    }


class _FakeJudge:
    model = "fake"

    def __init__(self, table: dict[str, str]) -> None:
        self.table, self.calls = table, 0

    def judge(self, name_a, name_b, host, country):
        self.calls += 1
        return self.table.get(name_a, "unknown"), True


class _Ledger:
    def __init__(self, over: bool = False) -> None:
        self.over, self.recorded = over, 0

    def is_over_budget(self) -> bool:
        return self.over

    def record(self, provider: str, units: int = 1) -> None:
        self.recorded += 1


def test_audited_homepage_alone_is_not_ownership() -> None:
    """'레이'(의료기기)의 잘못된 홈페이지 수정 이력만으로 kia.com 소유주가 되면 안 된다."""
    rows = [_row("ray", "dom:raymedical.com", "레이", host="kia.com"),
            _row("kia", "name:kr:기아", "기아 주식회사", host="kia.com")]
    out = {c.company_id: c for c in classify_group(rows, {"ray": {"kia.com"}})}
    assert out["ray"].evidence == ["audited_homepage"] and out["ray"].action == REVIEW
    assert out["kia"].action == REVIEW  # 강한 근거 있는 소유주 없음 → 전부 review


def test_two_char_name_gets_title_evidence() -> None:
    rows = [_row("kia", "name:kr:기아", "기아 주식회사", host="kia.com"),
            _row("x", "name:kr:다른회사", "다른회사", host="kia.com")]
    out = {c.company_id: c for c in classify_group(rows, {}, {"kia.com": "기아 | Kia Korea"})}
    assert out["kia"].action == OWNER and out["kia"].evidence == ["title"]


def test_llm_gate_maps_relations() -> None:
    rows = [
        _row("o", "dom:sebang.com", "세방(주)"),
        _row("a", "name:kr:에코프로비엠", "주식회사에코프로비엠"),
        _row("b", "name:kr:세방전지", "세방전지주식회사"),
        _row("c", "name:kr:향우종합관리", "향우종합관리(주)"),
        _row("d", "name:kr:모름", "모름상사"),
    ]
    judge = _FakeJudge({"주식회사에코프로비엠": "same", "세방전지주식회사": "affiliate",
                        "향우종합관리(주)": "different"})
    ledger = _Ledger()
    out = {c.company_id: c for c in classify_group(rows, {}, judge=judge, judge_budget=[10], ledger=ledger)}
    assert out["a"].action == SAME_ENTITY and out["b"].action == RELATED
    assert out["c"].action == AUTO_WRONG and "llm:different" in out["c"].evidence
    assert out["d"].action == REVIEW  # unknown → 사람
    assert judge.calls == 4 and ledger.recorded == 4


def test_llm_budget_and_stub_fall_back_to_review() -> None:
    rows = [_row("o", "dom:sebang.com", "세방(주)"), _row("c", "name:kr:향우", "향우종합관리(주)")]
    out = {c.company_id: c for c in classify_group(rows, {}, judge=_FakeJudge({}), judge_budget=[0])}
    assert out["c"].action == REVIEW  # 캡 소진
    out = {c.company_id: c for c in classify_group(rows, {}, judge=_FakeJudge({"향우종합관리(주)": "different"}),
                                                    judge_budget=[5], ledger=_Ledger(over=True))}
    assert out["c"].action == REVIEW  # 예산 초과
    out = {c.company_id: c for c in classify_group(rows, {}, judge=StubRelationJudge(), judge_budget=[5])}
    assert out["c"].action == REVIEW  # stub 은 unknown
    out = {c.company_id: c for c in classify_group(rows, {})}
    assert out["c"].action == AUTO_WRONG  # judge 없으면 규칙대로
