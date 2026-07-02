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
  email_status: string | null;
  email_mx: boolean | null;
  email_smtp: boolean | null;
  // 상장여부 — BE 계약 확장 필요(GET /queue·/queue/mine·POST /queue/claim 응답에 추가).
  listed: Listed;
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
}

// 검증 직원용 필터 옵션(국가+업종 한 번에) — GET /queue/filters (worker 접근 가능).
// listed 는 고정 3값(전체("")는 FE 가 덧붙임) — 셀렉트는 FE 하드코딩이라 소비 안 함.
export interface QueueFilters {
  countries: CountryOption[];
  industries: IndustryOption[];
  listed: string[];
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
  error: string | null;
  cancel_requested: boolean;
  triggered_by: string | null;
  started_at: string | null;
  updated_at: string | null;
  finished_at: string | null;
}
