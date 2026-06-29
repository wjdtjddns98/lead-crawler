"""수집 시점 인라인 렉시컬 중복 후보 탐지(opt-in, 갭1).

도메인 없는(``name:`` 티어) 신규 기업은 ``canonical_key`` 정확일치·도메인 동치로는
중복이 안 잡힌다(이름 표기만 미세하게 달라도 다른 key). 배치 ``dedup-report`` 가
국가+이름prefix 블로킹 + rapidfuzz 토큰셋으로 이런 렉시컬 근접쌍을 잡지만, 오프라인
실행에서만 뜬다. 이 모듈은 같은 판정을 **크롤 시점**에 돌려, 유사쌍을
``dedup_candidate``(워크벤치 pending)로 즉시 적재한다.

**자동 스킵/머지하지 않는다** — 동명이인(이름 유사·실제 다른 기업) 리드손실 방지(제약②)라
사람 확정에 위임한다. 추출·저장은 평소대로 진행되고, 여기선 후보만 남긴다.

블로킹·임계값·점수는 배치(:mod:`near_dup`)와 단일 출처를 공유해 인라인/배치 판정이
일관된다.
"""

from __future__ import annotations

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..dedup import tokenize_name
from ..schema import DiscoveredCompanyRow
from ..storage.dedup_candidate import upsert_candidate
from .near_dup import MAX_BLOCK_SIZE, NAME_MEDIUM, NAME_PREFIX_BLOCK, NAME_STRONG


def _block_key(country: str, tokens: list[str]) -> tuple[str, str]:
    """국가(소문자) + 정규화 이름 prefix 로 블록 버킷을 만든다(배치와 동일 규칙)."""
    return (country.strip().lower(), "".join(tokens)[:NAME_PREFIX_BLOCK])


class InlineLexicalMatcher:
    """``name:`` 티어 발견 원장을 블록 인덱스로 들고, 신규 기업과 렉시컬 대조한다.

    런 시작 시 기존 ``name:%`` 행을 한 번 적재하고, 처리하는 신규 기업도 인덱스에 더해
    같은 런 안의 후속 기업과도 대조되게 한다(메인 스레드 전담 — 동시성 안전).
    """

    def __init__(self, session: Session) -> None:
        self._blocks: dict[tuple[str, str], list[tuple[str, list[str]]]] = {}
        rows = session.execute(
            select(
                DiscoveredCompanyRow.canonical_key,
                DiscoveredCompanyRow.name,
                DiscoveredCompanyRow.country,
            ).where(DiscoveredCompanyRow.canonical_key.like("name:%"))
        ).all()
        for key, name, country in rows:
            self._index(key, name, country or "")

    def _index(self, key: str, name: str, country: str) -> None:
        tokens = tokenize_name(name)
        if not tokens:
            return
        self._blocks.setdefault(_block_key(country, tokens), []).append((key, tokens))

    def consider(self, session: Session, key: str, name: str, country: str) -> int:
        """신규 ``name:`` 티어 기업을 블록 내 기존 기업과 대조해 후보를 적재한다.

        토큰셋 유사도 ≥ ``NAME_MEDIUM`` 인 쌍을 ``dedup_candidate``(pending)로 upsert
        (NAME_STRONG 이상=lexical, 그 사이=shortlist). 생성/갱신된 후보 수를 반환한다.
        """
        tokens = tokenize_name(name)
        if not tokens:
            return 0
        bucket = _block_key(country, tokens)
        peers = self._blocks.get(bucket, [])
        created = 0
        # 블록 폭발(동일 prefix 군집) 방지 — 배치와 같은 캡. 초과 블록은 배치 리포트가 담당.
        if len(peers) < MAX_BLOCK_SIZE:
            joined = " ".join(tokens)
            for peer_key, peer_tokens in peers:
                if peer_key == key:
                    continue
                score = round(fuzz.token_set_ratio(joined, " ".join(peer_tokens)), 1)
                if score < NAME_MEDIUM:
                    continue
                if score >= NAME_STRONG:
                    tier, reason = "lexical", "이름 高·도메인 불명(수집 inline)"
                else:
                    tier, reason = "shortlist", "이름 중간 유사(수집 inline)"
                if upsert_candidate(
                    session, key, peer_key, tier=tier, name_score=score, reason=reason
                ):
                    created += 1
        self._index(key, name, country)  # 같은 런 후속 기업과도 대조되도록 인덱스에 추가.
        return created
