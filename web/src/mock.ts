// 백엔드 없이 프론트엔드만 개발하기 위한 메모리 mock. `npm run dev:mock`(vite --mode mock)일 때만
// main.tsx 가 installMock() 을 호출한다. window.fetch 를 가로채 검증 큐 API 를 메모리 상태로 응답하므로
// api.ts·컴포넌트는 전혀 수정하지 않는다. 상태는 메모리 전용 — 새로고침 시 초기 샘플로 리셋된다.
// admin 세션을 localStorage 에 시드해 로그인 화면을 건너뛴다. 매칭 안 되는 API 는 빈/스텁으로 응답.
import type { CandidateInfo, ClaimFilter, Listed, ReviewItem } from "./types";

// 영구 배정 계약(PRD-queue-claim-permanent) — claim 1회 = +BATCH 추가, 총량 CAP 상한.
const BATCH = 30;
const CAP = 100;

// 국가 옵션 — leadcrawler/sources/countries.py _COUNTRIES 전량(우선순위 순), aliases 도 동일.
// iso2=필터/저장 토큰, label=한글 표시명(korean_label=첫 한글 별칭), aliases=검색 매칭용.
const MOCK_COUNTRIES: { iso2: string; label: string; aliases: string[] }[] = [
  { iso2: "US", label: "미국", aliases: [
    "us", "usa", "u.s.", "u.s.a.", "united states", "united states of america",
    "america", "미국"] },
  { iso2: "KR", label: "대한민국", aliases: [
    "kr", "kor", "korea", "south korea", "republic of korea", "rok",
    "대한민국", "한국", "코리아", "남한"] },
  { iso2: "JP", label: "일본", aliases: ["jp", "jpn", "japan", "nippon", "nihon", "일본", "日本"] },
  { iso2: "CN", label: "중국", aliases: [
    "cn", "chn", "china", "prc", "people's republic of china", "mainland china",
    "중국", "中国", "中國"] },
  { iso2: "PH", label: "필리핀", aliases: [
    "ph", "phl", "philippines", "republic of the philippines", "pilipinas", "필리핀"] },
  { iso2: "TH", label: "태국", aliases: ["th", "tha", "thailand", "kingdom of thailand", "태국", "타이"] },
  { iso2: "ID", label: "인도네시아", aliases: [
    "id", "idn", "indonesia", "republic of indonesia", "인도네시아"] },
  { iso2: "MY", label: "말레이시아", aliases: ["my", "mys", "malaysia", "말레이시아"] },
  { iso2: "SG", label: "싱가포르", aliases: [
    "sg", "sgp", "singapore", "republic of singapore", "싱가포르", "新加坡"] },
  { iso2: "VN", label: "베트남", aliases: [
    "vn", "vnm", "vietnam", "viet nam", "socialist republic of vietnam", "베트남", "越南"] },
  { iso2: "IN", label: "인도", aliases: ["in", "ind", "india", "republic of india", "bharat", "인도"] },
  { iso2: "TW", label: "대만", aliases: ["tw", "twn", "taiwan", "chinese taipei", "대만", "台灣", "台湾"] },
  { iso2: "HK", label: "홍콩", aliases: ["hk", "hkg", "hong kong", "hong kong sar", "hksar", "홍콩", "香港"] },
  { iso2: "GB", label: "영국", aliases: [
    "gb", "uk", "u.k.", "gbr", "united kingdom",
    "united kingdom of great britain and northern ireland", "britain", "great britain", "영국"] },
  { iso2: "DE", label: "독일", aliases: [
    "de", "deu", "germany", "deutschland", "federal republic of germany", "독일"] },
  { iso2: "FR", label: "프랑스", aliases: [
    "fr", "fra", "france", "french republic", "république française", "프랑스"] },
  { iso2: "AU", label: "호주", aliases: [
    "au", "aus", "australia", "commonwealth of australia", "호주", "오스트레일리아"] },
  { iso2: "CA", label: "캐나다", aliases: ["ca", "can", "canada", "캐나다"] },
  { iso2: "BR", label: "브라질", aliases: [
    "br", "bra", "brazil", "brasil", "federative republic of brazil", "브라질"] },
];

