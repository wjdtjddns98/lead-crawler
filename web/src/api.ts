// 백엔드 API 클라이언트. 개발 시 Vite 프록시로 상대경로 호출(같은 출처), 운영 빌드는
// VITE_API_BASE 로 절대경로를 주입할 수 있다. 인증: 로그인 토큰을 localStorage 에 보관하고
// 모든 보호 요청에 Authorization: Bearer 헤더로 동반한다. 401 이면 세션을 비우고 콜백 통지.
import type {
  AuditEntry,
  CountryOption,
  CrawlTarget,
  IndustryOption,
  Listed,
  LoginResponse,
  QueueResponse,
  ReviewItem,
  ReviewStatus,
  Role,
  UserStats,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "";
const TOKEN_KEY = "lc_token";
const USER_KEY = "lc_user";
const ROLE_KEY = "lc_role";

function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function getUser(): string | null {
  return localStorage.getItem(USER_KEY);
}

export function getRole(): Role | null {
  return localStorage.getItem(ROLE_KEY) as Role | null;
}

function setSession(token: string, username: string, role: Role): void {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, username);
  localStorage.setItem(ROLE_KEY, role);
}

function clearSession(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  localStorage.removeItem(ROLE_KEY);
}

// 401(만료/무효 토큰) 발생 시 호출 — App 이 로그인 화면으로 돌리게 등록한다.
let onAuthError: (() => void) | null = null;
export function setAuthErrorHandler(fn: (() => void) | null): void {
  onAuthError = fn;
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = getToken();
  return { ...(extra ?? {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

// 401(만료/무효 토큰) 공통 처리 — 세션 비우고 콜백 통지 후 throw.
function handle401(res: Response): void {
  if (res.status === 401) {
    clearSession();
    onAuthError?.();
    throw new Error("세션이 만료되었습니다. 다시 로그인하세요.");
  }
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  handle401(res);
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

export async function login(username: string, password: string): Promise<LoginResponse> {
  const res = await fetch(`${BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (res.status === 401) throw new Error("아이디 또는 비밀번호가 올바르지 않습니다");
  if (!res.ok) throw new Error(`로그인 실패: ${res.status}`);
  const data = (await res.json()) as LoginResponse;
  setSession(data.token, data.username, data.role);
  return data;
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${BASE}/auth/logout`, { method: "POST", headers: authHeaders() });
  } finally {
    clearSession();
  }
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
  return jsonOrThrow<QueueResponse>(
    await fetch(`${BASE}/queue?${q.toString()}`, { headers: authHeaders() }),
  );
}

// 내 작업분을 배치 크기까지 채워 받는다(당겨가기 — 6명 동시 충돌 방지·자동 리필).
export async function claimWork(): Promise<ReviewItem[]> {
  return jsonOrThrow<ReviewItem[]>(
    await fetch(`${BASE}/queue/claim`, { method: "POST", headers: authHeaders() }),
  );
}

// 내가 점유한 미처리 항목을 풀로 반납한다(작업 종료).
export async function releaseWork(): Promise<{ released: number }> {
  return jsonOrThrow<{ released: number }>(
    await fetch(`${BASE}/queue/release`, { method: "POST", headers: authHeaders() }),
  );
}

// 담당자는 서버가 로그인 사용자로 자동 기록. selected = 사람이 고른 최종 이메일 후보.
export async function confirmReview(id: string, selected?: string): Promise<ReviewItem> {
  return jsonOrThrow<ReviewItem>(
    await fetch(`${BASE}/queue/${id}/confirm`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ selected: selected ?? null }),
    }),
  );
}

export async function rejectReview(id: string): Promise<ReviewItem> {
  return jsonOrThrow<ReviewItem>(
    await fetch(`${BASE}/queue/${id}/reject`, { method: "POST", headers: authHeaders() }),
  );
}

// --- 관리자 API(role==admin 만 200, 아니면 403) -----------------------

export async function fetchUsers(): Promise<UserStats[]> {
  return jsonOrThrow<UserStats[]>(
    await fetch(`${BASE}/admin/users`, { headers: authHeaders() }),
  );
}

export async function createUser(
  username: string,
  password: string,
  role: Role,
): Promise<UserStats> {
  return jsonOrThrow<UserStats>(
    await fetch(`${BASE}/admin/users`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ username, password, role }),
    }),
  );
}

export async function changeUserRole(id: string, role: Role): Promise<UserStats> {
  return jsonOrThrow<UserStats>(
    await fetch(`${BASE}/admin/users/${id}/role`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ role }),
    }),
  );
}

export async function setUserActive(id: string, active: boolean): Promise<UserStats> {
  return jsonOrThrow<UserStats>(
    await fetch(`${BASE}/admin/users/${id}/active?active=${active}`, {
      method: "POST",
      headers: authHeaders(),
    }),
  );
}

export async function fetchAudit(limit = 100): Promise<AuditEntry[]> {
  return jsonOrThrow<AuditEntry[]>(
    await fetch(`${BASE}/admin/audit?limit=${limit}`, { headers: authHeaders() }),
  );
}

export async function fetchCountries(): Promise<CountryOption[]> {
  return jsonOrThrow<CountryOption[]>(
    await fetch(`${BASE}/admin/countries`, { headers: authHeaders() }),
  );
}

export async function fetchIndustries(): Promise<IndustryOption[]> {
  return jsonOrThrow<IndustryOption[]>(
    await fetch(`${BASE}/admin/industries`, { headers: authHeaders() }),
  );
}

export async function fetchCrawlTarget(): Promise<CrawlTarget> {
  return jsonOrThrow<CrawlTarget>(
    await fetch(`${BASE}/admin/crawl-target`, { headers: authHeaders() }),
  );
}

export async function saveCrawlTarget(t: {
  countries: string;
  industries: string;
  listed: Listed;
  persist: boolean;
}): Promise<CrawlTarget> {
  return jsonOrThrow<CrawlTarget>(
    await fetch(`${BASE}/admin/crawl-target`, {
      method: "PUT",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(t),
    }),
  );
}

// 확정분 엑셀 다운로드. 인증 헤더가 필요해 평범한 링크 대신 fetch→blob 으로 받아 저장한다.
export async function exportConfirmed(): Promise<void> {
  const res = await fetch(`${BASE}/export`, { headers: authHeaders() });
  handle401(res);
  if (!res.ok) throw new Error(`엑셀 내보내기 실패: ${res.status}`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "leads_confirmed.xlsx";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
