# PRD — 검증 큐 "작업 받기" 영구 배정 (Permanent Claim)

> 상태: 확정(Approved) · 작성일 2026-07-02 · 영역: 백엔드 + 프론트엔드 분리
> 관련 계약 변경: FE↔BE (**`POST /queue/release` 삭제**, `GET /queue` 동작 변경, 관리자 회수 신설) — **양쪽 공유 필수**

---

## 1. 배경 / 결정

현재 "작업 받기"(claim)는 **임대(lease) 모델**이다: 직원이 배치를 점유해도 TTL(30분)이 지나면
점유가 만료돼 다른 직원이 가져갈 수 있고, `POST /queue/release`로 반납도 가능하다.

운영 판단(PO 확정 2026-07-02): **반납할 이유가 없다.** 한 번 받아간 작업은 처리(확정/거부)
전까지 그 계정 책임으로 남는 게 맞다. 따라서 다음 3가지로 확정한다.

1. **영구 배정**: 받아간 항목은 확정/거부 처리 전까지 계정에 영구 귀속. 반납·TTL 만료 회수 **없음**.
   로그아웃·탭 닫기·재로그인해도 내 작업분 그대로 유지.
2. **회수는 관리자만**: 퇴사·장기부재 등으로 방치된 점유는 관리자가 계정 단위로 회수해 풀로 되돌린다.
3. **배치 30개**: 1회 "작업 받기" 시 30개씩(기존 15). 총량 상한도 30 — **받은 걸 다 처리해야 다음을 받는다.**
4. **전체큐에서 점유 항목 숨김**: 누군가 받아간 항목은 전체큐 목록·건수에서 **아예 보이지 않는다**
   (전체큐 = "아직 아무도 안 받아간 작업"만).

### 비목표 (Out of scope)
- 하트비트/리스 연장 — 영구 배정이라 불필요(v1 설계에서 폐기).
- 직원 단건 반납("패스") — 반납 자체가 없음. 못 하는 항목은 거부(reject)로 처리.
- 관리자 강제 배정(work order) — 별도 트랙.

---

## 2. 현재 시스템 (As-is) — 근거

| 항목 | 현재 동작 | 위치 |
|---|---|---|
| 작업 받기 | `POST /queue/claim` — target(15)까지 top-up, SKIP LOCKED 배타 배정 | `api/app.py` `claim_queue` → `storage/review.py` `claim_work` |
| TTL 만료 회수 | `claimed_at` 30분 경과 시 타인이 재클레임 가능 | `storage/review.py` `_claim_more`/`_expiry` |
| 반납 | `POST /queue/release` (전체 반납) + 필터 전환 시 비매칭 자동 반납 | `release_my_claims` / `release_non_matching` |
| 충돌 백스톱 | 타인이 **활성**(TTL 이내) 점유 중이면 confirm/reject 409 | `set_review_status` |
| 전체큐 | 점유 여부와 무관하게 전부 표시 | `query_reviews`/`count_reviews` |
| 관리자 권한 | `role == 'admin'` + `require_admin` 의존성 존재 | `api/auth.py`, `api/admin.py` |

---

## 3. To-be 동작 (UX 흐름)

1. 직원이 "작업 받기" 클릭 → 내 점유가 **30개**까지 채워진다(부족분만 신규 배정).
2. 받은 항목은 **처리 전까지 내 것** — 시간이 지나도, 로그아웃해도 안 뺏긴다. 재로그인하면 그대로 복원.
3. 국가/업종/상장 **작업범위 필터는 신규 배정에만** 적용된다. 필터를 바꿔도 이미 받은 항목은
   반납되지 않고 내 작업분에 계속 남는다(총량 30 상한은 필터와 무관한 전역).
4. 전체큐 화면에는 **미점유 pending만** 보인다. 누가 받아가면 그 즉시 목록·건수에서 사라진다.
5. 관리자는 계정 관리 화면에서 각 계정의 **점유 건수**를 보고, 방치된 계정의 점유를 **회수**
   버튼으로 풀에 되돌린다(회수분은 즉시 다른 직원이 받을 수 있음).

---

## 4. 계약(API) 설계 — **FE↔BE 공유**

