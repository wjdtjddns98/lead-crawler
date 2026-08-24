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
  // 첨부파일 유무(BE #381) — null=미확인. 엑셀 12컬럼에 자리가 없어 export 미반영(화면·DB 전용).
  has_attachment: boolean | null;
  // 상대 회사 담당자명(BE #381, 최대 64자) — 엑셀 H(담당자) 컬럼에 기입된다.
  manager: string | null;
  email_status: string | null;
  email_mx: boolean | null;
  email_smtp: boolean | null;
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
