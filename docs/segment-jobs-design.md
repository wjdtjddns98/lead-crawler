# 세그먼트 작업 큐(트랙 S) 설계 — 확정안 (2026-08-25)

> 배경: 웹앱 관리자가 세그먼트(국가·업종·상장·지역)를 지정하면 시스템이 **발견→승격(실존게이트·이메일 추출·MX·리뷰큐 적재)** 까지 백그라운드에서 순차 처리한다. 최근 3주 수동 러너(`scripts/backfill_promote_domained.py`, `run-promote-seg-chain.ps1`)를 대체한다.
> 설계는 §7 교차설계(Codex·Claude 독립안 → 메인 종합). PO 확정(2026-08-25): 재추출 미포함 / 전 국가 허용·운영 KR 우선 / 백필 A·C 와 병행 허용.

## 1. 핵심 판단
- **승격 = 추출 = 큐적재.** `_build_lead`(`pipeline/run.py`)가 enrich→실존검증→이메일검증을 한 번에 하고 `save_lead`→`enqueue_email_review` 가 리뷰큐까지 적재한다. 별도 "추출 단계"는 트랙 A 와 같은 행 이중 처리 → **S 는 discover→promote 2단계.**
- **대상 집합 상호 배타.** S 승격 대상 = `discovered_company` 중 `company` 없음 & `domain<>''`. C = `domain=''` & company 없음. A = company 존재 & 무이메일. 병행 허용. 단 C 가 도메인 커밋 직후(`fill.py:562`) 승격 전에 S 가 같은 행을 잡는 경쟁창이 있음 — 회사 PK 가 canonical_key 결정적(`repository.company_id_for`)이라 결과는 중복 저장이 아닌 네트워크 1회 낭비. 수용·문서화.
- **새 프로세스 모델 없음.** `backfill_process._supervise`(Job Object·세대 펜싱·크래시 회로차단·예산·취소·재기동 재개)와 자식 CLI 의 `_ManagedJob`(`--job-id` 자기보고·`should_stop`)을 그대로 쓴다 → `backfill_job` 에 **트랙 S** 를 얹는다.
- **동시 1건.** `backfill_job.active_track` UNIQUE 가 "S 는 한 번에 1개"를 강제. 워커 3·배치 100·20배치마다 세대교체(현 수동 정본 값). 요청 간 병렬은 이득 없음(대상은 어차피 순차 소화).
- **폴링은 저장 카운터만.** 목록/상세는 행의 카운터만 읽는다. 원장 COUNT 는 `preview` 에서만(온디맨드).

## 2. 데이터 모델 (Alembic 1개, additive)
`backfill_job` 추가 컬럼: `listed String(16) default 'unknown'` · `regions String(512) default ''` · `priority Integer default 100`(낮을수록 먼저) · `stage String(16) default ''`(`''|discover|promote|done`) · `discovered Integer default 0` · `promote_cursor String(255) null` · `failed_items Integer default 0`.
상태 추가: `queued`, `paused`, `done`(TERMINAL 에 done 추가 — A/C 자식은 done 을 보고하지 않으므로 무영향). `track_lock._TRACK_LOCK_KEYS["S"]` 추가.
S 헬퍼(`storage/backfill_job.py`): `enqueue_segment_job`(queued, active_track NULL) · `activate_segment_job`(원자 UPDATE `WHERE status='queued'` → active_track='S', IntegrityError→BackfillBusy) · `next_queued`(ORDER BY priority, started_at) · `pause_backfill_job`(paused, active_track NULL, cancel_requested False, 커서 보존) · `requeue`(paused|failed|budget_exhausted→queued) · `set_priority`. `record_progress` 에 `stage`/`cursor`/`discovered`/`failed_items` 추가(세대 펜싱 그대로). `started_at` = 요청 생성 시각.

### 상태기계
```
queued ─activate→ running ─(stage=done, rc=0)→ done
running ─cancel→ cancelled          running ─pause→ paused        queued ─pause→ paused
paused|failed|budget_exhausted ─resume→ queued
running ─crash×3|spawn 실패→ failed  running ─월예산→ budget_exhausted
```
종료·pause 마다 디스패처가 `next_queued` 를 activate(예산 소진 상태면 대기 유지).

