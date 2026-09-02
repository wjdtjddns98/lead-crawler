// 백엔드 API DTO 와 1:1 대응하는 타입(leadcrawler/api/schemas.py 참조).

export type ReviewStatus = "pending" | "confirmed" | "rejected";

// 엑셀 추출 대상 상태(#462) — GET /export?status=. pending 은 BE 가 422 로 거절하므로
// ReviewStatus 를 그대로 쓰지 않고 추출 가능한 두 값만 별도로 좁힌다.
export type ExportStatus = Extract<ReviewStatus, "confirmed" | "rejected">;

export type Role = "admin" | "worker";

export interface CandidateInfo {
  value: string;
  email_status: string | null;
  email_mx: boolean | null;
  email_smtp: boolean | null;
}

export interface ReviewItem {
  id: string;
  company_id: string;
  field: string;
  candidates: CandidateInfo[];
  selected: string | null;
  status: ReviewStatus;
  assignee: string | null;
  reviewed_at: string | null;
  name: string;
  country: string;
  industry: string;
  homepage: string | null;
  site_alive: boolean;
  form: string | null;
  note: string | null; // 검수자 기타 메모(문의폼 미발송 사유 등) — 엑셀 L(기타) 컬럼.
  // 첨부파일 유무(BE #381) — null=미확인. 엑셀 12컬럼에 자리가 없어 export 미반영(화면·DB 전용).
  has_attachment: boolean | null;
  // 상대 회사 담당자명(BE #381, 최대 64자) — 엑셀 H(담당자) 컬럼에 기입된다.
  manager: string | null;
  email_status: string | null;
  email_mx: boolean | null;
  email_smtp: boolean | null;
  // 대표 전화번호(크롤 tel:·본문 서식 또는 등록처 폴백) — 엑셀 C(연락처) 컬럼과 같은 값.
  // BE #429 로 큐 API 3개 경로(GET /queue·/queue/mine·POST /queue/claim) 모두 내려온다.
  // 아직 안 주는 구버전 서버가 있어 옵셔널 유지 — 표시부에서 "" 로 강등한다.
  // BE #432 부터 검수자가 확정 시 교정할 수 있고(ConfirmEdits.phone), 확정 응답엔 교정된
  // 값이 담겨 돌아온다.
  phone?: string | null;
  // 상장여부 — BE 계약 확장 필요(GET /queue·/queue/mine·POST /queue/claim 응답에 추가).
  listed: Listed;
  // 상장 시장 보드(KOSPI/KOSDAQ/KONEX/NASDAQ…, 미상=null) — BE #136 큐 API 노출.
  market: string | null;
}

// 확정(POST /queue/{id}/confirm)에 실을 사람 교정분 — 필드 생략/undefined = 변경 없음.
// 인자 수가 늘어 위치인자(8개)로는 호출부에서 순서를 틀리기 쉬워 한 벌로 묶어 넘긴다.
export interface ConfirmEdits {
  selected?: string; // 사람이 고른/직접 입력한 최종 이메일
  homepage?: string; // 교정한 사이트 URL(정규화 통과분, 원본과 다를 때만)
  hasForm?: boolean; // 교정한 문의폼 유무(감지값과 다를 때만)
  note?: string; // 기타 메모 — 빈 문자열 = 메모 삭제
  removeEmails?: string[]; // 실존하지 않아 지울 이메일(후보+연락처)
  // 첨부파일 유무(#382) — 유/무만 전송 가능. 계약상 null=변경 없음이라 '미확인'으로
  // 되돌리는 요청은 표현할 수 없다(호출부가 미반영을 사용자에게 알린다).
  hasAttachment?: boolean;
  manager?: string; // 담당자명(#382, 최대 64자) — 빈 문자열 = 지움
  // 대표 전화 교정(#432, 최대 64자) — 빈 문자열 = 지움. 크롤/등록처 값을 사람 입력으로
  // 대체한다(엑셀 C 컬럼). 숫자가 하나도 없는 값은 BE 가 422 라 normPhone 이 먼저 거른다.
  phone?: string;
}

