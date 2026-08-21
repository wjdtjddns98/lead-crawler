// 백엔드 API DTO 와 1:1 대응하는 타입(leadcrawler/api/schemas.py 참조).

export type ReviewStatus = "pending" | "confirmed" | "rejected";

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
  email_status: string | null;
  email_mx: boolean | null;
  email_smtp: boolean | null;
  // 상장여부 — BE 계약 확장 필요(GET /queue·/queue/mine·POST /queue/claim 응답에 추가).
  listed: Listed;
  // 상장 시장 보드(KOSPI/KOSDAQ/KONEX/NASDAQ…, 미상=null) — BE #136 큐 API 노출.
  market: string | null;
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

export interface SendResult {
  dry_run: boolean;
  recipients: number;
  attempted: number;
  sent: number;
  failed: number;
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

export type CrawlJobStatus =
  | "idle"
  | "running"
  | "done"
  | "failed"
  | "cancelled";

export interface CrawlJob {
  id: string | null;
  status: CrawlJobStatus;
  countries: string;
  industries: string;
  listed: Listed;
  persist: boolean;
  segments_total: number;
  segments_done: number;
  discovered: number;
  enriched: number;
  saved: number;
  // 연속(continuous) 모드 — 취소까지 라운드 반복(#132). 카운터는 현재 라운드 기준,
  // rounds_done 은 완료된 라운드 수.
  mode: "once" | "continuous";
  rounds_done: number;
  error: string | null;
  cancel_requested: boolean;
  triggered_by: string | null;
  started_at: string | null;
  updated_at: string | null;
  finished_at: string | null;
}

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
// queue_pending 은 국가 조건만 반영한다(BE 주석 — 업종 필터 체계가 포함식이라 제외식과 불일치).
export interface BackfillOverview {
  resolve_pending: number;
  fill_pending: number;
  queue_pending: number;
}