### 4.1 삭제: `POST /queue/release` ❌
- 엔드포인트 자체가 사라진다(404). **FE는 이 호출을 전부 제거**해야 한다
  (로그아웃 훅·필터 전환 시 release 호출 등 포함).

### 4.2 변경: `POST /queue/claim`
- 요청/응답 스키마는 **그대로**(`ClaimRequest{country,industry,listed}` → `list[ReviewItem]`).
- 의미 변경:
  - 배치가 15 → **30**.
  - 응답에는 **필터와 무관하게 내 점유 전체**가 담긴다(필터는 신규 배정분에만 적용).
    → 필터를 바꾼 직후엔 이전 범위 항목 + 새 범위 항목이 섞여 내려올 수 있음(정상).
  - 내 점유가 이미 30이면 신규 배정 0 — 기존 30개가 그대로 내려온다.

### 4.3 변경: `GET /queue` (전체큐)
- 파라미터·응답 스키마 그대로. 단, **점유 중인 행은 items·total에서 제외**된다.
- 전체큐의 pending 건수 = "받아갈 수 있는 잔여 작업 수"로 의미가 명확해짐.

### 4.4 변경: confirm/reject 409 조건
- 타인이 점유한 항목은 시간 경과와 무관하게 **항상 409** (기존: TTL 지나면 처리 가능).
- 409 메시지: "다른 직원이 처리 중인 항목입니다."

### 4.5 신설: `POST /admin/users/{user_id}/reclaim` (관리자 전용)
```jsonc
// 요청 본문 없음. 응답:
{ "reclaimed": 12 }   // 해당 계정의 pending 점유를 전부 풀로 회수한 건수
```
- 403(비관리자) / 404(계정 없음). 회수 이력은 감사 로그(action="reclaim")에 남는다.

### 4.6 변경: `GET /admin/users` 응답 필드 추가 (하위호환 — 필드 추가만)
```jsonc
{ ..., "claimed": 12 }   // 이 계정이 현재 점유 중인 pending 건수 — 회수 판단용
```

---

## 5. 백엔드 작업 (리드 + Claude) — Python 전용

> 브랜치 `feat/be-claim-permanent`. 게이트: `ruff check .` + `pytest -q`. 대부분 **삭제**라 diff 작음.

- **BE-1 · TTL/반납 제거** (`storage/review.py`, `config.py`)
  - `_claim_more`: 배정 조건을 `claimed_by IS NULL`만으로(만료 재클레임 삭제). SKIP LOCKED 유지.
  - `_my_active_rows`: 만료 필터 삭제 + **작업범위 필터 인자 삭제**(내 점유 전체 반환).
  - `release_my_claims`·`release_non_matching`·`_expiry` 삭제.
  - `set_review_status`: 백스톱을 "타인 점유면 무조건 409"로 단순화(`claim_ttl_minutes` 인자 삭제).
  - 설정: `review_claim_batch` 15→**30**, `review_claim_ttl_minutes` 삭제.
- **BE-2 · 전체큐 숨김** (`storage/review.py`)
  - `query_reviews`·`count_reviews`에 `claimed_by IS NULL` 조건 추가(점유 행 제외).
  - 단건 `GET /queue/{id}`는 그대로(딥링크·처리 화면용).
- **BE-3 · 관리자 회수** (`storage/review.py`, `api/admin.py`, `storage/audit.py`, `api/schemas.py`)
  - `admin_reclaim(session, user_id, actor)` 신설 — 해당 계정 pending 점유 전부 해제 + 감사행(action="reclaim").
  - `POST /admin/users/{user_id}/reclaim` 라우트(require_admin).
  - `user_stats`에 계정별 현재 점유 건수(`claimed`) 추가 → `UserStatsItem.claimed`.
- **BE-4 · 라우트 정리** (`api/app.py`)
  - `POST /queue/release` 삭제. `claim_queue`·`_set_status`에서 TTL 인자 제거. docstring 갱신.
