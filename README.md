# lead-crawler

전 산업·전 기업(상장+비상장)의 **IR 연락처·이메일·문의폼**을 24/7 자동 추출하고, 직원이
최소 인원으로 **검증만** 하도록 만드는 B2B 리드 수집 시스템. 결과는 고정 엑셀 서식으로 산출한다.

> 자산운용사의 IR 콜드메일 대상 DB 구축용. **이메일 발송은 본 시스템 밖**(외부 메일솔루션).

## 핵심 원칙

- **dry_run 우선** — `LEADCRAWLER_DRY_RUN=true`(기본)면 외부 키 없이 전 파이프라인이 결정적 시뮬레이션.
- **중복 금지** — 이미 검색한 기업(기존 엑셀/CSV import 포함)은 `canonical_key` 로 재추출하지 않음.
- **실존만** — 등록처 active + 도메인/홈페이지 생존 검증 통과분만 저장.
- **고품질** — 멀티소스 교차검증 + 신뢰도(confidence)로 사람 검증 부담 최소화.

## 구조

```
leadcrawler/
  config.py logging.py models.py dedup.py emailrules.py excel_format.py importer.py
  sources/   발견 어댑터(EDGAR/DART/거래소/CH/디렉터리/검색API)
  enrich/    IR이메일·전화·문의폼 추출(BFS→헤드리스→OCR/비전→폼)
  verify/    실존성·이메일 유효성 검증
  pipeline/  discover→dedup→enrich→verify→lead
  scheduler/ 24/7 오케스트레이션
  storage/   export(고정 엑셀 서식)
  integrations/ notion(자동 리포팅)
  api/       FastAPI 검증 웹앱
web/         React(Vite) 프론트 → Vercel
```

## 개발

```bash
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -e ".[dev]"
ruff check .
pytest -q
```

## CLI

```bash
leadcrawler run --country KR --industry 건설 --out exports/leads.xlsx
leadcrawler import-existing "기존목록.xlsx"
leadcrawler report 2026-06-18 --done "..." --next "..."   # Notion 자동 리포팅
```
