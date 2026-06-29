// 백엔드 없이 프론트엔드만 개발하기 위한 메모리 mock. `npm run dev:mock`(vite --mode mock)일 때만
// main.tsx 가 installMock() 을 호출한다. window.fetch 를 가로채 검증 큐 API 를 메모리 상태로 응답하므로
// api.ts·컴포넌트는 전혀 수정하지 않는다. 상태는 메모리 전용 — 새로고침 시 초기 샘플로 리셋된다.
// admin 세션을 localStorage 에 시드해 로그인 화면을 건너뛴다. 매칭 안 되는 API 는 빈/스텁으로 응답.
import type { CandidateInfo, ClaimFilter, Listed, ReviewItem } from "./types";

const BATCH = 10;

// 상장여부는 큐 DTO(ReviewItem)에 없고 BE 가 DiscoveredCompanyRow 조인으로 거른다.
// mock 에선 id→listed 사이드맵으로 그 조인을 흉내 낸다(필터 동작 시연용).
const MOCK_LISTED: Record<string, Listed> = {
  c1: "listed",
  c11: "listed",
  c2: "listed",
  c3: "unlisted",
  c4: "listed",
  c5: "unlisted",
  c6: "listed",
  c7: "unknown",
  c8: "unlisted",
  c9: "listed",
  c10: "unknown",
};

function cand(
  value: string,
  email_status: string | null = "valid",
  email_mx: boolean | null = true,
  email_smtp: boolean | null = true,
): CandidateInfo {
  return { value, email_status, email_mx, email_smtp };
}

function mk(p: Partial<ReviewItem> & { id: string; name: string }): ReviewItem {
  return {
    company_id: p.id,
    field: p.industry ?? "건설",
    candidates: [],
    selected: null,
    status: "pending",
    assignee: null,
    reviewed_at: null,
    country: "KR",
    industry: "건설",
    homepage: null,
    site_alive: true,
    form: null,
    email_status: null,
    email_mx: null,
    email_smtp: null,
    ...p,
  };
}

// 국내 중소·중견 제조사 10개 샘플 — 홈페이지는 모두 실제 접속되는 사이트(팝업으로 열림). 이메일은
// 예시(가짜)이며 실제 주소 아님. 후보 1/다수로 변형을 섞어 라디오 선택·직접입력 UI 를 함께 검증한다.
function seed(): ReviewItem[] {
  return [
    mk({
      id: "c1",
      name: "로보티즈",
      industry: "로봇·로봇부품",
      homepage: "https://www.robotis.com/",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("ir@robotis.com"), cand("contact@robotis.com", "unknown", true, null)],
    }),
    mk({
      id: "c11",
      name: "서울반도체",
      industry: "반도체·LED",
      homepage: "https://www.seoulsemicon.com",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("ir@seoulsemicon.com")],
    }),
    mk({
      id: "c2",
      name: "한미반도체",
      industry: "반도체 장비",
      homepage: "https://www.hanmisemi.com",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("ir@hanmisemi.com"), cand("contact@hanmisemi.com", "unknown", true, null)],
    }),
    mk({
      id: "c3",
      name: "파크시스템스",
      industry: "계측장비",
      homepage: "https://www.parksystems.com",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("sales@parksystems.com")],
    }),
    mk({
      id: "c4",
      name: "심텍",
      industry: "반도체 기판(PCB)",
      homepage: "https://www.simmtech.com",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("ir@simmtech.com")],
    }),
    mk({
      id: "c5",
      name: "동진쎄미켐",
      industry: "반도체·화학소재",
      homepage: "https://www.dongjin.com",
      email_status: "unknown",
      email_mx: true,
      email_smtp: null,
      candidates: [cand("info@dongjin.com", "unknown", true, null)],
    }),
    mk({
      id: "c6",
      name: "솔브레인",
      industry: "반도체 소재",
      homepage: "https://www.soulbrain.co.kr",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("ir@soulbrain.co.kr")],
    }),
    mk({
      id: "c7",
      name: "대주전자재료",
      industry: "전자재료",
      homepage: "https://www.daejoo.co.kr",
      email_status: "unknown",
      email_mx: true,
      email_smtp: null,
      candidates: [
        cand("sales@daejoo.co.kr", "unknown", true, null),
        cand("info@daejoo.co.kr", "invalid", false, false),
      ],
    }),
    mk({
      id: "c8",
      name: "나노신소재",
      industry: "신소재",
      homepage: "https://www.nanonm.com",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("info@nanonm.com")],
    }),
    mk({
      id: "c9",
      name: "에스에프에이",
      industry: "스마트팩토리 장비",
      homepage: "https://www.sfa.co.kr",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("ir@sfa.co.kr")],
    }),
    mk({
      id: "c10",
      name: "인탑스",
      industry: "정밀사출·부품",
      homepage: "https://www.intops.co.kr",
      email_status: "unknown",
      email_mx: true,
      email_smtp: null,
      candidates: [cand("sales@intops.co.kr"), cand("ir@intops.co.kr", "unknown", true, null)],
    }),
  ];
}

