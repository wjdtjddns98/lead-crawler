// 백엔드 API DTO 와 1:1 대응하는 타입(leadcrawler/api/schemas.py 참조).

export type ReviewStatus = "pending" | "confirmed" | "rejected";

export interface ReviewItem {
  id: string;
  company_id: string;
  field: string;
  candidates: string[];
  status: ReviewStatus;
  assignee: string | null;
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
