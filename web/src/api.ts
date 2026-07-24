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
  ReviewDailyStats,
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
      const d = (await res.json()).detail;
      if (Array.isArray(d)) {
        // FastAPI 422 검증 오류 — detail 이 [{loc, msg}] 배열이라 그대로 두면 [object Object]로 보인다.
        detail =
          d
            .map((e: { loc?: unknown[]; msg?: string }) =>
              [e.loc?.slice(1).join("."), e.msg].filter(Boolean).join(": "),
            )
            .join("; ") || detail;
      } else if (d != null) {
        detail = typeof d === "string" ? d : JSON.stringify(d);
      }
    } catch {
      // 본문이 JSON 이 아니면 상태코드만 노출.
    }
    throw new Error(`요청 실패: ${detail}`);
  }
  return res.json() as Promise<T>;
}

// 공통 GET / POST·PUT 래퍼 — 인증 헤더 동반 + 401/오류 공통 처리 + JSON 파싱.
// body 를 주면 JSON 으로 직렬화해 보낸다(없으면 Content-Type 도 안 붙임 — 기존 동작 유지).
async function apiGet<T>(path: string): Promise<T> {
  return jsonOrThrow<T>(await fetch(`${BASE}${path}`, { headers: authHeaders() }));
}

async function apiSend<T>(method: "POST" | "PUT", path: string, body?: unknown): Promise<T> {
  return jsonOrThrow<T>(
    await fetch(`${BASE}${path}`, {
      method,
      headers:
        body === undefined ? authHeaders() : authHeaders({ "Content-Type": "application/json" }),
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
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
  if (params.filter?.market) q.set("market", params.filter.market);
  if (params.filter?.region) q.set("region", params.filter.region);
  return apiGet(`/queue?${q.toString()}`);
}

// 작업 받기 — 호출 1회 = +30개 추가 배정(선취, 총량 100 상한). 응답은 필터와 무관하게 내 점유
// 전체. 추가형이라 새로고침·복원 용도로 쓰면 안 됨(그 용도는 fetchMyWork) — "작업 받기" 버튼
// 클릭 시에만 호출한다. filter(국가·업종·상장·지역)는 신규 배정분에만 적용(빈값=전체).
export async function claimWork(filter?: ClaimFilter): Promise<ReviewItem[]> {
  return apiSend("POST", "/queue/claim", {
    country: filter?.country ?? "",
    industry: filter?.industry ?? "",
    listed: filter?.listed ?? "",
    region: filter?.region ?? "",
  });
}

// 검증 직원용 필터 옵션(국가+업종) — /admin/* 는 worker 가 403 이므로 비관리자 경로로 받는다.
export async function fetchQueueFilters(): Promise<QueueFilters> {
  return apiGet("/queue/filters");
}

// 내 작업분 조회(부작용 없음) — 페이지 로드·새로고침·재로그인 복원·처리 후 목록 갱신용.
// status=confirmed|rejected 면 내 처리 내역(최신 처리 먼저) — BE 계약 확장 제안분.
// 구서버는 파라미터를 무시하고 pending 점유분을 주므로 호출측이 status 로 한 번 더 거른다.
export async function fetchMyWork(status?: ReviewStatus): Promise<ReviewItem[]> {
  return apiGet(`/queue/mine${status ? `?status=${status}` : ""}`);
}

// 담당자는 서버가 로그인 사용자로 자동 기록. selected = 사람이 고른 최종 이메일 후보.
// homepage = 사람이 수정한 사이트 URL(null=변경 없음) — BE 계약 확장 제안분. hasForm = 사람이
// 교정한 문의폼 유무(undefined=변경 없음) — 마찬가지로 BE 계약 확장 제안분. Pydantic 기본이
// 추가 필드 무시라 미배포 서버에도 안전하다(수정만 반영 안 됨). PR 본문 계약 명세 참조.
// removeEmails = 실존하지 않아 삭제할 이메일(후보+연락처에서 제거, 최대 50건·각 320자 — BE
// PR#314). 다 지우고 문의폼이 있으면 엑셀 J 가 "사이트 내 문의폼"이 된다. 지운 주소를
// selected 로 함께 보내면 BE 가 400(모순) — 호출측이 selected 에서 제외해 보낸다.
export async function confirmReview(
  id: string,
  selected?: string,
  homepage?: string,
  hasForm?: boolean,
  note?: string,
  removeEmails?: string[],
): Promise<ReviewItem> {
  return apiSend("POST", `/queue/${id}/confirm`, {
    selected: selected ?? null,
    homepage: homepage ?? null,
    has_form: hasForm ?? null,
    note: note ?? null,
    remove_emails: removeEmails && removeEmails.length ? removeEmails : null,
  });
}

export async function rejectReview(id: string): Promise<ReviewItem> {
  return apiSend("POST", `/queue/${id}/reject`);
}

// --- 관리자 API(role==admin 만 200, 아니면 403) -----------------------

export async function fetchUsers(): Promise<UserStats[]> {
  return apiGet("/admin/users");
}

export async function createUser(
  username: string,
  password: string,
  role: Role,
): Promise<UserStats> {
  return apiSend("POST", "/admin/users", { username, password, role });
}

export async function changeUserRole(id: string, role: Role): Promise<UserStats> {
  return apiSend("POST", `/admin/users/${id}/role`, { role });
}

export async function setUserActive(id: string, active: boolean): Promise<UserStats> {
  return apiSend("POST", `/admin/users/${id}/active?active=${active}`);
}

// 계정의 pending 점유 전부를 풀로 회수한다(영구 배정의 유일한 해제 경로 — 관리자 전용).
export async function reclaimUser(id: string): Promise<{ reclaimed: number }> {
  return apiSend("POST", `/admin/users/${id}/reclaim`);
}

// BE 상한(le=500)까지 한 번에 받아 FE 에서 필터·페이지네이션한다.
// ponytail: BE 에 필터 파라미터가 없어 클라이언트 처리 — 이력이 500건을 넘어 잘리면 BE 계약 확장(offset·필터) 제안.
export async function fetchAudit(limit = 500): Promise<AuditEntry[]> {
  return apiGet(`/admin/audit?limit=${limit}`);
}

// 직원별 하루 처리량(확정/거부) — date 생략 시 BE 기본(오늘 KST).
export async function fetchReviewDaily(date?: string): Promise<ReviewDailyStats> {
  return apiGet(`/admin/stats/review-daily${date ? `?date=${date}` : ""}`);
}

export async function fetchCountries(): Promise<CountryOption[]> {
  return apiGet("/admin/countries");
}

export async function fetchIndustries(): Promise<IndustryOption[]> {
  return apiGet("/admin/industries");
}

// 업종 '미분류' 필터 옵션 — BE 분류 폴백 저장값(sources/taxonomy.py UNCLASSIFIED)과 동일 토큰.
// 옵션 API(supported_industries)엔 없지만 DB industry 컬럼에 실존해 필터가 그대로 매칭된다.
// 조회 필터(전체큐·작업받기·발송·추출)에만 붙인다 — 크롤 타깃 설정(실업종 지정)엔 무의미.
export const UNCLASSIFIED_INDUSTRY_OPTION: IndustryOption = {
  value: "미분류",
  label: "미분류",
  aliases: ["unclassified"],
};

// '미분류' 옵션을 목록 끝에 보장 — BE 가 이미 내려주면(#115 이후 /queue/filters) 그대로 두어
// 중복 옵션을 막고, 안 주는 목록(/admin/industries)엔 덧붙인다.
export function withUnclassified(industries: IndustryOption[]): IndustryOption[] {
  return industries.some((i) => i.value === UNCLASSIFIED_INDUSTRY_OPTION.value)
    ? industries
    : [...industries, UNCLASSIFIED_INDUSTRY_OPTION];
}

export async function fetchSendPreview(country = "", industry = ""): Promise<SendPreview> {
  const q = new URLSearchParams();
  if (country) q.set("country", country);
  if (industry) q.set("industry", industry);
  const qs = q.toString();
  return apiGet(`/send/preview${qs ? `?${qs}` : ""}`);
}

export async function sendCampaign(payload: {
  subject: string;
  body: string;
  from_display: string;
  country: string;
  industry: string;
}): Promise<SendResult> {
  return apiSend("POST", "/send", payload);
}

export async function fetchCrawlTarget(): Promise<CrawlTarget> {
  return apiGet("/admin/crawl-target");
}

export async function saveCrawlTarget(t: {
  countries: string;
  industries: string;
  listed: Listed;
  persist: boolean;
}): Promise<CrawlTarget> {
  return apiSend("PUT", "/admin/crawl-target", t);
}

// --- 직접 크롤(웹에서 즉시 실행 + 진행현황 폴링 + 중지) ----------------

// 폼 입력값으로 즉시 크롤을 시작한다(백그라운드). 이미 진행 중이면 409.
// continuous=true 면 취소(중지)까지 라운드를 반복하는 연속 크롤(#132).
// regions(쉼표구분, #139)는 KR 세그먼트를 지역별 검색으로 팬아웃 — 빈값=팬아웃 없음(기본).
export async function startCrawl(t: {
  countries: string;
  industries: string;
  listed: Listed;
  persist: boolean;
  continuous: boolean;
  regions: string;
}): Promise<CrawlJob> {
  return apiSend("POST", "/admin/crawl", t);
}

// 최근 크롤 작업 현황(없으면 status="idle"). 진행 중에는 주기 폴링으로 호출한다.
export async function fetchCrawlStatus(): Promise<CrawlJob> {
  return apiGet("/admin/crawl");
}

// 최근 크롤 이력(최신순). BE 계약 제안분 — 미배포 서버에선 404 이므로 호출측에서 조용히 숨긴다.
export async function fetchCrawlHistory(limit = 10): Promise<CrawlJob[]> {
  return apiGet(`/admin/crawl/history?limit=${limit}`);
}

// 진행 중 크롤에 취소를 요청한다(협조적 중단). 진행 중이 없으면 404.
export async function cancelCrawl(): Promise<CrawlJob> {
  return apiSend("POST", "/admin/crawl/cancel");
}

// 확정분 엑셀 다운로드. 인증 헤더가 필요해 평범한 링크 대신 fetch→blob 으로 받아 저장한다.
// country/industry(쉼표구분)로 국가·업종별 선택 추출(빈값=전체).
// dateFrom/dateTo(YYYY-MM-DD)=확정 처리일(KST, 포함) 필터, 빈값=전체(#308).
export async function exportConfirmed(
  country = "",
  industry = "",
  dateFrom = "",
  dateTo = "",
): Promise<void> {
  const q = new URLSearchParams();
  if (country) q.set("country", country);
  if (industry) q.set("industry", industry);
  if (dateFrom) q.set("date_from", dateFrom);
  if (dateTo) q.set("date_to", dateTo);
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
