# AGENTS — lead-crawler 프로젝트 개요

## 파이프라인 (5단계 + 산출)

`discover` → `dedup` → `enrich` → `verify` → `store` → `export(엑셀)`

- **discover** (`sources/`): 벌크 데이터셋/API 로 회사명 발견(사이트 1건씩 크롤 아님).
- **dedup** (`dedup.py`): `canonical_key`(registry_id → 도메인 → 이름+국가)로 중복 제거. 기존 import 시드 포함.
- **enrich** (`enrich/`): 아는 회사 홈페이지에서 IR이메일·전화·문의폼 추출(BFS→헤드리스→OCR/비전→폼).
- **verify** (`verify/`): 실존성(existence) + 이메일 유효성(email_validator).
- **store** (`schema.py`, PostgreSQL) / **export** (`storage/export.py`, 고정 엑셀 서식).

## Key Files

- `config.py` — pydantic-settings, `LEADCRAWLER_*` 환경변수, dry_run 기본 True.
- `models.py` — 도메인 모델(Company/Contact/CompanyLead 등), enum(EmailRole 등).
- `emailrules.py` — role 분류 + HR/언론 배제 + IR 우선 채택.
- `excel_format.py` — 12컬럼 서식·O/X 규칙(export/import 공유).
- `integrations/notion.py` — Notion 자동 리포팅(일일보고·스크럼·현황).

## dry_run 계약

모든 외부 연동은 `settings.dry_run` 분기에서 네트워크 없이 결정적 더미 반환.
실 경로는 별도 분기. 테스트는 `tests/conftest.py` 가 dry_run 강제 + 네트워크 차단.

## 규칙

한국어 주석·docstring, `from __future__ import annotations`, ruff line-length=100,
`ruff check .` + `pytest -q` green 후 커밋.

## 운영 제약 (전수리뷰 2026-07-13)

- **웹 서버는 단일 uvicorn worker 전용.** 크롤 동시 1건 가드(`_running`)와 워치독의
  스레드 생존 판정이 프로세스 로컬이라, `--workers 2+` 로 띄우면 다른 worker 의
  워치독이 정상 크롤을 죽은 잡으로 오판해 reap 한다(2026-09-01 제거 — 세그먼트
  작업 큐로 일원화). `leadcrawler web` CLI 는 workers 옵션 자체가 없어 안전 —
  **uvicorn 을 직접 띄울 때도 workers 를 늘리지 말 것.**
  (다중 프로세스가 필요해지면 DB 리스/하트비트로 전환이 선행돼야 한다.)
- **전체 `pytest -q` 는 저장소 루트에 `.env`(라이브 키) 없는 체크아웃에서 돌릴 것.**
  클린 워크트리 실측 ~80초. 루트 `.env` 가 있으면 라이브 경로 테스트가 실 네트워크를
  타서 행/실패한다(>10분 실측, 기존 베이스라인) — 로컬은 타깃 테스트, 전체는 CI 가 권위.
