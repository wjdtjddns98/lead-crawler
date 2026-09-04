"""C2 LLM 판정 — 쇼트리스트 필터·예산 가드·캡·파싱·스텁(전부 오프라인, 네트워크/과금 없음)."""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.dedup_resolve.llm_judge import (
    JUDGE_TIERS,
    ClaudeJudge,
    JudgeVerdict,
    StubJudge,
    _parse_verdict,
    build_judge,
    judge_candidates,
)
from leadcrawler.dedup_resolve.near_dup import DuplicateCandidate


def _cand(tier: str, *, key_a: str = "a", key_b: str = "b", dom_a=None, dom_b=None) -> DuplicateCandidate:
    return DuplicateCandidate(
        key_a=key_a, key_b=key_b, name_a="Acme Inc", name_b="Acme Corp",
        country="KR", domain_a=dom_a, domain_b=dom_b, name_score=88.0, tier=tier,
        reason="t",
    )


class FakeJudge:
    """판정 호출을 세고 고정 verdict 를 반환하는 더블. model 로 유료/스텁 구분 흉내.

    ``billed`` 는 기본적으로 유료 판정기(model!="stub")의 성공 왕복을 흉내내 True 로 둔다.
    호출 전 실패(미과금)를 흉내내려면 ``billed=False`` 를 명시한다.
    """

    def __init__(self, *, same: bool = True, model: str = "fake", billed: bool | None = None) -> None:
        self.calls = 0
        self._same = same
        self.model = model
        self._billed = billed if billed is not None else (model != "stub")

    def judge(self, candidate: DuplicateCandidate) -> JudgeVerdict:
        self.calls += 1
        return JudgeVerdict(
            same=self._same, confidence=0.7, reason="fake", model=self.model, billed=self._billed
        )


class FakeLedger:
    """예산 가드 테스트용 원장 더블 — over 플래그로 차단 흉내, record 호출 집계."""

    def __init__(self, *, over: bool = False) -> None:
        self.over = over
        self.records: list[str] = []

    def record(self, provider: str, units: int = 1):
        self.records.append(provider)
        return None

    def is_over_budget(self, month_key=None) -> bool:
        return self.over


# ── StubJudge: 도메인 동치만 같음(보수적), 그 외 불확실 ─────────────────────────
def test_stub_same_when_domains_equal() -> None:
    v = StubJudge().judge(_cand("domain", dom_a="acme.com", dom_b="acme.com"))
    assert v.same is True and v.confidence > 0 and v.model == "stub"


def test_stub_uncertain_when_domains_differ_or_missing() -> None:
    assert StubJudge().judge(_cand("lexical", dom_a="acme.com", dom_b=None)).same is False
    assert StubJudge().judge(_cand("lexical", dom_a="a.com", dom_b="b.com")).same is False


# ── judge_candidates: 티어 필터 ────────────────────────────────────────────────
def test_only_shortlist_tiers_are_judged() -> None:
    cands = [
        _cand("auto", dom_a="x.com", dom_b="x.com"),  # 제외(이미 해소)
        _cand("keep_both", dom_a="a.com", dom_b="b.com"),  # 제외(이미 결론)
        _cand("domain", key_a="d1", key_b="d2"),
        _cand("lexical", key_a="l1", key_b="l2"),
        _cand("shortlist", key_a="s1", key_b="s2"),
    ]
    fake = FakeJudge()
    out = judge_candidates(cands, fake)
    assert fake.calls == 3
    assert {p.candidate.tier for p in out} == JUDGE_TIERS


# ── 예산 가드 + 과금 적재 ──────────────────────────────────────────────────────
def test_paid_judge_records_cost_and_respects_budget() -> None:
    cands = [_cand("domain", key_a=f"k{i}", key_b=f"k{i}b") for i in range(3)]
    ledger = FakeLedger(over=False)
    out = judge_candidates(cands, FakeJudge(model="claude"), ledger=ledger)
    assert len(out) == 3
    assert ledger.records == ["dedup_llm"] * 3  # 유료 판정 3건 적재


def test_over_budget_stops_before_paid_calls() -> None:
    cands = [_cand("domain", key_a=f"k{i}", key_b=f"k{i}b") for i in range(3)]
    ledger = FakeLedger(over=True)
    fake = FakeJudge(model="claude")
    out = judge_candidates(cands, fake, ledger=ledger)
    assert out == [] and fake.calls == 0 and ledger.records == []


def test_stub_judge_is_not_charged() -> None:
    cands = [_cand("domain", dom_a="x.com", dom_b="x.com")]
    ledger = FakeLedger(over=False)
    judge_candidates(cands, StubJudge(), ledger=ledger)
    assert ledger.records == []  # 스텁은 무료 → 0원 행 안 남김


def test_unbilled_failure_not_charged() -> None:
    # 유료 판정기지만 호출 전 실패(미설치/키오류)로 billed=False → 과금 안 함(거짓 차감 방지).
    cands = [_cand("domain", key_a=f"k{i}", key_b=f"k{i}b") for i in range(3)]
    ledger = FakeLedger(over=False)
    out = judge_candidates(cands, FakeJudge(model="claude", billed=False), ledger=ledger)
    assert len(out) == 3 and ledger.records == []  # 판정은 남되 과금은 0건


# ── 런당 캡 ────────────────────────────────────────────────────────────────────
def test_max_pairs_cap() -> None:
    cands = [_cand("lexical", key_a=f"k{i}", key_b=f"k{i}b") for i in range(5)]
    fake = FakeJudge()
    out = judge_candidates(cands, fake, max_pairs=2)
    assert len(out) == 2 and fake.calls == 2


# ── 응답 파싱 강건성 ───────────────────────────────────────────────────────────
def test_parse_valid_json() -> None:
    v = _parse_verdict('{"same": true, "confidence": 0.8, "reason": "동일"}', model="m")
    assert v.same is True and v.confidence == 0.8 and v.reason == "동일"


def test_parse_strips_code_fence_and_prose() -> None:
    text = '결과:\n```json\n{"same": false, "confidence": 0.3, "reason": "다름"}\n```'
    v = _parse_verdict(text, model="m")
    assert v.same is False and v.confidence == 0.3


def test_parse_clamps_confidence_and_handles_garbage() -> None:
    assert _parse_verdict('{"same": true, "confidence": 9}', model="m").confidence == 1.0
    bad = _parse_verdict("no json here", model="m")
    assert bad.same is False and bad.confidence == 0.0  # 불확실 폴백


# ── build_judge: dry_run/키없음 → 스텁, 라이브+키 → Claude ─────────────────────
def test_build_judge_stub_under_dry_run() -> None:
    assert isinstance(build_judge(Settings(dry_run=True)), StubJudge)
    assert isinstance(build_judge(Settings(dry_run=False, anthropic_api_key="")), StubJudge)


def test_build_judge_claude_when_live_with_key() -> None:
    j = build_judge(Settings(dry_run=False, anthropic_api_key="sk-x"))
    assert isinstance(j, ClaudeJudge)
