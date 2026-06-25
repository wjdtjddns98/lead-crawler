# 개발 계획 — WAF우회 임베드 + 중복해소(Entity Resolution) 파이프라인

> 합의일 2026-06-25. 이 문서가 **정본**이다. 세션 초기화 후 새 세션은 이 파일 + 메모리(`dev-plan-entity-resolution`, `insane-search-embed-todo`)로 이어받는다.

## ▶ 착수(Kickoff) — 새 세션은 여기부터
1. `git checkout -b feat/entity-resolution` (main 직접 금지).
2. **1단계**: `Track C0`(마이그레이션) + `Track C1`(배치 중복리포트) 부터. 기존 `discovered_company` 20,179건 위에서 도는 **오프라인 배치잡**이라 수집 파이프라인 무관, 즉시 가능.
3. 병행: `Track A1`(제작자 동의 메시지 — 비차단).
4. 규모상 **팀 워크플로 권장**(리뷰어 ≥1). 커밋 전 `ruff check .` + `pytest -q` green 필수.

---

## 0. 불변 원칙 (전 단계 공통)
dry_run 우선 · 제약①(`canonical_key` 재추출 금지) · 제약②(실존만 저장) · `cost_ledger` 예산(월 ~50만원) · 사람 검증 워크벤치 · **전부 opt-in 플래그 + dry_run 스텁** · 기존 테스트 green · `feat/*` + PR + green CI · Conventional commit + `Co-Authored-By` 트레일러.

## 1. 목표 데이터 흐름
```
원천 수집(source 라벨 보존, JSON)
  → admit 게이트: 실접속 생존 + (이메일 OR 문의폼)      # 이메일 필수 아님 — PR#38 결정
  → 캐노니컬 회사명 라벨링
  → 중복해소 사다리 → 골든레코드 1건/회사
  → 엑셀 12컬럼 문서화 전달
```

## 2. 중복 판정 에스컬레이션 사다리 (enrich 사다리와 동일 철학)
```
canonical_key(정확·무료)
  → 도메인 root/리다이렉트 일치(무료)
  → 렉시컬 near-dup(rapidfuzz, 무료·결정적)
  → Claude 판정(Haiku API, 유료 — 못 가른 "쇼트리스트"만)
  → 사람 워크벤치(Claude 'uncertain'·경계만)
```
- 자동제거는 **최상위 티어(이름 高 + 도메인 root/리다이렉트 일치)만**. 나머지·확인불가는 워크벤치(제약② 리드손실 방지).
- 동명이인: 이름유사도≈100%라 1차로 못 가름 → 도메인이 명백히 다르면 **둘 다 유지**.

## 3. 기존 코드 매핑 (재사용 vs 신규)
| 그대로 (변경 ~0) | 신규 (additive·플래그 뒤) |
|---|---|
| `pipeline/run.py` 수집 파이프라인 | `existence` 헤드리스 실접속 검증 (Track B) |
| `dedup.py` `canonical_key`(1차 정확필터 유지) | `dedup_resolve/near_dup.py`(블로킹+렉시컬) |
| `storage/export.py` 엑셀 12컬럼 | `dedup_resolve/llm_judge.py`(Claude 판정) |
| `cost_ledger`·`audit`·워크벤치 골격 | `dedup_resolve/golden.py`(캐노니컬명+survivorship) |
| `enrich/vision.py`(Claude API 패턴 선례) | 워크벤치 "중복후보" 탭(api+react) |

---

## Track A — insane-search 임베드 (발견 커버리지) · 메모리 TODO#1
엔진 경로: `C:/Users/WSCOPY/.claude/plugins/cache/gptaku-plugins/insane-search/0.4.1/skills/insane-search/engine` (MIT, 제작자 fivetaku).

