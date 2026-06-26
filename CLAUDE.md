# lead-crawler — 작업 규칙

> 전 산업·전 기업 IR 연락처 추출 크롤러 + 검증 웹앱. (Nutti 광고 자동화와 별개 프로젝트.)

## 역할 분담 (작업자 2인 — 2026-06-25부터) *(공통)*
> 이 파일은 백엔드·프론트 양쪽이 공유한다. 아래는 **영역 기준**이며, 각 담당(과 각자의 Claude)은
> **자기 영역만** 직접 수정한다("우리/너" 같은 1인칭 아님 — 읽는 쪽 기준으로 해석하지 말 것).
- **백엔드 영역(리드 + Claude)**: Python 전부 — 파이프라인·sources·enrich·verify·storage·**FastAPI 서버 로직(라우트·스키마·인증)**·CLI·DB/Alembic 마이그레이션·dedup/entity-resolution·스케줄러·테스트.
- **프론트엔드 영역(프론트 개발자 + Claude)**: `web/**`(React/Vite), `*.tsx`, 컴포넌트·스타일·프론트 빌드/타입체크.
- **각 담당은 상대 영역을 직접 수정하지 않는다** — 필요하면 제안·PR 코멘트로만(직접 커밋 금지).
- 경계: 워크벤치 UI 트랙(예: PLAN C4)은 **백엔드가 API**, **프론트가 React 컴포넌트**. FastAPI 서버 로직은 백엔드.

### 협업 워크플로 (트렁크 기반 — 한 리포, 분기 안 나눔)
- **`dev`·역할별 장기 브랜치 안 씀.** main 하나가 트렁크. 모든 작업은 main 에서 짧은 브랜치 따서 PR.
- 브랜치명 영역 프리픽스: 백엔드 `feat/be-*`·`fix/be-*`, 프론트 `feat/fe-*`(또는 `feat/web-*`).
- 작업 시작 전 `git checkout main && git pull`. 변경은 **PR → green CI → squash 머지 → 브랜치 삭제**.
- 소유권은 `.github/CODEOWNERS` 로 표기(`web/**`→프론트, 그 외→백엔드). **상호 인간 코드리뷰는 안 한다**
  (서로 도메인을 모름) — 각자 Claude 로 리뷰하고 self-merge 한다. 그래서 PR 승인(approval) 요구는 0.
- CI(`.github/workflows/ci.yml`): 영역 path 필터로 백엔드 잡(test·pg)과 프론트 잡(web-build)을 분리하고
  단일 게이트 `ci-ok` 하나만 통과하면 머지 가능 → 백엔드 PR 은 프론트 빌드를, 프론트 PR 은 백엔드
  테스트를 **서로 안 기다린다**.
- **FE↔BE 계약(엔드포인트·요청/응답 스키마) 변경은 PR 본문에 명시 + 상대에게 공유**(양쪽 영향).
- main 브랜치 보호: **PR 필수 + `ci-ok` green 필수**(직접 push·force-push 차단). 인간 승인 0(위 사유).

## 0. 기본 동작 *(공통)*
- **소~중형 개발**(버그픽스·옵션 추가·멀티파일 수정) → 담당자가 직접 구현 + 리뷰어 1회 검증.
- **대형/보안민감**(신규 모듈·아키텍처·인증/시크릿/신뢰불가 입력) → 적대적 리뷰 2회.
- 범위 애매하면 한 번 확인, 합리적이면 추정·진행.
- 주석·커밋 메시지·PR 본문은 한국어.

## 1. 안전 가드레일 *(공통)*
- **main 직접 push 금지** — `feat/*`·`fix/*` 브랜치 + PR + green CI 경유(브랜치명 영역 프리픽스: `*-be`/`*-fe`).
- 커밋 전 로컬 게이트 green 필수 — **백엔드**: `ruff check .` + `pytest -q` / **프론트**: `npm run build`(tsc+vite)(+lint).
- Conventional commit + `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` 트레일러.

## 2. 백엔드 — 핵심 계약(DRY-RUN 우선) *(백엔드 전용)*
`LEADCRAWLER_DRY_RUN=true`(기본)면 외부 키 없이 전 파이프라인이 결정적 시뮬레이션으로 동작.
모든 외부 연동(sources·enrich·verify·integrations)은 dry_run 분기에서 네트워크 없이 더미 반환.
단위 테스트는 네트워크 없이 통과. 도메인 모델=Pydantic v2, ruff line-length=100.

## 3. 백엔드 — 제약(절대 위반 금지) *(백엔드 전용)*
- ① 이미 검색한 기업(기존 import 포함)은 `canonical_key` 로 재추출하지 않는다.
- ② 현 시점 실존(active+도메인 생존) 기업만 저장한다.
- 이메일 role: IR 우선 + contact/info/common 허용, **HR·언론(press/media/pr) 배제**.
- 출력은 고정 엑셀 12컬럼 서식(E·J·K 대문자 O/X, 이메일 없고 폼만 있으면 J="사이트 내 문의폼").

## 4. 백엔드 — 운영 *(백엔드 전용)*
- 예산: 월 운영비 ~50만원 한도(`cost_ledger` 추적).
- Notion 운영 서식(일일보고·스크럼·현황)은 **시스템이 자동 기입**(사람 수작업 0).

## 5. 프론트엔드 *(프론트 전용 — 프론트 개발자가 관리)*
> 백엔드(리드+Claude)는 이 섹션을 채우거나 `web/**` 를 수정하지 않는다. 프론트 개발자가 관리한다.
> (더 세분화가 필요하면 `web/CLAUDE.md` 를 따로 두면 그 디렉터리에서 자동 적용된다.)
- 스택/컨벤션: (프론트 개발자 작성 — 예: React/Vite, TS strict, 상태관리, 스타일 규칙)
- 게이트: `npm run build`(tsc --noEmit + vite build) green. (lint 도입 시 추가)
- API 연동: 백엔드 계약(엔드포인트·스키마)에 맞춰 호출. 계약 변경 PR 은 양쪽 공유(§협업 워크플로).