// 크롤 타깃용 업종 12종 — leadcrawler/sources/industry.py supported_industries() 전량.
// #143 이후 큐/발송 탭과 같은 택소노미 어휘(코드 매핑이 있는 대분류만 노출, 순서 동일).
const MOCK_INDUSTRIES: { value: string; label: string; aliases: string[] }[] = [
  { value: "반도체·디스플레이", label: "반도체·디스플레이", aliases: ["semiconductor"] },
  { value: "자동차·모빌리티", label: "자동차·모빌리티", aliases: ["automotive"] },
  { value: "화학·석유화학", label: "화학·석유화학", aliases: ["chemical"] },
  { value: "식품·음료", label: "식품·음료", aliases: ["food"] },
  { value: "제약·바이오", label: "제약·바이오", aliases: ["biotech"] },
  { value: "건설·엔지니어링", label: "건설·엔지니어링", aliases: ["construction"] },
  { value: "부동산·개발", label: "부동산·개발", aliases: ["real estate"] },
  { value: "IT·소프트웨어", label: "IT·소프트웨어", aliases: ["software"] },
  { value: "통신·네트워크", label: "통신·네트워크", aliases: ["telecommunications"] },
  { value: "유통·도소매", label: "유통·도소매", aliases: ["retail"] },
  { value: "물류·운송", label: "물류·운송", aliases: ["transport"] },
  { value: "에너지·전력", label: "에너지·전력", aliases: ["energy"] },
];

// 구분 택소노미 42종 — leadcrawler/sources/taxonomy.py INDUSTRY_TAXONOMY 전량(순서 동일).
// 큐 행 industry 저장 어휘이자 /queue/filters 구분 옵션(#115)의 출처.
const MOCK_TAXONOMY: string[] = [
  // 제조
  "반도체·디스플레이", "전자·전기부품", "자동차·모빌리티", "기계·산업장비", "조선·중공업",
  "화학·석유화학", "이차전지·소재", "철강·금속", "식품·음료", "제약·바이오",
  "의료기기", "화장품·뷰티", "섬유·의류·패션", "가구·생활용품", "기타 제조",
  // 건설·부동산
  "건설·엔지니어링", "건축자재", "부동산·개발",
  // 금융
  "은행", "증권·자산운용", "보험", "핀테크·결제",
  // IT·통신·미디어
  "IT·소프트웨어", "게임", "정보보안", "AI·데이터", "통신·네트워크",
  "미디어·엔터테인먼트", "광고·마케팅",
  // 유통·소비
  "이커머스·플랫폼", "유통·도소매", "물류·운송", "여행·숙박·항공", "외식·프랜차이즈",
  // 에너지·인프라
  "에너지·전력", "신재생에너지", "환경·폐기물",
  // 서비스·기타
  "의료·헬스케어", "교육", "전문서비스", "농림·수산", "공공·비영리",
];
// /queue/filters 구분 옵션 — BE #115 와 동일(value=label=한글, aliases 빈 배열, 맨 뒤 '미분류').
const MOCK_QUEUE_INDUSTRIES = [...MOCK_TAXONOMY, "미분류"].map((l) => ({
  value: l,
  label: l,
  aliases: [] as string[],
}));

const COUNTRY_CODES = MOCK_COUNTRIES.map((c) => c.iso2);
// 합성 아이템 업종 주기 — 큐 저장 어휘(구분 택소노미 + '미분류')를 그대로 돌려
// /queue/filters 옵션(#115)이 mock 에서도 전 옵션 실제로 걸리게 한다.
const SYNTH_INDUSTRY_KEYS = MOCK_QUEUE_INDUSTRIES.map((i) => i.value);
const LISTED_CYCLE: Listed[] = ["listed", "unlisted", "unknown"];

// 상장여부 — BE 는 DiscoveredCompanyRow 조인으로 채워 DTO(listed)에 싣는다.
// mock 실측분은 이 수기표에서, 합성분은 seed() 가 3주기로 배정한다.
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

// 시장 보드(#136) — 상장(listed) 실측분만 실제 보드로. 비상장/미상은 null(표기 없음).
const HAND_MARKET: Record<string, string> = {
  c1: "KOSDAQ",
  c11: "KOSDAQ",
  c2: "KOSPI",
  c4: "KOSDAQ",
  c6: "KOSDAQ",
  c9: "KOSDAQ",
};
// 합성 상장분 보드 주기 — 국내외 보드가 골고루 보이게.
const MARKET_CYCLE = ["KOSPI", "KOSDAQ", "NASDAQ", "NYSE"];

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
    field: p.industry ?? "건설·엔지니어링",
    candidates: [],
    selected: null,
    status: "pending",
    assignee: null,
    reviewed_at: null,
    country: "KR",
    industry: "건설·엔지니어링",
    homepage: null,
    site_alive: true,
    form: null,
    email_status: null,
    email_mx: null,
    email_smtp: null,
    listed: "unknown",
    market: null,
    ...p,
  };
}

