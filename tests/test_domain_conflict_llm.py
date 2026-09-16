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
    fj = _FakeJudge({"향우종합관리(주)": "different"})
    out = {c.company_id: c for c in classify_group(rows, {}, judge=fj, judge_budget=[5], ledger=_Ledger(over=True))}
    assert out["c"].action == REVIEW and fj.calls == 0  # 예산 초과 — 호출 자체가 없음
    out = {c.company_id: c for c in classify_group(rows, {}, judge=StubRelationJudge(), judge_budget=[5])}
    assert out["c"].action == REVIEW  # stub 은 unknown
    out = {c.company_id: c for c in classify_group(rows, {})}
    assert out["c"].action == AUTO_WRONG  # judge 없으면 규칙대로


def test_two_char_title_only_when_first_token() -> None:
    rows = [_row("k", "name:kr:한국", "한국(주)", host="x.com"), _row("y", "name:kr:다른", "다른회사", host="x.com")]
    out = {c.company_id: c for c in classify_group(rows, {}, {"x.com": "다른회사 | 한국 최고의 서비스"})}
    assert out["k"].evidence == []  # 태그라인의 '한국' 은 근거 아님
    out = {c.company_id: c for c in classify_group(rows, {}, {"x.com": "한국 | 홈"})}
    assert out["k"].evidence == ["title"]


def test_claude_relation_judge_parses_and_fails_closed(monkeypatch) -> None:
    from types import SimpleNamespace

    from leadcrawler.dedup_resolve import domain_conflict as dc

    class _Msg:
        def __init__(self, text):
            self.content = [SimpleNamespace(type="text", text=text)]

    class _Client:
        def __init__(self, text=None, exc=None):
            self.text, self.exc = text, exc
            self.messages = self

        def create(self, **kw):
            if self.exc:
                raise self.exc
            return _Msg(self.text)

    j = dc.ClaudeRelationJudge("k", model="m")
    fenced = "```json" + chr(10) + '{"relation": "Affiliate", "reason": "x"}' + chr(10) + "```"
    j._client = _Client(fenced)
    assert j.judge("a", "b", "h", "KR") == ("affiliate", True)
    j._client = _Client("no json here")
    assert j.judge("a", "b", "h", "KR") == ("unknown", True)  # 왕복은 됐으니 과금
    j._client = _Client(exc=RuntimeError("api down"))
    assert j.judge("a", "b", "h", "KR") == ("unknown", False)
    j._client = _Client('{"relation": "banana"}')
    assert j.judge("a", "b", "h", "KR") == ("unknown", True)


def test_best_owner_prefers_dom_key_over_latin() -> None:
    rows = [
        _row("t", "name:kr:x", "Acme Corp", host="acme.com", country="US"),  # latin_name
        _row("d", "dom:acme.com", "애크미코리아", host="acme.com"),  # dom_key
        _row("w", "name:kr:향우", "향우종합관리(주)", host="acme.com"),
    ]
    seen = []

    class _J:
        model = "fake"

        def judge(self, a, b, host, country):
            seen.append(b)
            return "different", True

    classify_group(rows, {}, judge=_J(), judge_budget=[5])
    assert seen == ["애크미코리아"]


def test_claude_relation_judge_uses_auth_token(monkeypatch) -> None:
    """라이브 .env 는 OAuth 토큰만 있다 — auth_token 이 anthropic_client 로 전달돼야 한다."""
    import leadcrawler.llm as llm_mod
    from leadcrawler.dedup_resolve import domain_conflict as dc

    seen = {}

    def _fake_client(*, api_key="", auth_token="", max_retries=2):
        seen.update(api_key=api_key, auth_token=auth_token)
        raise RuntimeError("stop here")

    monkeypatch.setattr(llm_mod, "anthropic_client", _fake_client)
    j = dc.ClaudeRelationJudge("", auth_token="tok", model="m")
    assert j.judge("a", "b", "h", "KR") == ("unknown", False)
    assert seen == {"api_key": "", "auth_token": "tok"}