- **BE-5 · 테스트** (`tests/test_claim.py`, `test_pg_integration.py`)
  - 유지: 배타 배정·top-up 멱등·확정 후 리필.
  - 교체: TTL 만료 재클레임 → **시간 경과해도 안 뺏김** / 반납 → **관리자 회수** /
    필터 전환 반납 → **필터 전환해도 점유 유지 + 전역 30 상한** / 전체큐 점유 숨김.

## 6. 프론트엔드 작업 (프론트 개발자 + Claude) — `web/**` 전용

> 백엔드는 이 절을 구현하지 않는다(제안만). 게이트: `npm run build`(tsc+vite).

- **FE-1 · release 호출 전면 제거**: `releaseWork()` 류 API 함수·호출부(로그아웃 훅, 필터 전환 흐름,
  작업 종료 버튼 등) 삭제. 남아 있으면 404.
- **FE-2 · 내 작업분 = 세션 무관 영속**: 로그인 시 `POST /queue/claim` 한 번으로 기존 작업분이
  그대로 복원됨을 전제로 화면 구성. "반납" 계열 버튼 제거.
- **FE-3 · 필터 UX 문구**: 작업범위 필터는 "**새로 받아올 작업**의 범위"임을 UI에 명시
  (바꿔도 기존 작업분은 유지된다는 안내). 혼합 표시가 싫으면 클라이언트단 표시 필터는 자유.
- **FE-4 · 전체큐**: 점유 항목이 서버에서 이미 제외되므로 FE 변경 없음(건수 의미만 인지).
  잔여 0 안내 문구는 "받아갈 수 있는 작업 없음"으로.
- **FE-5 · 관리자 화면**: 계정 목록에 `claimed`(점유 건수) 컬럼 + "회수" 버튼
  (`POST /admin/users/{id}/reclaim`, 확인 다이얼로그 권장) → 성공 시 `{reclaimed:n}` 토스트.
- **FE-6 · 409 처리**: confirm/reject 409 시 토스트 + 해당 항목 목록 제거 후 재클레임.

---

## 7. 경계 정리 (역할 분리 요약)

| 책임 | 백엔드 | 프론트엔드 |
|---|---|---|
| TTL/반납 로직 제거·영구 배정 | ✅ | — |
| 전체큐 점유 숨김(서버 쿼리) | ✅ | — |
| 관리자 회수 API·점유 건수 통계 | ✅ | — |
| release 호출 제거·반납 UI 제거 | — | ✅ |
| 관리자 회수 버튼·점유 컬럼 | — | ✅ |
| 계약 스키마 합의 | ✅(정의) | ✅(소비) — PR 양쪽 공유 |

---

## 8. 트레이드오프 (PO 인지 완료)

1. **방치 점유는 관리자 회수 전까지 잠김** — TTL 자동 복귀가 없으므로, 퇴사/부재 계정의 점유는
   관리자가 회수해야 풀린다(§4.5). 계정 비활성화가 점유를 자동 해제하지는 않는다(회수 별도).
2. **직원 스스로 손에서 뗄 방법은 거부뿐** — 애매한 항목도 확정/거부 중 하나로 종결해야 다음
   배치를 받는다(총량 30 상한). 이는 "받은 건 책임진다" 원칙의 의도된 결과.
3. **필터 전환 시 혼합 작업분** — 이전 범위 항목이 반납되지 않고 남는다(§4.2). 지시 범위가
   바뀌면 기존 잔여분을 먼저 소진하는 운영을 전제.

---

## 9. 검수 기준 (Acceptance)

1. 직원 A가 받은 항목은 31분+ 경과 후에도 직원 B의 claim에 안 내려오고, B의 confirm 시도는 409.
2. `POST /queue/claim` 1회에 최대 30개, 이미 30개 보유 시 신규 배정 0(기존 30개 반환).
3. 필터를 KR→US로 바꿔도 KR 점유가 유지되고, 신규 배정은 US만 + 총량 30 상한.
4. 점유된 항목은 `GET /queue` items·total에서 제외된다.
5. `POST /admin/users/{id}/reclaim` 후 그 항목들을 다른 직원이 받을 수 있고, 감사 로그에 reclaim이 남는다.
6. `GET /admin/users`에 `claimed` 건수가 나온다. `POST /queue/release`는 404.
7. BE: `ruff` + `pytest` green / FE: `npm run build` green.
