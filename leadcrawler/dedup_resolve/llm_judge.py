"""LLM 판정(C2) — 무료·결정적 사다리가 못 가른 쇼트리스트만 Claude 로 grounded 비교.

C1(:mod:`near_dup`)이 분류한 후보 중 **자동제거(auto)도 둘다유지(keep_both)도 아닌**
경계 티어(``domain``/``lexical``/``shortlist``)만 Claude(Haiku)에 보내 "같은 기업인가?"
를 이름·도메인·국가 근거로 판정한다. ``auto`` 는 이미 해소됐고 ``keep_both`` 는 도메인이
명백히 달라 이미 결론(둘 다 유지)이므로 유료 호출을 낭비하지 않는다.

설계는 :mod:`leadcrawler.enrich.vision`(Claude Vision) 선례를 따른다:
- **opt-in 플래그** ``dedup_llm_judge`` + ``anthropic_api_key`` 가 있을 때만 실호출.
- **dry_run**: :class:`StubJudge` 가 네트워크 없이 결정적 판정(도메인root 일치 휴리스틱).
- **cost_ledger**: 유료 호출 1건마다 ``record("dedup_llm")`` + 호출 전 예산 가드.
- **audit**: 판정마다 ``{same, confidence, reason, model}`` 를 남겨 사후 검수·롤백 근거로.
- 미설치/키없음/오류/JSON파싱실패는 **불확실(same=False, confidence=0)** 폴백 — 절대
  같다고 단정하지 않는다(제약② 리드손실 방지: 모르면 사람 워크벤치로).

이 모듈은 판정만 한다. 머지 기록(``duplicate_of``)은 C3 골든레코드/C4 워크벤치가 한다.
"""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import BaseModel, Field

from ..cost_ledger import SupportsCostLedger
from ..logging import get_logger
from .near_dup import DuplicateCandidate

log = get_logger("dedup.llm_judge")

# cost_ledger·가드용 provider 식별자(단가는 DEFAULT_PRICING_KRW 에 등록).
PROVIDER = "dedup_llm"

# 사다리가 못 가른 = LLM 판정 대상 티어. auto(이미 해소)·keep_both(이미 결론)는 제외.
JUDGE_TIERS: frozenset[str] = frozenset({"domain", "lexical", "shortlist"})

_PROMPT = (
    "두 회사 레코드가 **동일한 실존 기업**인지 판정하라. 표기 차이(법인격 Inc/Co.,Ltd, "
    "현지어/영문, 약어)는 같은 기업일 수 있으나, 도메인이 명백히 다른 별개 사업체(동명이인)는 "
    "다른 기업이다. 근거가 부족하면 confidence 를 낮춰라.\n"
    "오직 아래 JSON 만 출력하라(설명·코드펜스 금지):\n"
    '{{"same": true|false, "confidence": 0.0~1.0, "reason": "한 문장 근거"}}\n\n'
    "회사 A: 이름={name_a!r} 도메인={domain_a!r} 국가={country!r}\n"
    "회사 B: 이름={name_b!r} 도메인={domain_b!r} 국가={country!r}"
)


class JudgeVerdict(BaseModel):
    """LLM(또는 스텁) 판정 1건 — 사후 검수·롤백 audit 근거."""

    same: bool  # 같은 기업인가
    confidence: float = Field(ge=0.0, le=1.0)  # 0~1 확신도(0=불확실/판정불가)
    reason: str  # 한 문장 근거(한국어)
    model: str = ""  # 판정 주체("stub" 또는 모델명) — audit 추적용


class JudgedPair(BaseModel):
    """중복 후보 1쌍 + 그 LLM 판정 — 리포트/워크벤치 입력."""

    candidate: DuplicateCandidate
    verdict: JudgeVerdict


class SupportsJudge(Protocol):
    """판정기 인터페이스(테스트 더블·스텁·실제 Claude 가 구현)."""

    def judge(self, candidate: DuplicateCandidate) -> JudgeVerdict:
        """후보 1쌍이 동일 기업인지 판정한다(실패해도 예외 없이 불확실 반환)."""
        ...


def _uncertain(reason: str, *, model: str = "") -> JudgeVerdict:
    """불확실 폴백 판정 — 모르면 같다고 단정하지 않는다(사람 워크벤치로 위임)."""
    return JudgeVerdict(same=False, confidence=0.0, reason=reason, model=model)


def _parse_verdict(text: str, *, model: str) -> JudgeVerdict:
    """모델 응답 텍스트에서 JSON 판정을 안전하게 파싱한다(실패 시 불확실 폴백).

    코드펜스·앞뒤 잡설을 견디기 위해 첫 ``{`` ~ 마지막 ``}`` 구간만 떼어 파싱한다.
    confidence 는 0~1 로 클램프, same 은 bool 강제 — 모델 일탈 출력에 강건.
    """
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return _uncertain("LLM 응답에 JSON 없음(파싱 실패)", model=model)
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return _uncertain("LLM JSON 파싱 실패", model=model)
    try:
        conf = float(data.get("confidence", 0.0))
    except (ValueError, TypeError):
        conf = 0.0
    conf = min(1.0, max(0.0, conf))
    reason = str(data.get("reason", "")).strip() or "(근거 미제공)"
    return JudgeVerdict(same=bool(data.get("same", False)), confidence=conf, reason=reason, model=model)


