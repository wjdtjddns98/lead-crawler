# lead-crawler — 작업 규칙

> 전 산업·전 기업 IR 연락처 추출 크롤러 + 검증 웹앱. (Nutti 광고 자동화와 별개 프로젝트.)

## 역할 분담 (작업자 2인 — 2026-06-25부터)
- **백엔드(리드 + Claude 담당)**: Python 전부 — 파이프라인·sources·enrich·verify·storage·**FastAPI 서버 로직(라우트·스키마·인증)**·CLI·DB/Alembic 마이그레이션·dedup/entity-resolution·스케줄러·테스트.
- **프론트엔드(별도 프론트 개발자 담당)**: `web/**`(React/Vite), `*.tsx`, 컴포넌트·스타일·프론트 빌드/타입체크. **우리는 일절 관여하지 않는다** — 필요해도 제안만 하고 직접 수정 금지.
- 경계: 워크벤치 UI 트랙(예: PLAN C4)은 **백엔드 API만** 우리가, React 컴포넌트는 프론트 담당. FastAPI는 백엔드. CI "프론트엔드 빌드(타입체크)" 잡이 우리 백엔드 변경과 무관하게 깨져도 우리가 고치지 않는다.

## 0. 기본 동작
- **소~중형 개발**(버그픽스·옵션 추가·멀티파일 수정) → 리드가 직접 구현 + 리뷰어 1회 검증.
- **대형/보안민감**(신규 모듈·아키텍처·인증/시크릿/신뢰불가 입력) → 팀 워크플로.
- 범위 애매하면 한 번 확인, 합리적이면 추정·진행.

## 1. 안전 가드레일
- **main/dev 직접 push 금지** — `feat/*`·`fix/*` 브랜치 + PR + green CI 경유.
- 커밋 전 `ruff check .` + `pytest -q` 둘 다 green 필수.
- Conventional commit + `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` 트레일러.

## 2. 핵심 계약 — DRY-RUN 우선
`LEADCRAWLER_DRY_RUN=true`(기본)면 외부 키 없이 전 파이프라인이 결정적 시뮬레이션으로 동작.
모든 외부 연동(sources·enrich·verify·integrations)은 dry_run 분기에서 네트워크 없이 더미 반환.
단위 테스트는 네트워크 없이 통과. 도메인 모델=Pydantic v2, 주석·docstring 한국어, ruff line-length=100.

## 3. 제약(절대 위반 금지)
- ① 이미 검색한 기업(기존 import 포함)은 `canonical_key` 로 재추출하지 않는다.
- ② 현 시점 실존(active+도메인 생존) 기업만 저장한다.
- 이메일 role: IR 우선 + contact/info/common 허용, **HR·언론(press/media/pr) 배제**.
- 출력은 고정 엑셀 12컬럼 서식(E·J·K 대문자 O/X, 이메일 없고 폼만 있으면 J="사이트 내 문의폼").

## 4. 운영
- 예산: 월 운영비 ~50만원 한도(`cost_ledger` 추적).
- Notion 운영 서식(일일보고·스크럼·현황)은 **시스템이 자동 기입**(사람 수작업 0).
