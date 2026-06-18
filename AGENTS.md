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
