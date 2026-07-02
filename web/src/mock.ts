// 백엔드 없이 프론트엔드만 개발하기 위한 메모리 mock. `npm run dev:mock`(vite --mode mock)일 때만
// main.tsx 가 installMock() 을 호출한다. window.fetch 를 가로채 검증 큐 API 를 메모리 상태로 응답하므로
// api.ts·컴포넌트는 전혀 수정하지 않는다. 상태는 메모리 전용 — 새로고침 시 초기 샘플로 리셋된다.
// admin 세션을 localStorage 에 시드해 로그인 화면을 건너뛴다. 매칭 안 되는 API 는 빈/스텁으로 응답.
import type { CandidateInfo, ClaimFilter, Listed, ReviewItem } from "./types";

// 영구 배정 계약(PRD-queue-claim-permanent) — claim 1회 = +BATCH 추가, 총량 CAP 상한.
const BATCH = 30;
const CAP = 100;

// 국가 옵션 — leadcrawler/sources/countries.py supported_countries() 전량(우선순위 순).
// iso2=필터/저장 토큰, label=한글 표시명(korean_label), aliases=검색용(영문/ISO).
const MOCK_COUNTRIES: { iso2: string; label: string; aliases: string[] }[] = [
  { iso2: "US", label: "미국", aliases: ["us", "usa", "united states", "america"] },
  { iso2: "KR", label: "대한민국", aliases: ["kr", "kor", "korea", "south korea", "한국"] },
  { iso2: "JP", label: "일본", aliases: ["jp", "jpn", "japan", "日本"] },
  { iso2: "CN", label: "중국", aliases: ["cn", "chn", "china", "prc", "中国"] },
  { iso2: "PH", label: "필리핀", aliases: ["ph", "phl", "philippines"] },
  { iso2: "TH", label: "태국", aliases: ["th", "tha", "thailand"] },
  { iso2: "ID", label: "인도네시아", aliases: ["id", "idn", "indonesia"] },
  { iso2: "MY", label: "말레이시아", aliases: ["my", "mys", "malaysia"] },
  { iso2: "SG", label: "싱가포르", aliases: ["sg", "sgp", "singapore"] },
  { iso2: "VN", label: "베트남", aliases: ["vn", "vnm", "vietnam"] },
  { iso2: "IN", label: "인도", aliases: ["in", "ind", "india", "bharat"] },
  { iso2: "TW", label: "대만", aliases: ["tw", "twn", "taiwan"] },
  { iso2: "HK", label: "홍콩", aliases: ["hk", "hkg", "hong kong"] },
  { iso2: "GB", label: "영국", aliases: ["gb", "uk", "gbr", "united kingdom", "britain"] },
  { iso2: "DE", label: "독일", aliases: ["de", "deu", "germany", "deutschland"] },
  { iso2: "FR", label: "프랑스", aliases: ["fr", "fra", "france"] },
  { iso2: "AU", label: "호주", aliases: ["au", "aus", "australia"] },
  { iso2: "CA", label: "캐나다", aliases: ["ca", "can", "canada"] },
  { iso2: "BR", label: "브라질", aliases: ["br", "bra", "brazil", "brasil"] },
];

// 업종 옵션 — leadcrawler/sources/industry.py _EN_INDUSTRY 키 전량(supported_industries()).
// value/label=한글 업종키('it' 만 영문), aliases=영문 검색어.
const MOCK_INDUSTRIES: { value: string; label: string; aliases: string[] }[] = [
  { value: "건설", label: "건설", aliases: ["construction"] },
  { value: "제조", label: "제조", aliases: ["manufacturing"] },
  { value: "금융", label: "금융", aliases: ["finance"] },
  { value: "it", label: "it", aliases: ["it"] },
  { value: "소프트웨어", label: "소프트웨어", aliases: ["software"] },
  { value: "바이오", label: "바이오", aliases: ["biotech"] },
  { value: "제약", label: "제약", aliases: ["pharmaceutical"] },
  { value: "유통", label: "유통", aliases: ["retail"] },
  { value: "도소매", label: "도소매", aliases: ["retail"] },
  { value: "운송", label: "운송", aliases: ["transport"] },
  { value: "물류", label: "물류", aliases: ["logistics"] },
  { value: "에너지", label: "에너지", aliases: ["energy"] },
  { value: "부동산", label: "부동산", aliases: ["real estate"] },
  { value: "식품", label: "식품", aliases: ["food"] },
  { value: "화학", label: "화학", aliases: ["chemical"] },
  { value: "자동차", label: "자동차", aliases: ["automotive"] },
  { value: "반도체", label: "반도체", aliases: ["semiconductor"] },
  { value: "통신", label: "통신", aliases: ["telecommunications"] },
];

const COUNTRY_CODES = MOCK_COUNTRIES.map((c) => c.iso2);
const INDUSTRY_KEYS = MOCK_INDUSTRIES.map((i) => i.value);
const LISTED_CYCLE: Listed[] = ["listed", "unlisted", "unknown"];