let db: ReviewItem[] = seed();

function setStatus(
  id: string,
  status: ReviewItem["status"],
  selected?: string | null,
): ReviewItem | null {
  const it = db.find((x) => x.id === id);
  if (!it) return null;
  it.status = status;
  if (selected !== undefined) it.selected = selected;
  it.assignee = "mock-admin";
  it.reviewed_at = new Date().toISOString();
  return it;
}

function jsonRes(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// 쉼표구분 문자열 → 소문자 토큰 집합(빈값=빈 집합).
function csvSet(s: string | null | undefined): Set<string> {
  return new Set(
    (s ?? "").split(",").map((t) => t.trim().toLowerCase()).filter(Boolean),
  );
}

// 항목이 필터에 맞는지. 빈 조건은 통과.
// ponytail: 국가는 단순 소문자 일치(BE 의 country_match_set 별칭확장은 흉내 안 냄 — mock 시연용).
function matches(it: ReviewItem, f: ClaimFilter): boolean {
  const countries = csvSet(f.country);
  if (countries.size && !countries.has(it.country.toLowerCase())) return false;
  const industries = csvSet(f.industry);
  if (industries.size && !industries.has(it.industry.toLowerCase())) return false;
  if (f.listed && MOCK_LISTED[it.id] !== f.listed) return false;
  return true;
}

function readFilter(u: URL, init?: RequestInit): ClaimFilter {
  // GET /queue 는 쿼리파라미터, POST /queue/claim 은 JSON 본문.
  let body: Partial<ClaimFilter> = {};
  try {
    body = JSON.parse(String(init?.body ?? "{}")) as Partial<ClaimFilter>;
  } catch {
    // 본문 없음 — 쿼리파라미터만 사용.
  }
  return {
    country: u.searchParams.get("country") ?? body.country ?? "",
    industry: u.searchParams.get("industry") ?? body.industry ?? "",
    listed: (u.searchParams.get("listed") as "" | Listed | null) ?? body.listed ?? "",
  };
}

// URL+메서드를 검증 큐 API 로 라우팅. 매칭되면 Response, 아니면 undefined(=실제 fetch 로 통과).
function route(url: string, method: string, init?: RequestInit): Response | undefined {
  const u = new URL(url, location.origin);
  const path = u.pathname;
  const pending = () => db.filter((x) => x.status === "pending");

  // 인증 — mock 은 무조건 admin.
  if (path === "/auth/login" && method === "POST")
    return jsonRes({ token: "mock-token", username: "mock-admin", role: "admin" });
  if (path === "/auth/logout") return jsonRes({});
  if (path === "/health") return jsonRes({ status: "ok" });

  // 검증 큐 필터 옵션(국가+업종) — 시드에 등장하는 실제 업종으로 셀렉트를 채운다.
  if (path === "/queue/filters" && method === "GET") {
    const industries = [...new Set(db.map((x) => x.industry))].map((v) => ({
      value: v,
      label: v,
      aliases: [],
    }));
    return jsonRes({
      countries: [
        { iso2: "KR", label: "대한민국", aliases: ["korea", "한국"] },
        { iso2: "US", label: "미국", aliases: ["usa", "united states"] },
      ],
      industries,
      listed: ["listed", "unlisted", "unknown"],
    });
  }

  // 검증 큐.
  if (path === "/queue" && method === "GET") {
    const status = u.searchParams.get("status");
    const limit = Number(u.searchParams.get("limit") ?? "50");
    const offset = Number(u.searchParams.get("offset") ?? "0");
    const f = readFilter(u, init);
    const filtered = db.filter(
      (x) => (!status || x.status === status) && matches(x, f),
    );
    return jsonRes({
      items: filtered.slice(offset, offset + limit),
      total: filtered.length,
      limit,
      offset,
    });
  }
  if (path === "/queue/claim" && method === "POST") {
    const f = readFilter(u, init);
    return jsonRes(pending().filter((x) => matches(x, f)).slice(0, BATCH));
  }
  if (path === "/queue/release" && method === "POST")
    return jsonRes({ released: pending().length });

  const confirm = path.match(/^\/queue\/([^/]+)\/confirm$/);
  if (confirm && method === "POST") {
    let selected: string | null = null;
    try {
      selected = (JSON.parse(String(init?.body ?? "{}")) as { selected?: string | null }).selected ?? null;
    } catch {
      // 본문 없음/파싱 실패 — 선택 없이 확정.
    }
    const it = setStatus(confirm[1], "confirmed", selected);
    return it ? jsonRes(it) : jsonRes({ detail: "검증 항목을 찾을 수 없습니다" }, 404);
  }
  const reject = path.match(/^\/queue\/([^/]+)\/reject$/);
  if (reject && method === "POST") {
    const it = setStatus(reject[1], "rejected");
    return it ? jsonRes(it) : jsonRes({ detail: "검증 항목을 찾을 수 없습니다" }, 404);
  }

  // admin / send — 범위 밖이라 화면이 안 깨질 만큼만 빈/스텁 응답.
  if (path === "/admin/users") return jsonRes([]);
  if (path === "/admin/audit") return jsonRes([]);
  if (path === "/admin/countries") return jsonRes([]);
  if (path === "/admin/industries") return jsonRes([]);
  if (path === "/admin/crawl-target")
    return jsonRes({
      countries: "",
      industries: "",
      listed: "unknown",
      persist: false,
      updated_by: null,
      updated_at: null,
    });
  if (path === "/admin/crawl")
    return jsonRes({
      id: null,
      status: "idle",
      countries: "",
      industries: "",
      listed: "unknown",
      persist: false,
      segments_total: 0,
      segments_done: 0,
      discovered: 0,
      enriched: 0,
      saved: 0,
      error: null,
      cancel_requested: false,
      triggered_by: null,
      started_at: null,
      updated_at: null,
      finished_at: null,
    });
  if (path === "/send/preview")
    return jsonRes({
      recipients: 0,
      enabled: false,
      daily_cap: 0,
      remaining_today: 0,
      sender: "mock@example.com",
      sample: [],
    });
  if (path === "/send" && method === "POST")
    return jsonRes({ dry_run: true, recipients: 0, attempted: 0, sent: 0, failed: 0, capped: 0 });
  if (path === "/export")
    return new Response(new Blob([""]), {
      status: 200,
      headers: { "Content-Type": "application/octet-stream" },
    });

  // 그 외 /admin/* 등 알 수 없는 API — 빈 객체로 안전 처리(네트워크 hang 방지).
  if (path.startsWith("/admin/")) return jsonRes({});
  return undefined;
}

export function installMock(): void {
  // admin 세션 시드 — App 이 getUser() 로 로그인 여부를 보고 로그인 화면을 건너뛴다.
  localStorage.setItem("lc_token", "mock-token");
  localStorage.setItem("lc_user", "mock-admin");
  localStorage.setItem("lc_role", "admin");
  db = seed();

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url =
      typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    const hit = route(url, method, init);
    return hit ?? realFetch(input, init);
  };

  // eslint-disable-next-line no-console
  console.info("[mock] 백엔드 없이 메모리 mock 으로 동작 중 (admin 자동로그인, 샘플 %d건)", db.length);
}