## 3. 프로세스 모델
- 자식: `python -m leadcrawler.cli segment-run --job-id --job-generation --batch 100 --workers 3 --max-batches 20 --stall-exit-secs 900`. 필터는 argv 가 아니라 **행에서 로드**.
- 자식 알고리즘: 트랙 S 락 → `invalid_reason` → `stage in ('', 'discover')` 면 `run_pipeline(generate_segments(countries, industries, listed, regions), persist=True, record_only=True, should_cancel=throttled(should_stop), on_progress→report(discovered))` → `report(stage='promote', remaining=count_promote_targets)` → `promote_cursor` 부터 배치 루프(`promote_batch` → persist 후 `report(processed, promoted, emails, failed_items, batches=1, cursor=last_key)`; 보고 거부(세대 불일치)시 즉시 종료) → 대상 0 이면 `report(stage='done')` exit 0 → `max_batches` 도달 시 exit 0(세대 교체).
- `run_pipeline(record_only=True)`: dedup·inline 도메인 해석 통과 후 원장 기록만 하고 pending(추출)에 넣지 않음. 발견 커서는 `discovery_cursor`(source, segment.label) 전역 커서 그대로(잡별 커서 없음). **발견 단계는 정체 워치독 없음**(세그먼트 단위 블로킹이라 beat 불가·신규 0건 구간이 정상적으로 장시간 — 취소/pause 는 세그먼트 경계에서 협조 중단). 승격 배치만 자식 워치독(rc 86→크래시 회로차단).
- supervisor 변경: `_TRACK_CMD/_TRACK_DEFAULTS/_running` 에 S. rc==0 분기에서 S 이면 행 재조회 → `stage=='done'` 이면 `_finish(DONE)`, 아니면 세대교체. cancelled 분기에서 `stop_reason=='pause'` 면 `pause_backfill_job`. `finally` 에서 S 면 `dispatch_next_segment_job`. 디스패처 호출점: API 생성/재개, `_supervise` finally, `resume_active_jobs` 말미. 경쟁은 UNIQUE + `_running` 가드.
- 재시작 복구: running S 는 `resume_active_jobs` 그대로(세대 bump, 커서에서 재개). queued 는 디스패처. failed 는 resume 으로 큐 복귀(커서 보존).
- DRY-RUN: `segment-run` 은 dry_run 에서 중단하지 않는다(A 의 게이트 미적용) — `run_pipeline`·`_build_lead` 가 결정적 더미라 테스트가 review_queue 까지 검증.

## 4. 승격 이관 — `leadcrawler/pipeline/promote.py`
스크립트의 대상 SQL·`_load_domain_guards`(점유·3건+ 과공유 도메인 차단)·`_split_multi`·워커별 Enricher/ExistenceVerifier/EmailValidator·`_build_lead`/`_persist_lead` 를 이전. `_dc_from_row` 는 `fill._dc_from_row` 재사용. `fill._scoped` 에 `regions` 절 추가. listed: `listed`→only_listed, `unlisted`→exclude_listed, `unknown`→무필터(기존 API 의미). API: `count_promote_targets(sm, countries, industries, listed, regions)` · `promote_batch(settings, sm, *, run: PromoteRun, after, limit, workers, guards, countries, industries, exclude_industries, listed, regions, stall_exit_s) -> (rows, last_key, promoted, emails, failed)` — `PromoteRun.open(settings)`(cost_ledger·registry_checker·classifier)은 **런당 1회**(LLM 호출 상한이 런당이라 배치마다 만들면 리셋됨). 커서 기록은 배치 persist **후**. 회사 1건 실패는 격리(failed_items+1, 커서 전진). 스크립트는 얇은 래퍼로 1릴리스 유예 후 삭제(트랙 S 락은 PR2 에서 키 추가 후 획득 — PR1 구간은 관리형 승격과 수동 병행 금지). import 시드는 같은 필터면 자동 포함(별도 source 필터 없음; 전량은 broad 요청 1회).

