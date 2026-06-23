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
  email_status: string | null;
  email_mx: boolean | null;
  email_smtp: boolean | null;
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

export type Listed = "unknown" | "listed" | "unlisted";

export interface CrawlTarget {
  countries: string;
  industries: string;
  listed: Listed;
  persist: boolean;
  updated_by: string | null;
  updated_at: string | null;
}