// 상장여부는 큐 DTO(ReviewItem)에 없고 BE 가 DiscoveredCompanyRow 조인으로 거른다.
// mock 에선 id→listed 사이드맵으로 그 조인을 흉내 낸다(필터 동작 시연용). 합성분은 seed() 가 채운다.
const HAND_LISTED: Record<string, Listed> = {
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
let MOCK_LISTED: Record<string, Listed> = { ...HAND_LISTED };

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

// 국내 중소·중견 제조사 실측 샘플 — 홈페이지는 모두 실제 접속되는 사이트(팝업으로 열림). 이메일은
// 예시(가짜)이며 실제 주소 아님. 후보 1/다수로 변형을 섞어 라디오 선택·직접입력 UI 를 함께 검증한다.
function handSamples(): ReviewItem[] {
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

// 필터 동작 검증용 합성 샘플 — 국가·업종을 다양하게 돌려 전체 큐를 채운다. 업종·국가 값은 BE 표준
// 키와 동일해 필터가 실제로 걸린다(홈페이지는 example.com 더미라 팝업 실접속은 안 됨 — 필터/카운트용).
function synthSamples(count: number): ReviewItem[] {
  const rows: ReviewItem[] = [];
  for (let i = 0; i < count; i++) {
    const country = COUNTRY_CODES[i % COUNTRY_CODES.length];
    // 업종은 *5 로 국가 주기와 어긋나게 돌려 (국가×업종) 조합을 다양화(gcd(5,18)=1 → 전 업종 순회).
    const industry = INDUSTRY_KEYS[(i * 5) % INDUSTRY_KEYS.length];
    const id = `g${i + 1}`;
    // 이메일 상태를 3주기로 변형 — valid / unknown / 없음(폼만) 셀을 골고루 만든다.
    const variant = i % 3;
    const candidates =
      variant === 0
        ? [cand(`ir@${id}.example.com`)]
        : variant === 1
          ? [cand(`contact@${id}.example.com`, "unknown", true, null)]
          : [];
    rows.push(
      mk({
        id,
        name: `${industry} 컴퍼니 ${i + 1} (${country})`,
        industry,
        country,
        homepage: variant === 2 ? null : `https://example.com/${id}`,
        form: variant === 2 ? "https://example.com/contact" : null,
        email_status: variant === 0 ? "valid" : variant === 1 ? "unknown" : null,
        email_mx: variant === 2 ? null : true,
        email_smtp: variant === 0 ? true : null,
        candidates,
      }),
    );
  }
  return rows;
}

// 전체 큐 시드 = 실측 11건 + 합성 89건(총 100건). 합성분 상장여부는 3주기로 배정해 MOCK_LISTED 채움.
function seed(): ReviewItem[] {
  const hand = handSamples();
  const synth = synthSamples(89);
  MOCK_LISTED = { ...HAND_LISTED };
  synth.forEach((r, i) => {
    MOCK_LISTED[r.id] = LISTED_CYCLE[i % LISTED_CYCLE.length];
  });
  return [...hand, ...synth];
}

let db: ReviewItem[] = seed();
// 내(mock 단일 사용자) 점유 id — 처리(확정/거부)하면 점유도 소멸. 새로고침 시 리셋(메모리 전용).
let claimedIds = new Set<string>();

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
  claimedIds.delete(id); // 처리 완료 — 점유 종료.
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

  // 인증 — mock 은 무조건 admin. 단, username "locked" 면 429(스로틀 카운트다운 시연용).
  if (path === "/auth/login" && method === "POST") {
    let body: { username?: string } = {};
    try {
      body = JSON.parse(String(init?.body ?? "{}"));
    } catch {
      // 본문 없음/비JSON.
    }
    if (body.username?.trim().toLowerCase() === "locked") {
      return new Response(
        JSON.stringify({ detail: "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요." }),
        { status: 429, headers: { "Content-Type": "application/json", "Retry-After": "10" } },
      );
    }
    return jsonRes({ token: "mock-token", username: "mock-admin", role: "admin" });
  }
  if (path === "/auth/logout") return jsonRes({});
  if (path === "/health") return jsonRes({ status: "ok" });

  // 검증 큐 필터 옵션 — 국가(countries.py)·업종(industry.py) 표준 목록 전량.
  if (path === "/queue/filters" && method === "GET") {
    return jsonRes({
      countries: MOCK_COUNTRIES,
      industries: MOCK_INDUSTRIES,
      listed: ["listed", "unlisted", "unknown"],
    });
  }

  // 검증 큐 — 전체큐(GET /queue)에선 점유 중인 행이 아예 안 보인다(미점유만, BE 와 동일).
  if (path === "/queue" && method === "GET") {
    const status = u.searchParams.get("status");
    const limit = Number(u.searchParams.get("limit") ?? "50");
    const offset = Number(u.searchParams.get("offset") ?? "0");
    const f = readFilter(u, init);
    const filtered = db.filter(
      (x) => (!status || x.status === status) && !claimedIds.has(x.id) && matches(x, f),
    );
    return jsonRes({
      items: filtered.slice(offset, offset + limit),
      total: filtered.length,
      limit,
      offset,
    });
  }
  // 작업 받기(추가형) — 미점유 pending 에서 필터 매칭분 +BATCH 점유(총량 CAP 상한).
  // 응답은 필터와 무관하게 내 점유 전체(BE §4.2 와 동일).
  if (path === "/queue/claim" && method === "POST") {
    const f = readFilter(u, init);
    const room = Math.max(0, CAP - claimedIds.size);
    pending()
      .filter((x) => !claimedIds.has(x.id) && matches(x, f))
      .slice(0, Math.min(BATCH, room))
      .forEach((x) => claimedIds.add(x.id));
    return jsonRes(pending().filter((x) => claimedIds.has(x.id)));
  }
  // 내 작업분 조회(부작용 없음) — 몇 번을 불러도 점유 불변.
  if (path === "/queue/mine" && method === "GET")
    return jsonRes(pending().filter((x) => claimedIds.has(x.id)));

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
  claimedIds = new Set();

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
