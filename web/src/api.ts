// 백엔드 API 클라이언트. 개발 시 Vite 프록시로 상대경로 호출(같은 출처), 운영 빌드는
// VITE_API_BASE 로 절대경로를 주입할 수 있다. 인증: 로그인 토큰을 localStorage 에 보관하고
// 모든 보호 요청에 Authorization: Bearer 헤더로 동반한다. 401 이면 세션을 비우고 콜백 통지.
import type {
  AuditEntry,
  ClaimFilter,
  CountryOption,
  CrawlJob,
  CrawlTarget,
  IndustryOption,
  Listed,
  LoginResponse,
  QueueFilters,
  QueueResponse,
  ReviewItem,
  ReviewStatus,
  Role,
  SendPreview,
  SendResult,
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
  if (res.status === 429) {
    // 무차별대입 스로틀 — 백엔드 detail(잠금 안내) + Retry-After(잠금 잔여 초)를 전달한다.
    let detail = "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.";
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      // 본문이 JSON 이 아니면 기본 안내 문구.
    }
    const err = new Error(detail) as Error & { retryAfter?: number };
    err.retryAfter = Number(res.headers.get("Retry-After")) || 0;
    throw err;
  }
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
  filter?: ClaimFilter; // 빈값=전체. total 도 이 필터 반영분으로 내려온다(잔여건수용).
}): Promise<QueueResponse> {
  const q = new URLSearchParams();
  if (params.status) q.set("status", params.status);
  q.set("limit", String(params.limit));
  q.set("offset", String(params.offset));
  if (params.filter?.country) q.set("country", params.filter.country);
  if (params.filter?.industry) q.set("industry", params.filter.industry);
  if (params.filter?.listed) q.set("listed", params.filter.listed);
  return jsonOrThrow<QueueResponse>(
    await fetch(`${BASE}/queue?${q.toString()}`, { headers: authHeaders() }),
  );
}

// 작업 받기 — 호출 1회 = +30개 추가 배정(선취, 총량 100 상한). 응답은 필터와 무관하게 내 점유
// 전체. 추가형이라 새로고침·복원 용도로 쓰면 안 됨(그 용도는 fetchMyWork) — "작업 받기" 버튼
// 클릭 시에만 호출한다. filter(국가·업종·상장)는 신규 배정분에만 적용(빈값=전체).
export async function claimWork(filter?: ClaimFilter): Promise<ReviewItem[]> {
  return jsonOrThrow<ReviewItem[]>(
    await fetch(`${BASE}/queue/claim`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        country: filter?.country ?? "",
        industry: filter?.industry ?? "",
        listed: filter?.listed ?? "",
      }),
    }),
  );
}

// 검증 직원용 필터 옵션(국가+업종) — /admin/* 는 worker 가 403 이므로 비관리자 경로로 받는다.
export async function fetchQueueFilters(): Promise<QueueFilters> {
  return jsonOrThrow<QueueFilters>(
    await fetch(`${BASE}/queue/filters`, { headers: authHeaders() }),
  );
}

// 내 작업분 조회(부작용 없음) — 페이지 로드·새로고침·재로그인 복원·처리 후 목록 갱신용.
export async function fetchMyWork(): Promise<ReviewItem[]> {
  return jsonOrThrow<ReviewItem[]>(
    await fetch(`${BASE}/queue/mine`, { headers: authHeaders() }),
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

// 계정의 pending 점유 전부를 풀로 회수한다(영구 배정의 유일한 해제 경로 — 관리자 전용).
export async function reclaimUser(id: string): Promise<{ reclaimed: number }> {
  return jsonOrThrow<{ reclaimed: number }>(
    await fetch(`${BASE}/admin/users/${id}/reclaim`, { method: "POST", headers: authHeaders() }),
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

// 업종 '미분류' 필터 옵션 — BE 분류 폴백 저장값(sources/taxonomy.py UNCLASSIFIED)과 동일 토큰.
// 옵션 API(supported_industries)엔 없지만 DB industry 컬럼에 실존해 필터가 그대로 매칭된다.
// 조회 필터(전체큐·작업받기·발송·추출)에만 붙인다 — 크롤 타깃 설정(실업종 지정)엔 무의미.
export const UNCLASSIFIED_INDUSTRY_OPTION: IndustryOption = {
  value: "미분류",
  label: "미분류",
  aliases: ["unclassified"],
};

export async function fetchSendPreview(country = "", industry = ""): Promise<SendPreview> {
  const q = new URLSearchParams();
  if (country) q.set("country", country);
  if (industry) q.set("industry", industry);
  const qs = q.toString();
  return jsonOrThrow<SendPreview>(
    await fetch(`${BASE}/send/preview${qs ? `?${qs}` : ""}`, { headers: authHeaders() }),
  );
}

export async function sendCampaign(payload: {
  subject: string;
  body: string;
  from_display: string;
  country: string;
  industry: string;
}): Promise<SendResult> {
  return jsonOrThrow<SendResult>(
    await fetch(`${BASE}/send`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    }),
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

// --- 직접 크롤(웹에서 즉시 실행 + 진행현황 폴링 + 중지) ----------------

// 폼 입력값으로 즉시 크롤을 시작한다(백그라운드). 이미 진행 중이면 409.
export async function startCrawl(t: {
  countries: string;
  industries: string;
  listed: Listed;
  persist: boolean;
}): Promise<CrawlJob> {
  return jsonOrThrow<CrawlJob>(
    await fetch(`${BASE}/admin/crawl`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(t),
    }),
  );
}

// 최근 크롤 작업 현황(없으면 status="idle"). 진행 중에는 주기 폴링으로 호출한다.
export async function fetchCrawlStatus(): Promise<CrawlJob> {
  return jsonOrThrow<CrawlJob>(
    await fetch(`${BASE}/admin/crawl`, { headers: authHeaders() }),
  );
}

// 진행 중 크롤에 취소를 요청한다(협조적 중단). 진행 중이 없으면 404.
export async function cancelCrawl(): Promise<CrawlJob> {
  return jsonOrThrow<CrawlJob>(
    await fetch(`${BASE}/admin/crawl/cancel`, { method: "POST", headers: authHeaders() }),
  );
}

// 확정분 엑셀 다운로드. 인증 헤더가 필요해 평범한 링크 대신 fetch→blob 으로 받아 저장한다.
// country/industry(쉼표구분)로 국가·업종별 선택 추출(빈값=전체).
export async function exportConfirmed(country = "", industry = ""): Promise<void> {
  const q = new URLSearchParams();
  if (country) q.set("country", country);
  if (industry) q.set("industry", industry);
  const qs = q.toString();
  const res = await fetch(`${BASE}/export${qs ? `?${qs}` : ""}`, { headers: authHeaders() });
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