class StubJudge:
    """dry_run·테스트용 결정적 판정기 — 네트워크·과금 없음.

    도메인root 가 둘 다 있고 일치하면 같음(높은 확신), 그 외엔 불확실로 위임한다.
    (C1 이 이미 도메인일치+이름高를 auto 로 빼므로, 여기 오는 도메인일치 쌍은 이름이
    낮아 사람 판정이 맞다 — 스텁은 보수적으로 도메인 동치만 같다고 본다.)
    """

    model = "stub"

    def judge(self, candidate: DuplicateCandidate) -> JudgeVerdict:
        da, db = candidate.domain_a, candidate.domain_b
        if da and db and da == db:
            return JudgeVerdict(
                same=True, confidence=0.9, reason="도메인root 동일(dry-run 스텁)", model=self.model
            )
        return _uncertain("dry-run 스텁: 도메인 불일치/불명 → 사람 위임", model=self.model)


class ClaudeJudge:
    """Claude(Haiku) 기반 동일기업 판정 — 미설치/오류/파싱실패 시 불확실(graceful)."""

    def __init__(self, api_key: str, *, model: str, max_tokens: int = 200) -> None:
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens

    def judge(self, candidate: DuplicateCandidate) -> JudgeVerdict:
        prompt = _PROMPT.format(
            name_a=candidate.name_a,
            domain_a=candidate.domain_a,
            name_b=candidate.name_b,
            domain_b=candidate.domain_b,
            country=candidate.country,
        )
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=self._api_key)
            msg = client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            log.info("dedup.llm_judge.call", model=self._model)  # 과금 호출 추적
            text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
            return _parse_verdict(text, model=self._model)
        except Exception as exc:  # 미설치(ImportError)·키오류·API오류 → 불확실 폴백.
            log.info("dedup.llm_judge.error", err=str(exc))
            return _uncertain(f"LLM 호출 실패: {exc}", model=self._model)


def judge_candidates(
    candidates: list[DuplicateCandidate],
    judge: SupportsJudge,
    *,
    ledger: SupportsCostLedger | None = None,
    max_pairs: int = 200,
) -> list[JudgedPair]:
    """쇼트리스트 티어 후보만 판정한다(예산 가드 + 런당 캡 + audit 로그).

    - ``JUDGE_TIERS`` 외(auto/keep_both)는 건너뛴다(이미 해소/결론).
    - ``ledger`` 가 주어지면 호출 **직전마다** ``is_over_budget`` 를 확인해 초과 시 중단한다
      (남은 후보는 판정하지 않음 — 사람 워크벤치로 남음). 판정 1건마다 ``record`` 로 과금 적재.
      스텁(model=="stub")은 무료라 과금하지 않는다(dry_run 0원 행 방지).
    - ``max_pairs`` 로 런당 유료 판정 수를 제한한다(우발적 대량 과금 방지). 초과분은 미판정.
    결과는 입력 순서(= C1 의 결정적 정렬)를 보존한다.
    """
    targets = [c for c in candidates if c.tier in JUDGE_TIERS]
    judged: list[JudgedPair] = []
    for cand in targets:
        if len(judged) >= max_pairs:
            log.warning("dedup.llm_judge.cap_reached", cap=max_pairs, remaining=len(targets) - len(judged))
            break
        paid = ledger is not None and getattr(judge, "model", "") != "stub"
        if paid and ledger.is_over_budget():
            log.warning("dedup.llm_judge.over_budget", judged=len(judged), remaining=len(targets) - len(judged))
            break
        verdict = judge.judge(cand)
        if paid:
            ledger.record(PROVIDER)
        log.info(
            "dedup.llm_judge.verdict",
            key_a=cand.key_a,
            key_b=cand.key_b,
            tier=cand.tier,
            same=verdict.same,
            confidence=verdict.confidence,
            model=verdict.model,
        )
        judged.append(JudgedPair(candidate=cand, verdict=verdict))
    return judged


def build_judge(settings, *, force_stub: bool = False) -> SupportsJudge:
    """설정에 맞는 판정기를 만든다 — dry_run/키없음/force_stub 면 :class:`StubJudge`.

    실호출(:class:`ClaudeJudge`)은 ``dry_run`` 이 꺼져 있고 ``anthropic_api_key`` 가 있을
    때만. 그 외엔 네트워크 없는 결정적 스텁으로 폴백한다(CLI·파이프라인 공용 진입점).
    """
    if force_stub or settings.dry_run or not settings.anthropic_api_key:
        return StubJudge()
    return ClaudeJudge(settings.anthropic_api_key, model=settings.dedup_llm_model)
