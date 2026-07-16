# lead-crawler

[![CI](https://github.com/wjdtjddns98/lead-crawler/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/wjdtjddns98/lead-crawler/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![FastAPI](https://img.shields.io/badge/FastAPI-webapp-009688?logo=fastapi&logoColor=white)](leadcrawler/api)
[![React](https://img.shields.io/badge/React-Vite-61DAFB?logo=react&logoColor=black)](web)

전 산업·전 기업(상장+비상장)의 IR 연락처 — 이메일·전화·문의폼 — 를 자동 수집하고,
웹앱에서 사람이 검증한 뒤 고정 엑셀 서식으로 내보내는 B2B 리드 수집 시스템.
이메일 발송은 범위 밖이다(외부 메일 솔루션 사용).

```
discover → dedup → enrich → verify → export
```

- 기본값이 dry-run — `LEADCRAWLER_DRY_RUN=true`면 외부 API 키 없이 전 과정이 시뮬레이션으로 동작
- 한 번 수집한 기업은 `canonical_key` 기준으로 다시 추출하지 않음
- 등록처 active + 사이트 생존 검증을 통과한 기업만 저장

## 빠른 시작

의존성 관리는 [uv](https://docs.astral.sh/uv/)를 쓴다.

```bash
uv sync                 # .venv + 런타임 + dev 도구(pytest, ruff, mypy)
uv run pytest -q        # 단위 테스트는 네트워크 없이 통과
uv run leadcrawler run --country KR --industry 건설 --out exports/leads.xlsx
```

기능별 extra: `--extra api`(웹앱) · `--extra db`(psycopg) · `--extra crawl`(헤드리스) ·
`--extra ocr` · `--all-extras`. 운영 설치는 `uv sync --no-dev --extra api --extra db`.

## 검증 웹앱

운영 DB는 PostgreSQL(단위 테스트는 SQLite), 스키마는 Alembic으로 관리한다.

```bash
docker compose up -d               # 로컬 PostgreSQL
uv sync --extra api --extra db
uv run leadcrawler db-upgrade      # alembic upgrade head
uv run leadcrawler web
```

프론트를 빌드해 두면(`cd web && npm install && npm run build`) 백엔드가 `web/dist`를
같은 출처로 서빙하므로 CORS나 `VITE_API_BASE` 설정이 필요 없다. 빌드가 없으면
API(`/docs`)만 동작한다.

## 구조

```
leadcrawler/
  sources/      발견 어댑터 (EDGAR, DART, 거래소, Companies House, 디렉터리, 검색 API)
  enrich/       이메일·전화·문의폼 추출 (정적 BFS → 헤드리스 → OCR/비전)
  verify/       실존성·이메일 유효성 검증
  pipeline/     discover → dedup → enrich → verify → lead
  scheduler/    24/7 크롤 오케스트레이션
  storage/      엑셀 export
  integrations/ Notion 자동 리포팅
  api/          FastAPI 검증 웹앱
web/            React(Vite) 프론트
```

## CLI

```bash
leadcrawler run --country KR --industry 건설 --persist   # 수집 결과를 DB에 영속화
leadcrawler import-existing "기존목록.xlsx"               # 기존 목록 시드(중복 방지 기준)
leadcrawler report 2026-06-18 --done "..." --next "..."  # Notion 리포팅
```

## 배포 (내부망 HTTPS)

리포 루트의 `serve-https.bat` 실행 — 첫 실행 때 자체서명 인증서를 만들고
(호스트명·로컬 IP를 SAN에 포함) `0.0.0.0:8000`에 HTTPS로 띄운다.
포트 변경은 `serve-https.bat -Port 8443`.

자체서명이라 브라우저 최초 접속 시 경고가 한 번 뜬다. 경고 없이 쓰려면
`certs\cert.pem`을 각 클라이언트의 신뢰할 수 있는 루트 인증 기관에 설치한다.

## 기술 스택

Python 3.10+ · Pydantic v2 · SQLAlchemy 2 + Alembic · FastAPI · PostgreSQL · React + Vite · uv