## 5. API 계약 (전부 `require_admin`, CSV 관례 = 기존 `/admin/backfill`)
| Method/Path | 요청 | 응답 | 오류 |
|---|---|---|---|
| POST `/admin/segment-jobs` | `{countries:"KR,US"(≥1), industries:"연기금,증권·자산운용"(≥1, 택소노미), listed:"unknown|listed|unlisted", regions:""|"all"|"서울,경기", priority:100}` | 201 `SegmentJobInfo`(즉시 activate 되면 running) | 422 미지원 국가/업종·`crawl_max_segments` 초과·regions 는 KR 포함 시만 |
| GET `/admin/segment-jobs?status=&limit=50&offset=0` | — | `{items:[SegmentJobInfo], total}` 정렬 running→queued(priority,started_at)→나머지 최신순 | — |
| GET `/admin/segment-jobs/{id}` | — | `SegmentJobInfo` | 404 |
| POST `/admin/segment-jobs/{id}/cancel` | — | info | 404 / 409 종료건 |
| POST `/admin/segment-jobs/{id}/pause` | — | info(running 은 수초 내 paused) | 409 (running/queued 외) |
| POST `/admin/segment-jobs/{id}/resume` | — | info(queued 또는 running) | 409 (paused/failed/budget_exhausted 외) |
| PATCH `/admin/segment-jobs/{id}` | `{priority:int}` | info | 409 (queued/paused 외) |
| GET `/admin/segment-jobs/preview?countries&industries&listed&regions` | — | `{segments:int, promote_pending:int}` (온디맨드, 폴링 금지) | 422 |

`SegmentJobInfo` = `BackfillJobInfo` + `listed, regions, priority, stage, discovered, failed_items, promote_cursor, queue_position:int|null`. 진행률 = promote 단계 `processed/initial_target`; discover 단계는 `discovered` 만. 기존 `/admin/backfill/*` 은 트랙 A/C 만 조회 → 무영향.

## 6. PR 계획 (순서 = 의존)
1. **promote 이관** — `pipeline/promote.py`, `fill._scoped(regions)`, 스크립트 래퍼화, 이 문서 `docs/`. 테스트: 기존 스크립트 테스트 import 전환 + regions 절 SQLite + 실패 격리.
2. **스키마·storage** — Alembic, `schema.py`, `storage/backfill_job.py`, `track_lock`. 테스트: activate UNIQUE 경쟁, pause/requeue 전이, stage/cursor 세대 펜싱.
3. **자식 CLI** — `cli.segment_run`, `run_pipeline(record_only)`, throttle 폴러. 테스트(dry_run·SQLite): ''→discover→promote→done, review_queue 생성, 커서 재개 시 기처리 미재처리(제약①), should_stop 커서 보존.
4. **supervisor·디스패처** — `backfill_process.py`, `resume_active_jobs` 말미 디스패치. 테스트(fake launcher): done→다음 큐 자동 시작(priority 순), pause, 재시작 재개, 예산 소진 시 큐 정지.
5. **API** — `api/admin.py`, `api/schemas.py`, `tests/test_segment_jobs_api.py`. PR 본문에 §5 표(FE 공유).
6. **러너 퇴역** — 스크립트·ps1 폐기(1릴리스 유예), 운영 문서.

## 7. 위험·함정
- Playwright 메모리: 워커 합 A2+C2+S3=7. 세대교체+정체 종료로 방어. 초과 시 S 워커 축소.
- 수동 스크립트 병행: 락 없음 → PR2(트랙 락 키 추가) 이후 래퍼가 S 락 획득. PR1~PR2 구간은 운영 합의로 동시 실행 금지.
- 커서: `promote_cursor` 는 canonical_key 순 — paused 중 새로 발견된 키(<커서)는 이번 요청이 못 봄 → 같은 세그먼트 새 요청이 회수. 발견 커서는 (source,label) 전역, 지역 팬아웃은 naver_local 만 돌아 파편화 없음.
- `_load_domain_guards`: 세대당 1회 수십 MB — 수용.
- 해외 발견 단계: 소요시간 예측 불가 — UI 경고만(코드 제한 없음).
- C→S 경쟁창: §1.

## 8. 버린 대안
신규 `segment_job`+회사별 target 테이블+전용 supervisor(300줄 복제·잡당 수만 행) / `crawl_job` 스레드 모델 확장(서버 내 Playwright·세대 리셋 불가) / S 실행 중 A·C exclusive 정지(백필 손실) / 4단계(추출 별도 = A 이중) / 요청=체인(FIFO+priority 로 동일) / S 동시 N(행 claim 없음).

## 9. 잔여 수동 작업(범위 밖)
`nps-import`(월간), `nps-map-industries`, `dart-cache-fill`.