export interface QueueResponse {
  items: ReviewItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface LoginResponse {
  token: string;
  username: string;
  role: Role;
}

export interface UserStats {
  id: string;
  username: string;
  role: Role;
  is_active: boolean;
  created_at: string | null;
  confirmed: number;
  rejected: number;
  claimed: number; // 현재 점유 중인 pending 건수 — 관리자 회수 판단용
  last_action_at: string | null;
}

export interface ReviewDailyStatsItem {
  username: string;
  confirmed: number;
  rejected: number;
}

// GET /admin/stats/review-daily 응답 — 직원별 하루(KST) 확정/거부 건수(reclaim 등 제외).
export interface ReviewDailyStats {
  date: string; // YYYY-MM-DD(KST)
  items: ReviewDailyStatsItem[];
}

export interface AuditEntry {
  id: string;
  review_id: string;
  actor_username: string;
  action: string;
  selected: string | null;
  company_name: string;
  at: string | null;
}

export interface CountryOption {
  iso2: string;
  label: string;
  aliases: string[];
}

export interface IndustryOption {
  value: string;
  label: string;
  aliases: string[];
}

export interface SendPreview {
  recipients: number;
  enabled: boolean;
  daily_cap: number;
  remaining_today: number;
  sender: string;
  sample: string[];
}

// 발송 결과 요약. 카운터는 서로 배타라 recipients = sent + failed + uncertain + skipped + capped
// 가 성립한다 — 일부만 그리면 합이 안 맞아 보이므로 0 초과인 항목은 전부 표시한다.
export interface SendResult {
  dry_run: boolean;
  recipients: number;
  attempted: number;
  sent: number;
  failed: number;
  // 결과 불명(BE #422) — SMTP DATA 를 다 보낸 뒤 250 응답을 못 읽은 건. 전달됐을 수 있어
  // BE 가 자동 재발송에서 **영구 제외**하고 운영자 수동 확인에 맡긴다. 화면에 안 띄우면
  // 확인할 건이 있다는 사실 자체가 전달되지 않으므로 경고 톤으로 노출한다.
  uncertain: number;
  // 동시 캠페인 선점/기발송 스킵(BE #267) — 실패가 아니라 중복 방지로 건너뛴 건.
  skipped: number;
  capped: number;
}

export type Listed = "unknown" | "listed" | "unlisted";

// 검증 큐 당겨가기 세션 필터 — 빈값=전체. listed 는 ""(전체)+Listed 3값.
// (계약: POST /queue/claim 본문 · GET /queue 쿼리파라미터, PRD-queue-filtered-claim §4)
export interface ClaimFilter {
  country: string; // 쉼표구분 ISO2/별칭, 빈값=전체
  industry: string; // 쉼표구분 업종, 빈값=전체
  listed: "" | Listed; // 빈값=전체
  // 쉼표구분 시장 보드(KOSPI/KOSDAQ…), 빈값=전체 — 전체 큐(GET /queue) 조회 전용.
  // BE 계약 확장 대기: 파라미터 추가 전까지 실서버에선 무시된다(PR 본문 계약 명세 참조).
  market?: string;
  // 쉼표구분 지역(시/도, KR 전용), 빈값=전체. country 에 KR 이 있을 때만 의미가 있다
  // (BE ClaimRequest.region·GET /queue region 파라미터, #139 region 컬럼 활용).
  region: string;
}

// 검증 직원용 필터 옵션(국가+업종 한 번에) — GET /queue/filters (worker 접근 가능).
// listed 는 고정 3값(전체("")는 FE 가 덧붙임) — 셀렉트는 FE 하드코딩이라 소비 안 함.
export interface QueueFilters {
  countries: CountryOption[];
  industries: IndustryOption[];
  listed: string[];
  // 시장 보드 어휘(DB distinct) — BE 계약 확장 대기. 없으면 FE 폴백 목록 사용.
  markets?: string[];
  // 지역 어휘(KR 시/도, DB distinct·실측값만) — BE 이미 배포됨(GET /queue/filters regions).
  regions?: string[];
}

export interface CrawlTarget {
  countries: string;
  industries: string;
  listed: Listed;
  persist: boolean;
  updated_by: string | null;
  updated_at: string | null;
}

// 크롤 작업(CrawlJob/CrawlJobStatus) 타입은 2026-09-01 제거됐다(#448, BE #450) — 웹 즉시크롤이
// 사라지고 세그먼트 작업(SegmentJobInfo)으로 일원화. CrawlTarget 은 일일 스케줄러가 계속 쓴다.

// --- 백필 제어(#352) ----------------------------------------------------
// 지속형 consumer 라 '완료(done)'가 없다 — 대상을 다 소진해도 대기 상태로 남는다.
// budget_exhausted 는 월 예산 소진에 의한 **정상 종료**(실패 아님 — 표시 톤 구분 필요).
export type BackfillJobStatus =
  | "idle"
  | "running"
  | "failed"
  | "cancelled"
  | "budget_exhausted";

// 트랙 1개분 현황. track("C"=도메인해석, "A"=이메일)은 내부 개념이라 화면엔 노출하지
// 않고, 응답을 구분해 받기 위해서만 존재한다(딸깍 원칙 — 조건 하나·버튼 하나).
export interface BackfillJob {
  id: string | null;
  track: string;
  status: BackfillJobStatus;
  countries: string;
  // 업종 조건은 포함식·제외식 배타(#372) — 한쪽이 채워지면 다른 쪽은 항상 빈 문자열.
  industries: string;
  exclude_industries: string;
  exclude_listed: boolean;
  batch: number;
  workers: number;
  max_batches: number;
  min_queue: number;
  // initial_target 은 시작 시점 스냅샷(고정), remaining 은 실시간 잔여(신규 유입으로 늘 수 있음).
  initial_target: number;
  remaining: number;
  processed: number;
  resolved: number;
  promoted: number;
  emails: number;
  batches_done: number;
  generation: number;
  recycles: number;
  crash_restarts: number;
  pid: number | null;
  cancel_requested: boolean;
  // operator | monthly_budget | cancelled_before_resume
  stop_reason: string | null;
  error: string | null;
  triggered_by: string | null;
  started_at: string | null;
  updated_at: string | null;
  progress_at: string | null;
  finished_at: string | null;
}

export interface BackfillStatus {
  resolve: BackfillJob; // 도메인 해석→승격
  fill: BackfillJob; // 이메일 채우기
}

// 시작 전 잔여 미리보기. 대형 조인이라 폴링 금지(진행 중 잔여는 status.remaining).
// queue_pending 은 국가 조건만 반영한다(BE 주석 — 업종 조건은 포함·제외 어느 쪽도 미반영).
export interface BackfillOverview {
  resolve_pending: number;
  fill_pending: number;
  queue_pending: number;
}

// --- 세그먼트 작업 큐(#403 · BE v1.14.0) --------------------------------
// 관리자가 지정한 세그먼트(국가·업종·상장·지역)를 발견→승격(실존검증·이메일·리뷰큐 적재)까지
// 백그라운드로 완주시키는 트랙 S 작업. **한 번에 1건만 running**, 나머지는 우선순위 큐로 대기.

// 백필(지속형)과 달리 대상이 유한해 '완료(done)'가 있고, 대기(queued)·일시중지(paused)가 있다.
export type SegmentJobStatus =
  | "queued"
  | "running"
  | "paused"
  | "done"
  | "cancelled"
  | "failed"
  | "budget_exhausted";

// running 중 세부 단계 — ""(시작 전) → discover → promote → done.
// discover 는 대상 총량을 모르는 구간이라 **진행률이 없다**(발견 건수만 표시).
export type SegmentJobStage = "" | "discover" | "promote" | "done";

// = BackfillJob 전 필드 + 트랙 S 전용(listed·regions·priority·stage·discovered·failed_items·
// promote_cursor·queue_position). 목록·상세가 같은 스키마라 상세 조회 없이 목록만으로 그린다.
export interface SegmentJobInfo {
  id: string;
  track: string; // "S" 고정 — 내부 개념이라 화면엔 노출하지 않는다.
  status: SegmentJobStatus;
  countries: string;
  industries: string;
  listed: Listed;
  regions: string; // "" | "all" | "서울,경기" — countries 에 KR 포함 시에만 유효(아니면 422)
  priority: number; // 0~1000, **낮을수록 먼저**
  stage: SegmentJobStage;
  discovered: number;
  // 시작 시점 스냅샷이지만 discover 단계에선 **0 = 아직 모름**(백필의 '대상 없음'과 다르다).
  initial_target: number;
  remaining: number;
  processed: number;
  promoted: number;
  emails: number;
  failed_items: number;
  batches_done: number;
  promote_cursor: string | null;
  // queued 일 때만 값, 그 외 null. **예약 회차(not_before 미래)도 null** — BE 가 순번
  // 계산에서 예약행을 빼기 때문(is_ready 필터). 즉 순번과 예약 시각은 동시에 뜨지 않는다.
  queue_position: number | null;
  // 반복 간격(분, #452) — 0=1회성. >0 이면 done 으로 끝날 때 같은 필터·우선순위로 다음
  // 회차가 자동 적재된다(취소·실패·예산소진이면 반복 종료). 상한 10080(7일).
  repeat_every_min: number;
  // 반복 복제분의 실행 가능 시각(ISO, offset 포함 — BE 가 naive 값도 UTC 로 붙여 보낸다).
  // 미래면 queued 로 대기하며 BE 큐 티커(60초)가 시각 도래분을 집어 간다. 1회성은 null.
  not_before: string | null;
  generation: number;
  recycles: number;
  crash_restarts: number;
  pid: number | null;
  // running 에 대한 pause/cancel 은 즉시 반영되지 않는다 — 이 플래그로 200 을 받고 수 초 내 전이.
  cancel_requested: boolean;
  // operator | monthly_budget | cancelled_before_resume | pause.
  // **pause 만 종료 사유가 아니다**(BE #398) — 일시중지 요청 시 running 에 먼저 심기고 paused
  // 로 전이한 뒤에도 남는다. cancel_requested 하나로 취소·일시중지가 겸용이라 둘을 구분하는
  // 유일한 단서이기도 하다. 재개(requeue)하면 null 로 지워진다.
  stop_reason: string | null;
  error: string | null;
  triggered_by: string | null;
  started_at: string | null; // 트랙 S 에선 **요청 생성 시각**(실행 시작이 아니다)
  updated_at: string | null;
  progress_at: string | null;
  finished_at: string | null;
  // 백필과 공유하는 실행 파라미터 — 트랙 S 폼에는 없어 화면 표시도 하지 않는다.
  exclude_industries: string;
  exclude_listed: boolean;
  batch: number;
  workers: number;
  max_batches: number;
  min_queue: number;
  resolved: number;
}

// 정렬은 BE 고정 — running → queued(priority, 요청시각) → 나머지 최신순.
export interface SegmentJobList {
  items: SegmentJobInfo[];
  total: number;
}

// 제출 전 규모 확인. 원장 COUNT 라 **폴링 금지**(입력 확정 시 1회).
// segments > max_segments 면 생성이 422 로 거부되므로 사전 경고에 쓴다.
export interface SegmentJobPreview {
  segments: number;
  promote_pending: number;
  max_segments: number;
}

// POST /admin/segment-jobs 본문 — countries·industries 는 CSV 필수(빈값 422).
export interface SegmentJobRequest {
  countries: string;
  industries: string;
  listed: Listed;
  regions: string;
  priority: number;
  // 반복 간격(분, #452) — 0=1회성(기본), 0~10080. 범위를 벗어나면 BE 422.
  repeat_every_min: number;
}

// --- 보유 데이터 대시보드(#378) ----------------------------------------
// GET /dashboard/summary — 원장·회사·큐 한 응답 스냅샷(진입 1회 + 수동 새로고침, 폴링 금지).

// 발견 원장 4분면 — total = promoted + domained_unpromoted + undomained_unpromoted + absorbed.
export interface LedgerSummary {
  total: number;
  promoted: number; // company 로 승격 완료(실존 확인분)
  domained_unpromoted: number; // 도메인 확정·미승격(승격 백필 대상)
  undomained_unpromoted: number; // 도메인 미확정(해석 대기)
  absorbed: number; // dedup 흡수분 — 승격 대상이 아니라 백로그에 섞으면 안 된다
}

// country: 등록국=ISO2, 미등록=원문, ''=국가 미상(큐 필터와 동일 어휘).
export interface CountryCount {
  country: string;
  n: number;
}

// industry: 구분 라벨(빈 업종은 '미분류'로 접힘).
export interface IndustryCount {
  industry: string;
  n: number;
}

// 승격(실존) 회사 현황. total 이 ledger.promoted 를 넘을 수 있다(원장 없는 고아 company —
// 정상 현상). 두 수를 등치로 그리지 말 것(BE 계약 주석).
export interface CompaniesSummary {
  total: number;
  with_email: number; // 이메일 연락처 1개 이상 보유 회사 수(distinct)
  by_country: CountryCount[]; // n 내림차순
  by_industry: IndustryCount[]; // n 내림차순
}

// 검증 큐 상태별 카운트 — pending 은 점유 여부로 분해.
export interface QueueSummary {
  pending_unclaimed: number;
  pending_claimed: number;
  confirmed: number;
  rejected: number;
}

export interface DashboardSummary {
  ledger: LedgerSummary;
  companies: CompaniesSummary;
  queue: QueueSummary;
}

// --- 관리자 회사 DB 검색(BE PR#418) ------------------------------------
// GET /admin/companies — 큐 상태와 **무관하게** company 전체를 회사명·홈페이지·이메일/문의폼
// URL·발견 원장의 보관 상호(name_eng)로 부분일치(대소문자 무시) 조회한다. 중복 확인·수동
// 조회용. name_eng 의 표기는 소스마다 다르다(DART·NPS=국내 영문명, EDINET=일문 원문 — BE
// #412 로 name 이 영문 상호가 되면서 자리를 맞바꿨다). jp_assoc(BE #443)도 #445 이후 IMAJ 는
// 같은 슬롯을 쓰지만, JSDA·IMAJ 미매칭분은 표시명이 일문이고 name_eng 가 없다. fsa_jp
// (BE #449)는 gBizINFO 토큰 유무로 갈린다 — 있으면 EDINET 슬롯, 없으면 후자(토큰 발급 전이라
// 당분간 유입분 전량). 일본 기업은 일문이 두 슬롯 중 하나엔 있지만 영문은 없을 수 있다.

// role 은 EmailRole 어휘(ir/general/hr/press/personal/unknown), status 는 이메일 검증
// 상태(valid/risky/invalid/unknown) — **미검증이면 null**이라 큐의 email_status 와 같은 표기를 쓴다.
export interface CompanyEmailInfo {
  value: string;
  role: string;
  status: string | null;
}

export interface CompanySearchItem {
  id: string;
  canonical_key: string; // 재추출 금지 판정 키(제약 ①) — 중복 확인의 실제 근거
  name: string;
  country: string;
  industry: string;
  homepage: string | null;
  is_active: boolean;
  site_alive: boolean;
  listed: Listed;
  market: string | null;
  // 검색 대상은 email·form 만(주소 제외 — BE 계약). 전화는 검색어 매칭 대상은 아니고
  // 표시용으로만 내려온다 — **BE 계약 확장 필요**(GET /admin/companies 응답에 phone 추가).
  // 미배포 서버에선 undefined 가 오므로 옵셔널(ReviewItem.phone 과 동일 규약).
  emails: CompanyEmailInfo[];
  form: string | null;
  phone?: string | null;
  // 검증 큐(field="email") 적재분 — **미적재면 3필드 모두 null**.
  review_id: string | null;
  review_status: ReviewStatus | null;
  review_assignee: string | null;
}

export interface CompanySearchResponse {
  items: CompanySearchItem[];
  total: number;
  limit: number;
  offset: number;
}
