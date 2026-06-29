# PRD — 검증 큐 국가·업종·상장 필터링 (Filtered Claim)

> 상태: 초안(Draft) · 작성일 2026-06-29 · 영역: 백엔드 + 프론트엔드 분리
> 관련 계약 변경: FE↔BE (엔드포인트 `GET /queue`, `POST /queue/claim` 시그니처 확장) — **양쪽 공유 필수**

---

## 1. 배경 / 문제

관리자는 검증 직원에게 **구두로** 그날의 작업 범위를 지정한다.

> 예) "오늘은 **미국 / 자산운용사 / 상장사**만 검증해주세요."

현재 검증 워크벤치는 직원이 `POST /queue/claim`("당겨가기")으로 풀에서 **상태(status)만** 기준으로 배치를 끌어온다. 국가·업종·상장 여부로 좁힐 수단이 없어, 직원은 지시받은 작업분만 골라 처리할 수 없다(섞여서 내려옴).

**목표**: 직원이 UI에서 `국가 / 업종 / 상장여부`를 골라 두면, 적재 큐(pending 풀)에서 **그 조건에 맞는 작업분만** 당겨가도록 한다. 지시는 구두이므로 시스템 강제(권한)는 아니고 **직원이 스스로 거는 세션 필터**다.

### 비목표 (Out of scope)
- 관리자가 직원별로 작업분을 **강제 배정**하는 기능(작업지시 워크오더) — §8 향후 확장으로 분리.
- 새 국가/업종 택소노미 추가 — 기존 `supported_countries()`·`supported_industries()` 그대로 재사용.
- 발송/엑셀(`/send`, `/export`)은 이미 국가·업종 필터가 있으므로 변경 없음.

---

## 2. 현재 시스템 (As-is) — 근거

| 항목 | 현재 동작 | 위치 |
|---|---|---|
| 큐 조회 | `status` + `limit/offset`만 | `leadcrawler/api/app.py:88` `list_queue` |
| 당겨가기 | 필터 없이 pending 풀에서 top-up | `leadcrawler/api/app.py:106` `claim_queue` → `storage/review.py:355` `claim_work` → `:304` `_claim_more` |
| 국가 컬럼 | `CompanyRow.country` `String(8)` ISO2 (예: `KR`,`US`) | `leadcrawler/schema.py` (CompanyRow) |
| 업종 컬럼 | `CompanyRow.industry` `String(128)` 자유문자열 (예: `금융`) | 동상 |
| **상장여부** | `DiscoveredCompanyRow.listed` `unknown/listed/unlisted` — **CompanyRow엔 없음** | `leadcrawler/schema.py:49` |
| 국가 별칭매칭 | `country_match_set()` ('KR'↔'대한민국' 별칭 확장, 소문자) | `leadcrawler/sources/countries.py:113` |
| 옵션 목록 | `GET /admin/countries`·`GET /admin/industries` (label+aliases) — FE가 이미 소비 | `leadcrawler/api/admin.py:173,183` |
| 발송/엑셀 필터 선례 | `country`/`industry` CSV 쿼리파라미터 | `app.py:170 /export`, `:206 /send/preview` |

**핵심 갭 2가지**
1. `claim_work`/`_claim_more`(당겨가기 본체)에 필터 인자가 없다.
2. **상장(`listed`)은 `CompanyRow`에 없다** → 큐에서 상장 필터를 걸려면 `CompanyRow.canonical_key → DiscoveredCompanyRow` **조인**이 필요.

**중요 제약(택소노미 한계)**: 큐의 `industry`는 크롤 시점에 태깅된 자유문자열이다. 택소노미(`sources/industry.py`)는 거칠어(예: `금융`) "자산운용사" 같은 세분류가 별도 값으로 저장돼 있지 **않을 수 있다**. 따라서 1차 범위에서 업종 필터는 **저장된 `industry` 문자열과 부분/정확 매칭**이며, 운영자가 고를 수 있는 값은 `/admin/industries`가 노출하는 실제 등록 업종으로 한정된다. ("자산운용사"를 별도 업종으로 세분하려면 크롤 세그먼트·택소노미 확장이 선행 — 본 PRD 범위 밖, §8.)

---

## 3. To-be 동작 (UX 흐름)