// 국내 중소·중견 제조사 실측 샘플 — 홈페이지는 모두 실제 접속되는 사이트(팝업으로 열림). 이메일은
// 예시(가짜)이며 실제 주소 아님. 후보 1/다수로 변형을 섞어 라디오 선택·직접입력 UI 를 함께 검증한다.
// 업종은 실DB 값 형태와 동일하게 taxonomy.py INDUSTRY_TAXONOMY 대분류만 쓴다(자유 텍스트 금지).
function handSamples(): ReviewItem[] {
  return [
    mk({
      id: "c1",
      name: "로보티즈",
      industry: "기계·산업장비",
      homepage: "https://www.robotis.com/",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("ir@robotis.com"), cand("contact@robotis.com", "unknown", true, null)],
    }),
    mk({
      id: "c11",
      name: "서울반도체",
      industry: "반도체·디스플레이",
      homepage: "https://www.seoulsemicon.com",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("ir@seoulsemicon.com")],
    }),
    mk({
      id: "c2",
      name: "한미반도체",
      industry: "반도체·디스플레이",
      homepage: "https://www.hanmisemi.com",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("ir@hanmisemi.com"), cand("contact@hanmisemi.com", "unknown", true, null)],
    }),
    mk({
      id: "c3",
      name: "파크시스템스",
      industry: "기계·산업장비",
      homepage: "https://www.parksystems.com",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("sales@parksystems.com")],
    }),
    mk({
      id: "c4",
      name: "심텍",
      industry: "전자·전기부품",
      homepage: "https://www.simmtech.com",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("ir@simmtech.com")],
    }),
    mk({
      id: "c5",
      name: "동진쎄미켐",
      industry: "화학·석유화학",
      homepage: "https://www.dongjin.com",
      email_status: "unknown",
      email_mx: true,
      email_smtp: null,
      candidates: [cand("info@dongjin.com", "unknown", true, null)],
    }),
    mk({
      id: "c6",
      name: "솔브레인",
      industry: "화학·석유화학",
      homepage: "https://www.soulbrain.co.kr",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("ir@soulbrain.co.kr")],
    }),
    mk({
      id: "c7",
      name: "대주전자재료",
      industry: "전자·전기부품",
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
      industry: "이차전지·소재",
      homepage: "https://www.nanonm.com",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("info@nanonm.com")],
    }),
    mk({
      id: "c9",
      name: "에스에프에이",
      industry: "기계·산업장비",
      homepage: "https://www.sfa.co.kr",
      email_status: "valid",
      email_mx: true,
      email_smtp: true,
      candidates: [cand("ir@sfa.co.kr")],
    }),
    mk({
      id: "c10",
      name: "인탑스",
      industry: "전자·전기부품",
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
    // 업종은 *5 로 국가 주기와 어긋나게 돌려 (국가×업종) 조합을 다양화(gcd(5,43)=1 → 전 업종 순회).
    const industry = SYNTH_INDUSTRY_KEYS[(i * 5) % SYNTH_INDUSTRY_KEYS.length];
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

// 전체 큐 시드 = 실측 11건 + 합성 89건(총 100건). 합성분 상장여부는 3주기로 배정.
function seed(): ReviewItem[] {
  const hand = handSamples().map((r) => ({
    ...r,
    listed: HAND_LISTED[r.id] ?? r.listed,
    market: HAND_MARKET[r.id] ?? null,
  }));
  const synth = synthSamples(89).map((r, i) => ({
    ...r,
    listed: LISTED_CYCLE[i % LISTED_CYCLE.length],
    // listed(3주기 첫 슬롯)만 보드 부여 — BE 와 동일하게 미상장은 null.
    market:
      LISTED_CYCLE[i % LISTED_CYCLE.length] === "listed"
        ? MARKET_CYCLE[i % MARKET_CYCLE.length]
        : null,
  }));
  return [...hand, ...synth];
}

let db: ReviewItem[] = seed();
// 내(mock 단일 사용자) 점유 id — 처리(확정/거부)하면 점유도 소멸. 새로고침 시 리셋(메모리 전용).
let claimedIds = new Set<string>();

// 크롤 타깃 — GET/PUT /admin/crawl-target 이 공유하는 메모리 상태(BE CrawlTargetInfo 기본값과 동일).
type CrawlTargetState = {
  countries: string;
  industries: string;
  listed: string;
  persist: boolean;
  updated_by: string | null;
  updated_at: string | null;
};
const DEFAULT_CRAWL_TARGET: CrawlTargetState = {
  countries: "",
  industries: "",
  listed: "unknown",
  persist: true,
  updated_by: null,
  updated_at: null,
};
let crawlTarget: CrawlTargetState = { ...DEFAULT_CRAWL_TARGET };

// 크롤 작업 — POST /admin/crawl 로 시작해 시간 경과로 진행하는 시뮬레이션. 타이머 없이
// GET 폴링 시점에 경과시간으로 세그먼트 진행을 계산한다(BE CrawlJobInfo 계약과 동일 필드).
const CRAWL_SEGMENTS_TOTAL = 12;
const CRAWL_SEC_PER_SEGMENT = 2; // 총 ~24초 완주 — 진행바·중지 버튼을 눈으로 확인할 시간.
const CRAWL_CANCEL_DELAY_MS = 2000; // 협조적 취소 흉내 — 요청 후 다음 세그먼트 경계에서 확정.
type CrawlJobState = {
  id: string;
  countries: string;
  industries: string;
  listed: string;
  persist: boolean;
  continuous: boolean; // true 면 중지(취소)까지 라운드 반복(#132) — 자동 완료 없음.
  startedAt: number; // epoch ms
  cancelRequestedAt: number | null;
  finalStatus: "done" | "cancelled";
  finishedAt: number | null;
};
let crawlJob: CrawlJobState | null = null;

// 응답 JSON 모양 — route 분기가 status 만 보므로 나머지는 느슨하게 둔다(jsonRes 는 unknown).
type CrawlJobJson = { status: string } & Record<string, unknown>;

const IDLE_CRAWL_JOB: CrawlJobJson = {
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
  mode: "once",
  rounds_done: 0,
  error: null,
  cancel_requested: false,
  triggered_by: null,
  started_at: null,
  updated_at: null,
  finished_at: null,
};

// 현재 크롤 작업 스냅샷 — 호출 시점 기준으로 진행/종료를 확정해 CrawlJob JSON 을 만든다.
function crawlJobInfo(): CrawlJobJson {
  if (!crawlJob) return IDLE_CRAWL_JOB;
  const j = crawlJob;
  const now = Date.now();
  const roundMs = CRAWL_SEGMENTS_TOTAL * CRAWL_SEC_PER_SEGMENT * 1000;
  if (j.finishedAt === null) {
    if (j.cancelRequestedAt !== null && now - j.cancelRequestedAt >= CRAWL_CANCEL_DELAY_MS) {
      j.finalStatus = "cancelled";
      j.finishedAt = now;
    } else if (!j.continuous && now - j.startedAt >= roundMs) {
      // 단발(once)만 자동 완료 — 연속은 중지 전까지 라운드를 계속 돈다.
      j.finalStatus = "done";
      j.finishedAt = j.startedAt + roundMs;
    }
  }
  const endMs = j.finishedAt ?? now;
  const elapsed = endMs - j.startedAt;
  // 연속: 카운터는 현재 라운드 기준(BE 계약과 동일) — 라운드 경계로 나머지 연산.
  const done = j.continuous
    ? Math.floor((elapsed % roundMs) / (CRAWL_SEC_PER_SEGMENT * 1000))
    : Math.min(Math.floor(elapsed / (CRAWL_SEC_PER_SEGMENT * 1000)), CRAWL_SEGMENTS_TOTAL);
  const rounds = j.continuous
    ? Math.floor(elapsed / roundMs)
    : j.finishedAt !== null && j.finalStatus === "done"
      ? 1
      : 0;
  return {
    id: j.id,
    status: j.finishedAt !== null ? j.finalStatus : "running",
    countries: j.countries,
    industries: j.industries,
    listed: j.listed,
    persist: j.persist,
    segments_total: CRAWL_SEGMENTS_TOTAL,
    segments_done: done,
    // 그럴싸한 비율의 가짜 카운터 — 발견 > 처리 > 저장(실존만) 순으로 좁아진다.
    discovered: done * 17,
    enriched: done * 11,
    saved: done * 6,
    mode: j.continuous ? "continuous" : "once",
    rounds_done: rounds,
    error: null,
    cancel_requested: j.cancelRequestedAt !== null,
    triggered_by: "mock-admin",
    started_at: new Date(j.startedAt).toISOString(),
    updated_at: new Date(endMs).toISOString(),
    finished_at: j.finishedAt !== null ? new Date(j.finishedAt).toISOString() : null,
  };
}

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
  if (f.listed && it.listed !== f.listed) return false;
  const markets = csvSet(f.market ?? "");
  if (markets.size && !markets.has((it.market ?? "").toLowerCase())) return false;
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
    market: u.searchParams.get("market") ?? body.market ?? "",
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

  // 검증 큐 필터 옵션 — 국가(countries.py) 전량 + 구분 택소노미 42+미분류(#115, BE 와 동일).
  if (path === "/queue/filters" && method === "GET") {
    return jsonRes({
      countries: MOCK_COUNTRIES,
      industries: MOCK_QUEUE_INDUSTRIES,
      listed: ["listed", "unlisted", "unknown"],
      // 시장 어휘 — BE 계약(distinct market)과 동일하게 데이터 실측분만 정렬해 내려준다.
      markets: [...new Set(db.map((x) => x.market).filter((m): m is string => !!m))].sort(),
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
  // 단, 계정 목록·회수는 FE-5(점유 컬럼+회수 버튼) 시연용으로 실제 점유 상태를 반영한다:
  // 작업 받기 → claimed 증가 → 회수 → 점유 전부 풀로(전체큐 복귀) 흐름을 mock 만으로 확인 가능.
  if (path === "/admin/users" && method === "GET")
    return jsonRes([
      {
        id: "u1",
        username: "mock-admin",
        role: "admin",
        is_active: true,
        created_at: null,
        confirmed: db.filter((x) => x.status === "confirmed").length,
        rejected: db.filter((x) => x.status === "rejected").length,
        claimed: claimedIds.size,
        last_action_at: null,
      },
    ]);
  const reclaimM = path.match(/^\/admin\/users\/([^/]+)\/reclaim$/);
  if (reclaimM && method === "POST") {
    const n = claimedIds.size;
    claimedIds = new Set();
    return jsonRes({ reclaimed: n });
  }
  if (path === "/admin/audit") return jsonRes([]);
  // 크롤 타깃 픽커 옵션 — BE 와 동일하게 국가/업종 표준 목록 전량(/queue/filters 와 같은 출처).
  if (path === "/admin/countries") return jsonRes(MOCK_COUNTRIES);
  if (path === "/admin/industries") return jsonRes(MOCK_INDUSTRIES);
  if (path === "/admin/crawl-target" && method === "GET") return jsonRes(crawlTarget);
  if (path === "/admin/crawl-target" && method === "PUT") {
    let body: Partial<CrawlTargetState> = {};
    try {
      body = JSON.parse(String(init?.body ?? "{}"));
    } catch {
      // 본문 없음/비JSON.
    }
    // BE CrawlTargetRequest 는 업종 최소 1개(트림 후) — 빈 업종은 422.
    if (!body.industries?.trim()) return jsonRes({ detail: "업종은 최소 1개 필요합니다" }, 422);
    crawlTarget = {
      countries: body.countries ?? "",
      industries: body.industries.trim(),
      listed: body.listed ?? "unknown",
      persist: body.persist ?? true,
      updated_by: "mock-admin",
      updated_at: new Date().toISOString(),
    };
    return jsonRes(crawlTarget);
  }
  if (path === "/admin/crawl" && method === "GET") return jsonRes(crawlJobInfo());
  if (path === "/admin/crawl" && method === "POST") {
    if (crawlJobInfo().status === "running")
      return jsonRes({ detail: "이미 진행 중인 크롤이 있습니다" }, 409);
    let body: Partial<CrawlTargetState> & { continuous?: boolean } = {};
    try {
      body = JSON.parse(String(init?.body ?? "{}"));
    } catch {
      // 본문 없음/비JSON.
    }
    if (!body.industries?.trim()) return jsonRes({ detail: "업종은 최소 1개 필요합니다" }, 422);
    crawlJob = {
      id: `mock-crawl-${Date.now()}`,
      countries: body.countries ?? "",
      industries: body.industries.trim(),
      listed: body.listed ?? "unknown",
      persist: body.persist ?? true,
      continuous: body.continuous ?? false,
      startedAt: Date.now(),
      cancelRequestedAt: null,
      finalStatus: "done",
      finishedAt: null,
    };
    return jsonRes(crawlJobInfo(), 202);
  }
  if (path === "/admin/crawl/cancel" && method === "POST") {
    if (crawlJobInfo().status !== "running")
      return jsonRes({ detail: "진행 중인 크롤이 없습니다" }, 404);
    if (crawlJob && crawlJob.cancelRequestedAt === null) crawlJob.cancelRequestedAt = Date.now();
    return jsonRes(crawlJobInfo());
  }
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
  // 기존 lc_role 이 있으면 유지 — 콘솔에서 "user" 로 바꿔 직원 화면을 볼 수 있게.
  localStorage.setItem("lc_role", localStorage.getItem("lc_role") ?? "admin");
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
