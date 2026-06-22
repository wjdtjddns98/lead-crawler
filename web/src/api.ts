// 백엔드 API 클라이언트. 개발 시 Vite 프록시로 상대경로 호출(같은 출처), 운영 빌드는
// VITE_API_BASE 로 절대경로를 주입할 수 있다.
import type { QueueResponse, ReviewItem, ReviewStatus } from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "";

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      // 본문이 JSON 이 아니면 상태코드만 노출.
    }
    throw new Error(`요청 실패: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export async function fetchQueue(params: {
  status?: ReviewStatus | "";
  limit: number;
  offset: number;
}): Promise<QueueResponse> {
  const q = new URLSearchParams();
  if (params.status) q.set("status", params.status);
  q.set("limit", String(params.limit));
  q.set("offset", String(params.offset));
  return jsonOrThrow<QueueResponse>(await fetch(`${BASE}/queue?${q.toString()}`));
}

export async function confirmReview(id: string, assignee?: string): Promise<ReviewItem> {
  return jsonOrThrow<ReviewItem>(
    await fetch(`${BASE}/queue/${id}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ assignee: assignee ?? null }),
    }),
  );
}

export async function rejectReview(id: string, assignee?: string): Promise<ReviewItem> {
  return jsonOrThrow<ReviewItem>(
    await fetch(`${BASE}/queue/${id}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ assignee: assignee ?? null }),
    }),
  );
}

// 확정분 엑셀 다운로드 URL(브라우저가 직접 받게 link 로 연다).
export function exportUrl(): string {
  return `${BASE}/export`;
}