1. 직원이 "내 작업" 화면 상단의 **작업범위 바**에서 `국가 / 업종 / 상장여부`를 선택(다중 선택 가능, 빈값=전체).
2. 선택은 브라우저(localStorage)에 저장돼 **claim 폴링·리필 때마다 동반 전송**된다.
3. `POST /queue/claim`이 그 조건에 맞는 pending 행만 배타 배정(SKIP LOCKED 유지)해서 내려준다.
4. 직원이 필터를 **바꾸면**: 현재 점유 중 비매칭 항목을 **자동 반납(release)** 후 새 조건으로 재클레임 → 화면엔 항상 "현재 지시 범위"만 남는다.
5. (선택) 상단에 "이 조건의 잔여 pending N건" 카운트 표시 → 관리자 지시 소진 여부를 직원이 확인.

조건에 맞는 pending이 0이면 빈 목록 + "이 범위에 남은 작업이 없습니다" 안내.

---

## 4. 계약(API) 설계 — **FE↔BE 공유**

> 기존 `country`/`industry` CSV 규약(`/export`)과 **동일 표기**를 재사용해 일관성 유지.

### 4.1 `POST /queue/claim` (확장)
요청 본문(JSON, 모두 선택·빈값=전체):
```jsonc
{
  "country":  "US",            // 쉼표구분 ISO2/별칭, 빈값=전체 (country_match_set로 별칭 확장)
  "industry": "금융",          // 쉼표구분 업종, 빈값=전체 (소문자 매칭)
  "listed":   "listed"         // "" | "listed" | "unlisted" | "unknown", 빈값=전체
}
```
응답: 기존과 동일 `list[ReviewItem]` (조건에 맞는 내 점유분).
하위호환: 본문 생략/빈 객체 = 전체(현행 동작 그대로).

### 4.2 `GET /queue` (확장 — 조회/카운트 일관)
쿼리파라미터 추가: `country`, `industry`, `listed` (위와 동형, 전부 선택).
`total`도 동일 필터 적용된 카운트로 반환 → 잔여건수 표시에 사용.

### 4.3 옵션 목록 (변경 없음 — 재사용)
- `GET /admin/countries` → 국가 셀렉트 옵션(iso2/label/aliases).
- `GET /admin/industries` → 업종 셀렉트 옵션(value/label/aliases).
- 상장여부는 고정 3값(`listed`/`unlisted`/`unknown`) → FE 하드코딩.

> ⚠️ 옵션 엔드포인트는 현재 `role==admin`만 200(`/admin/*`). 직원(worker)도 필터 옵션이 필요하므로 **권한을 worker까지 허용**하거나 **비관리자용 별칭 경로**(`GET /queue/filters`)를 신설해야 한다 → §5 BE-2.

---

## 5. 백엔드 작업 (리드 + Claude) — Python 전용

> 브랜치 `feat/be-queue-filter`. 게이트: `ruff check .` + `pytest -q`. DRY-RUN 무관(DB 쿼리 로직).

- **BE-1 · 스토리지 필터** (`storage/review.py`)
  - `_claim_more`·`_my_active_rows`·`claim_work`·`query_reviews`·`count_reviews`에
    `countries: list[str] | None`, `industries: list[str] | None`, `listed: str | None` 인자 추가.
  - WHERE 절: 국가 = `func.lower(CompanyRow.country).in_(country_match_set(countries))`(선례 `/export`와 동일),
    업종 = `func.lower(CompanyRow.industry).in_({i.lower() for i in industries})`.
  - **상장 필터**: `CompanyRow.canonical_key == DiscoveredCompanyRow.canonical_key` 조인 후
    `DiscoveredCompanyRow.listed == listed`. `_claim_more`의 `SELECT ... FOR UPDATE SKIP LOCKED`에 조인이 들어가므로 **잠금이 review_queue 행에만 걸리도록** `.with_for_update(of=ReviewQueueRow, skip_locked=True)` 사용(조인 테이블 잠금 회피) — 동시성 회귀 주의(테스트 필수).
  - 필터 변경 시 비매칭 점유 반납: `release_non_matching(session, user_id, filters)` 헬퍼 신설(또는 claim 진입 시 비매칭 active 행을 release 후 top-up).