| 단계 | 내용 | 크기 |
|---|---|---|
| A1 | 제작자 동의·attribution 메시지(병행, 비차단 — MIT라 진행 가능). github.com/fivetaku/gptaku_plugins Issue | XS |
| A2 | 엔진 **벤더링**: `leadcrawler/sources/_bypass/`에 버전 핀 복사 + MIT 고지, `[bypass]` extra(curl_cffi·beautifulsoup4·pyyaml) | S |
| A3 | `InsaneFetcher(SupportsFetch)` 어댑터: `engine.fetch(url)→FetchResult.content`(ok일 때), `ok=False`→graceful 빈/예외. dry_run no-op | S |
| A4 | SET/Bursa `_live`를 InsaneFetcher 주입으로 전환 + 오프라인 테스트(FakeFetcher canned HTML) | M |
| A5 | 한계 문서화: 엔진 `playwright_mcp` tier는 Claude 세션 MCP 의존 → 임베드 미동작(graceful). curl_cffi 격자 + 로컬 Playwright은 정상 | XS |
| — | **PoC 미완 상태**: 엔진 import OK, `.venv`에 curl_cffi 미설치. 재개: `pip install curl_cffi beautifulsoup4 pyyaml` 후 `PYTHONPATH=<plugin>/skills/insane-search python -m engine <SET_URL> --json --trace` | |

## Track B — 실접속(헤드리스) 검증 강화
| 단계 | 내용 | 크기 |
|---|---|---|
| B1 | `verify/existence.py`에 헤드리스 렌더 확인 추가(`SupportsRender` 주입, 플래그 `verify_headless`) — 파킹/JS전용 사이트 거름. insane-search 렌더러 재사용 | S~M |
| B2 | HEAD 차단(405)→GET 폴백 + 파킹페이지 휴리스틱 | S |

## Track C — Entity Resolution (핵심 신규 덩어리)
| 단계 | 내용 | 크기 |
|---|---|---|
| C0 | Alembic 마이그레이션: `canonical_name`, `duplicate_of`(or cluster_id), 머지 audit 컬럼 (additive) | S |
| **C1** | **배치 중복리포트(먼저!)** — 기존 20,179건 위 오프라인 잡. 블로킹(국가+도메인root+이름prefix) + `near_dup.py`(rapidfuzz char-ngram/token-set, 결정적) + 도메인 근접(브랜드root/리다이렉트, existence 재사용) → 중복후보 쌍 리포트. CLI `leadcrawler dedup-report` | M |
| C2 | `llm_judge.py` — 사다리에서 못 가른 **쇼트리스트만** Claude API(Haiku) grounded 비교(이름·도메인·국가·사이트제목 제공) → `{same, confidence, reason}`. 플래그 `dedup_llm_judge` + dry_run 스텁 + cost_ledger + audit | M |
| C3 | 캐노니컬 라벨 + 골든레코드 survivorship(이름=등록처 법인명/현지어 우선, 도메인=권위소스, 이메일=IR>contact) | S~M |
| C4 | 워크벤치 "중복후보" 탭 — Claude `uncertain`·경계 케이스 사람 확정/분리 (api+react+review) | M |
| C5 | 수집 파이프라인 inline 승격(`run.py` 신규 리드 적재 시 통합 적용) | S~M |

---

## 4. 권장 실행 순서 (리스크·체감 최소)
1. **C0 + C1(배치 리포트)** + **A1(동의 메시지, 병행)** → 기존 데이터로 즉시 중복 실태 파악, 파이프라인 무관.
2. **A2~A4(insane-search 임베드)** → SET/Bursa 등 막힌 발견 복구.
3. **C2(Claude 판정) + C4(워크벤치 탭)** → 자동/사람 중복 해소 가동.
4. **B(헤드리스 검증) + C3(골든레코드) + C5(inline 승격)** → 정밀도·자동화 완성.

## 5. 신규 파일(예상)
`sources/_bypass/*`(벤더) · `sources/insane_fetcher.py` · `dedup_resolve/near_dup.py` · `dedup_resolve/llm_judge.py` · `dedup_resolve/golden.py` · CLI `dedup-report` · alembic 1~2개 · `web/src/components/Duplicates.tsx` + 각 테스트.

## 6. 리스크 / 결정사항
- **규모**: 중형 1덩어리(+임베드). 신규 외부연동(Claude·bypass)·신뢰불가 입력·full-stack → 팀 워크플로 권장.
- Claude 판정은 쇼트리스트만 → 예산 안. **구독 auth 아님 — API+Haiku**(`ANTHROPIC_API_KEY`).
- 자동제거 최상위 티어만, 가역(audit 로그).
- 한계: insane-search MCP tier 임베드 미동작. WAF 우회는 대상 사이트 ToS 판단 사용자 몫(상장사 공개데이터·정당 용도라 리스크 낮음).
