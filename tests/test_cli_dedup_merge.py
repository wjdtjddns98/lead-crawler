"""dedup-merge 확정쌍 수집 — auto 항상, LLM 은 same·confidence·비스텁만(아키텍트 Rec.1)."""

from __future__ import annotations

from leadcrawler.cli import confirmed_pairs_from_report


def _report() -> dict:
    return {
        "candidates": [
            {"key_a": "a1", "key_b": "a2", "tier": "auto"},
            {"key_a": "d1", "key_b": "d2", "tier": "domain"},  # 비-auto → 제외
        ],
        "judged": [
            {"candidate": {"key_a": "llm1", "key_b": "llm2"},
             "verdict": {"same": True, "confidence": 0.9, "model": "claude-haiku-4-5-20251001"}},
            {"candidate": {"key_a": "stub1", "key_b": "stub2"},
             "verdict": {"same": True, "confidence": 0.9, "model": "stub"}},  # 스텁 → 제외
            {"candidate": {"key_a": "low1", "key_b": "low2"},
             "verdict": {"same": True, "confidence": 0.5, "model": "claude"}},  # 저신뢰 → 제외
            {"candidate": {"key_a": "no1", "key_b": "no2"},
             "verdict": {"same": False, "confidence": 0.99, "model": "claude"}},  # same=False → 제외
        ],
    }


def test_auto_only_by_default() -> None:
    pairs = confirmed_pairs_from_report(_report(), include_llm=False, min_confidence=0.8)
    assert pairs == [("a1", "a2")]


def test_include_llm_excludes_stub_lowconf_and_notsame() -> None:
    pairs = confirmed_pairs_from_report(_report(), include_llm=True, min_confidence=0.8)
    # auto + 비스텁·고신뢰·same 인 llm 1쌍만. 스텁/저신뢰/not-same 은 배제.
    assert set(pairs) == {("a1", "a2"), ("llm1", "llm2")}


def test_empty_report_yields_no_pairs() -> None:
    assert confirmed_pairs_from_report({}, include_llm=True, min_confidence=0.8) == []