- **BE-2 · 라우트/스키마** (`api/app.py`, `api/schemas.py`)
  - `POST /queue/claim` 본문 모델 `ClaimRequest{country,industry,listed}` 추가(전부 기본 `""`).
  - `GET /queue`에 동형 쿼리파라미터 추가, `total`도 필터 반영.
  - CSV 파싱은 기존 `_split_csv` 재사용. `listed` 화이트리스트 검증(잘못된 값 422).
  - 필터 옵션 접근권: `/admin/countries`·`/admin/industries`를 worker도 200 받게 의존성 완화 **또는** `GET /queue/filters` 신설(국가+업종+상장 옵션 한 번에) — 후자 권장(관리자 라우트 오염 없음).

- **BE-3 · 테스트** (`tests/`)
  - 단위: 국가별칭(US↔미국)·업종 대소문자·listed 조인 매칭/비매칭·빈필터=전체.
  - 동시성: PG 경로에서 2직원이 **다른 필터**로 동시 claim 시 행겹침 없음 + 조인 잠금 회귀 없음.
  - 필터 전환 시 비매칭 점유 반납 검증.

- **BE-4 · 계약 문서화**: PR 본문에 새 요청/응답 스키마 명시 + 프론트에 공유(§협업 워크플로).

---

## 6. 프론트엔드 작업 (프론트 개발자 + Claude) — `web/**` 전용

> 백엔드는 이 절을 구현하지 않는다(제안만). 게이트: `npm run build`(tsc+vite).

- **FE-1 · API 클라이언트** (`web/src/api.ts`, `types.ts`)
  - `claimWork()`에 `filter?: {country?:string; industry?:string; listed?:string}` 인자 추가 → 본문으로 전송.
  - `fetchQueue()`에 동형 파라미터 추가(잔여건수용).
  - `fetchQueueFilters()`(또는 기존 `fetchCountries`/`fetchIndustries`) — BE-2 결정에 맞춤.
  - 타입: `ClaimFilter` 추가, `Listed`(이미 존재: `unknown|listed|unlisted`) 재사용.

- **FE-2 · 작업범위 바 UI** (`web/src/components/MyWork.tsx` 상단 + 기존 `MultiPicker.tsx` 재사용)
  - 국가(다중)·업종(다중)·상장여부(단일 셀렉트) 컨트롤.
  - 선택값 localStorage 보존(`lc_claim_filter`), 마운트 시 복원.
  - "현재 범위 잔여 N건" 배지(`GET /queue` total).

- **FE-3 · 클레임/리필 연동**
  - 기존 claim 폴링·자동리필 호출에 현재 필터 동반.
  - 필터 변경 시: `releaseWork()` → `claimWork(filter)` 순서로 재클레임(화면 갱신).
  - 빈 결과 시 안내문구.

- **FE-4 · 빌드/타입 게이트 green** + 계약 일치 확인(FE↔BE).

---

## 7. 경계 정리 (역할 분리 요약)

| 책임 | 백엔드 | 프론트엔드 |
|---|---|---|
| `/queue/claim`·`/queue` 필터 파라미터·검증 | ✅ | — |
| 스토리지 WHERE/JOIN·동시성·반납 로직 | ✅ | — |
| 필터 옵션 엔드포인트 권한/신설 | ✅ | — |
| 작업범위 바 UI·상태보존 | — | ✅ |
| 클레임 호출에 필터 동반·재클레임 흐름 | — | ✅ |
| 계약 스키마 합의 | ✅(정의) | ✅(소비) — PR 양쪽 공유 |

---

## 8. 향후 확장 (별도 PRD 후보)

- **관리자 작업지시(Work Order)**: 관리자가 화면에서 "오늘 범위"를 설정→직원 필터 기본값/강제. 구두지시의 디지털화.
- **업종 세분류**: "자산운용사" 등 세그먼트를 별도 업종값으로 태깅(크롤 택소노미 확장) — 현재는 거친 `금융` 수준.
- **잔여/소진 대시보드**: 국가×업종×상장 조합별 pending 잔량(관리자 진척 추적).

---

## 9. 검수 기준 (Acceptance)

1. 직원이 `US/금융/listed` 선택 → claim이 미국·금융·상장 pending만 내려준다(타국가·비상장 0건).
2. 빈 필터 = 현행 전체 동작과 동일(회귀 0).
3. 6직원 서로 다른 필터 동시 claim → 행 겹침 0(SKIP LOCKED 유지) + 조인 잠금 회귀 0.
4. 필터 변경 시 비매칭 점유가 자동 반납돼 화면엔 새 범위만 남는다.
5. `GET /queue` total이 필터 반영 카운트로 표시된다.
6. BE: `ruff` + `pytest` green / FE: `npm run build` green.
