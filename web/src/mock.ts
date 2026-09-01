// 백엔드 없이 프론트엔드만 개발하기 위한 메모리 mock. `npm run dev:mock`(vite --mode mock)일 때만
// main.tsx 가 installMock() 을 호출한다. window.fetch 를 가로채 검증 큐 API 를 메모리 상태로 응답하므로
// api.ts·컴포넌트는 전혀 수정하지 않는다. 상태는 메모리 전용 — 새로고침 시 초기 샘플로 리셋된다.
// admin 세션을 localStorage 에 시드해 로그인 화면을 건너뛴다. 매칭 안 되는 API 는 빈/스텁으로 응답.
import type {
  AuditEntry,
  CandidateInfo,
  ClaimFilter,
  CompanySearchItem,
  DashboardSummary,
  Listed,
  ReviewItem,
} from "./types";

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

// 지역(KR 시/도, #243) — leadcrawler/region.py KR_REGIONS 와 동일 목록·순서. ReviewItem 자체엔
// 없는 필드라(BE 도 필터 전용, 응답엔 안 실음) id→지역 맵으로 따로 들고 매칭에만 쓴다.
const KR_REGIONS = [
  "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
  "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
];
// 실측 KR 샘플의 소재지 인근 시/도(수기표) — 합성분은 seed() 가 KR_REGIONS 순환 배정.
const HAND_REGION: Record<string, string> = {
  c1: "인천", c11: "경기", c2: "경기", c3: "경기", c4: "경기",
  c5: "경기", c6: "충남", c7: "부산", c8: "충북", c9: "경기", c10: "경기",
};
// 비KR 행은 지역 없음(BE DiscoveredCompanyRow.region 미기입과 동일 — 필터 걸면 자연 배제).
let regionById = new Map<string, string>();

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
    note: null,
    has_attachment: null,
    manager: null,
    // 전화는 BE #429 로 큐 API 가 실제로 내려주지만, 수집률이 100% 가 아니라 빈 셀이
    // 흔하다 — mock 은 유/무를 섞어 '있음/—' 표시와 유무 정렬을 함께 확인한다.
    phone: null,
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
      // 첨부 유무·담당자 기존값 보유 샘플(#382) — 재진입 시 값 표시와 '미확인으로 되돌리기
      // 불가' 안내를 mock 만으로 확인할 수 있게 한다.
      has_attachment: true,
      manager: "김담당 과장",
      phone: "02-1234-5678",
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
      phone: "+82-31-000-0000", // 국제표기 셀 — 폭·nowrap 확인용
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
      has_attachment: false, // '첨부 무' 표시 확인용(#382)
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
        // 전화 유무를 섞어 '있음/—' 셀과 유무 정렬(전화 컬럼)을 함께 확인한다.
        phone: variant === 2 ? null : `02-${1000 + (i % 9000)}-0000`,
      }),
    );
  }
  return rows;
}

// 전체 큐 시드 = 실측 11건 + 합성 89건(총 100건). 합성분 상장여부는 3주기로 배정.
function seed(): ReviewItem[] {
  regionById = new Map();
  const hand = handSamples().map((r) => {
    if (r.country === "KR") regionById.set(r.id, HAND_REGION[r.id] ?? KR_REGIONS[0]);
    return {
      ...r,
      listed: HAND_LISTED[r.id] ?? r.listed,
      market: HAND_MARKET[r.id] ?? null,
    };
  });
  const synth = synthSamples(89).map((r, i) => {
    if (r.country === "KR") regionById.set(r.id, KR_REGIONS[i % KR_REGIONS.length]);
    return {
      ...r,
      listed: LISTED_CYCLE[i % LISTED_CYCLE.length],
      // listed(3주기 첫 슬롯)만 보드 부여 — BE 와 동일하게 미상장은 null.
      market:
        LISTED_CYCLE[i % LISTED_CYCLE.length] === "listed"
          ? MARKET_CYCLE[i % MARKET_CYCLE.length]
          : null,
    };
  });
  return [...hand, ...synth];
}

let db: ReviewItem[] = seed();
// 내(mock 단일 사용자) 점유 id — 처리(확정/거부)하면 점유도 소멸. 새로고침 시 리셋(메모리 전용).
let claimedIds = new Set<string>();

// 최근 검증 이력 — 확정/거부(setStatus)마다 최신순으로 쌓인다.
// 시드 25건: 담당자·액션을 섞어 관리자 화면의 필터·페이지네이션 동작을 mock 에서 확인할 수 있게.
const MOCK_AUDIT_ACTORS = ["mock-admin", "worker1", "worker2"];
const audit: AuditEntry[] = db.slice(0, 25).map((it, i) => ({
  id: `audit-seed-${i}`,
  review_id: it.id,
  actor_username: MOCK_AUDIT_ACTORS[i % MOCK_AUDIT_ACTORS.length],
  action: i % 3 === 0 ? "rejected" : "confirmed",
  selected: i % 3 === 0 ? null : it.candidates[0]?.value ?? null,
  company_name: it.name,
  at: new Date(Date.now() - (i + 1) * 3600_000).toISOString(),
}));

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

// 크롤 작업(POST/GET /admin/crawl, /history, /cancel) 시뮬레이션은 2026-09-01 제거됐다
// (#448, BE #450) — 웹 즉시크롤이 사라지고 세그먼트 작업 목이 그 자리를 대신한다.

// 백필 작업(#352) — 무타이머 방식(폴링 시점 경과시간으로 진행 계산)이되,
// 지속형 consumer 라 '완료(done)'가 없다. 대상을 소진해도 계속 돌며(신규 유입분 처리)
// 종료는 중지·예산소진뿐. 데모 시간에 맞춰 처리속도는 실 BE 보다 크게 잡았다.
const BF_RATE = { C: 420, A: 300 }; // 초당 처리(모의) — 초기 대상 소진까지 ~30초.
const BF_CANCEL_DELAY_MS = 2000; // 협조적 취소 흉내(요청 후 확정까지).
// 예산 소진 데모 — A 가 먼저(45초), C 가 나중(60초)에 끝나 그 사이 구간이 '한쪽만 중단'
// 상태가 된다(진행 카드의 "일부 작업 중단됨" 분기 확인용).
const BF_BUDGET_MS = { A: 45_000, C: 60_000 };

// 잔여 풀 — overview 의 기준값. 작업이 끝나면 처리한 만큼 깎아 재조회 시 감소를 보여준다.
const backfillPool = { C: 12_043, A: 8_210 };
let backfillDrained = false; // 종료분 차감 1회성 가드

type BackfillJobState = {
  id: string;
  countries: string;
  industries: string; // 포함식(#372) — excludeIndustries 와 배타(둘 중 하나만 채워진다)
  excludeIndustries: string;
  excludeListed: boolean;
  startedAt: number;
  initial: { C: number; A: number };
  cancelRequestedAt: number | null;
};
let backfillJob: BackfillJobState | null = null;

type BackfillJobJson = { status: string } & Record<string, unknown>;

function idleBackfill(track: "C" | "A"): BackfillJobJson {
  return {
    id: null, track, status: "idle", countries: "", industries: "", exclude_industries: "",
    exclude_listed: false, batch: 0, workers: 0, max_batches: 0, min_queue: 0,
    initial_target: 0, remaining: 0, processed: 0, resolved: 0, promoted: 0, emails: 0,
    batches_done: 0, generation: 0, recycles: 0, crash_restarts: 0, pid: null,
    cancel_requested: false, stop_reason: null, error: null, triggered_by: null,
    started_at: null, updated_at: null, progress_at: null, finished_at: null,
  };
}

// 트랙 종료 시점·사유 — 취소가 예산소진보다 먼저면 취소로 확정(둘 다 미도달이면 진행 중).
function backfillEnd(
  j: BackfillJobState,
  track: "C" | "A",
): { at: number; status: string; reason: string } | null {
  const cancelAt =
    j.cancelRequestedAt !== null ? j.cancelRequestedAt + BF_CANCEL_DELAY_MS : Infinity;
  const budgetAt = j.startedAt + BF_BUDGET_MS[track];
  const at = Math.min(cancelAt, budgetAt);
  if (Date.now() < at) return null;
  return cancelAt <= budgetAt
    ? { at: cancelAt, status: "cancelled", reason: "operator" }
    : { at: budgetAt, status: "budget_exhausted", reason: "monthly_budget" };
}

function backfillTrackInfo(track: "C" | "A"): BackfillJobJson {
  const j = backfillJob;
  if (!j) return idleBackfill(track);
  const end = backfillEnd(j, track);
  const endMs = end?.at ?? Date.now();
  const elapsedSec = Math.max(0, (endMs - j.startedAt) / 1000);
  // 지속형 — processed 는 initial 을 넘어서도 계속 증가한다(대상 소진 후 신규 유입분).
  const processed = Math.floor(elapsedSec * BF_RATE[track]);
  const initial = j.initial[track];
  const batches = Math.floor(processed / 200);
  return {
    id: `${j.id}-${track}`,
    track,
    status: end ? end.status : "running",
    countries: j.countries,
    industries: j.industries,
    exclude_industries: j.excludeIndustries,
    exclude_listed: j.excludeListed,
    batch: 200,
    workers: track === "C" ? 6 : 4,
    max_batches: 0,
    min_queue: 500,
    initial_target: initial,
    remaining: Math.max(0, initial - processed),
    processed,
    // 트랙별 산출물 — C 는 도메인 해석·승격, A 는 이메일(반대쪽은 항상 0).
    resolved: track === "C" ? Math.floor(processed * 0.34) : 0,
    promoted: track === "C" ? Math.floor(processed * 0.31) : 0,
    emails: track === "A" ? Math.floor(processed * 0.22) : 0,
    batches_done: batches,
    generation: 1 + Math.floor(batches / 40),
    recycles: Math.floor(batches / 40),
    crash_restarts: 0,
    pid: track === "C" ? 13820 : 13821,
    cancel_requested: j.cancelRequestedAt !== null,
    stop_reason: end?.reason ?? null,
    error: null,
    triggered_by: "mock-admin",
    started_at: new Date(j.startedAt).toISOString(),
    updated_at: new Date(endMs).toISOString(),
    progress_at: new Date(endMs).toISOString(),
    finished_at: end ? new Date(end.at).toISOString() : null,
  };
}

// 포함식·제외식 동시 지정 거부(#372) — FE 는 모드 하나만 보내는 계약이라 여기 걸리면 버그다.
function backfillIndustryConflict(): Response {
  return jsonRes(
    { detail: "industries 와 exclude_industries 는 동시 지정 불가(둘 중 하나만)" },
    422,
  );
}

// 두 트랙 스냅샷 + 전부 종료했으면 잔여 풀에서 처리분 1회 차감(overview 재조회 시 감소 확인용).
function backfillStatusJson(): { resolve: BackfillJobJson; fill: BackfillJobJson } {
  const resolve = backfillTrackInfo("C");
  const fill = backfillTrackInfo("A");
  if (backfillJob && !backfillDrained && resolve.status !== "running" && fill.status !== "running") {
    backfillPool.C = Math.max(0, backfillPool.C - (resolve.processed as number));
    backfillPool.A = Math.max(0, backfillPool.A - (fill.processed as number));
    backfillDrained = true;
  }
  return { resolve, fill };
}

// --- 세그먼트 작업 큐(#403) --------------------------------------------
// 트랙 S 는 한 번에 1건만 실행 — 나머지는 우선순위 큐(낮을수록 먼저). 진행은 시간 기반으로
// 계산해 discover(발견) → promote(승격) → done 단계 전이를 눈으로 확인할 수 있게 한다.
const SEG_DISCOVER_MS = 6_000;
const SEG_PROMOTE_MS = 24_000;
const SEG_DISCOVERED = 119; // 라이브 E2E 실측치와 같은 자릿수
const SEG_TARGET = 127;
// running 에 건 pause/cancel 은 즉시 반영되지 않는다 — 이만큼 지나야 실제로 전이한다.
const SEG_STOP_LAG_MS = 2_500;

type SegStatus = "queued" | "running" | "paused" | "done" | "cancelled";

interface SegJob {
  id: string;
  countries: string;
  industries: string;
  listed: Listed;
  regions: string;
  priority: number;
  createdAt: number;
  startedAt: number | null; // 실행 시작(대기 중이면 null) — 일시중지 누적분만큼 뒤로 민다
  progressMs: number; // 일시중지 시점까지 누적 진행 시간
  status: SegStatus;
  pending: null | "pause" | "cancel"; // running 에 접수된 요청(수 초 뒤 실제 전이)
  pendingAt: number;
  finishedAt: number | null;
}

const segJobs: SegJob[] = [];
let segSeq = 0;

const segElapsed = (j: SegJob, now: number): number =>
  j.startedAt === null ? j.progressMs : now - j.startedAt;

// 상태 전이 — 조회·변경 요청마다 1회 돌려 '살아 있는 큐'처럼 보이게 한다.
// ① 접수된 pause/cancel 을 지연 후 반영 ② 완주 판정 ③ 빈 슬롯에 대기열 1건 투입.
function segTick(): void {
  const now = Date.now();
  for (const j of segJobs) {
    if (j.status !== "running") continue;
    if (j.pending && now - j.pendingAt >= SEG_STOP_LAG_MS) {
      j.progressMs = segElapsed(j, now);
      j.startedAt = null;
      j.status = j.pending === "pause" ? "paused" : "cancelled";
      if (j.status === "cancelled") j.finishedAt = now;
      j.pending = null;
      continue;
    }
    if (segElapsed(j, now) >= SEG_DISCOVER_MS + SEG_PROMOTE_MS) {
      j.status = "done";
      j.finishedAt = now;
      j.progressMs = SEG_DISCOVER_MS + SEG_PROMOTE_MS;
      j.startedAt = null;
    }
  }
  // 디스패처 — 실행 중이 없으면 우선순위(낮을수록 먼저)·요청시각 순으로 1건만 올린다.
  if (!segJobs.some((j) => j.status === "running")) {
    const next = segQueued()[0];
    if (next) {
      next.status = "running";
      next.startedAt = now - next.progressMs;
    }
  }
}

const segQueued = (): SegJob[] =>
  segJobs
    .filter((j) => j.status === "queued")
    .sort((a, b) => a.priority - b.priority || a.createdAt - b.createdAt);

// 진행 카운터 — discover 구간엔 initial_target 이 0(아직 모름), promote 구간부터 확정된다.
function segCounters(j: SegJob): {
  stage: string;
  discovered: number;
  initial_target: number;
  processed: number;
} {
  const el = Math.max(0, Math.min(segElapsed(j, Date.now()), SEG_DISCOVER_MS + SEG_PROMOTE_MS));
  if (j.status === "queued") return { stage: "", discovered: 0, initial_target: 0, processed: 0 };
  if (el < SEG_DISCOVER_MS) {
    const r = el / SEG_DISCOVER_MS;
    return {
      stage: "discover",
      discovered: Math.round(SEG_DISCOVERED * r),
      initial_target: 0,
      processed: 0,
    };
  }
  const r = (el - SEG_DISCOVER_MS) / SEG_PROMOTE_MS;
  return {
    stage: j.status === "done" ? "done" : "promote",
    discovered: SEG_DISCOVERED,
    initial_target: SEG_TARGET,
    processed: Math.min(SEG_TARGET, Math.round(SEG_TARGET * r)),
  };
}

function segInfo(j: SegJob): Record<string, unknown> {
  const c = segCounters(j);
  const queue = segQueued();
  const at = new Date(j.finishedAt ?? Date.now()).toISOString();
  return {
    id: j.id,
    track: "S",
    status: j.status,
    countries: j.countries,
    industries: j.industries,
    listed: j.listed,
    regions: j.regions,
    priority: j.priority,
    stage: c.stage,
    discovered: c.discovered,
    initial_target: c.initial_target,
    remaining: Math.max(0, c.initial_target - c.processed),
    processed: c.processed,
    promoted: Math.round(c.processed * 0.08),
    emails: Math.round(c.processed * 0.02),
    failed_items: 0,
    batches_done: Math.floor(c.processed / 100),
    promote_cursor: c.processed > 0 ? `mock-cursor-${c.processed}` : null,
    queue_position: j.status === "queued" ? queue.indexOf(j) + 1 : null,
    generation: 0,
    recycles: 0,
    crash_restarts: 0,
    pid: j.status === "running" ? 39064 : null,
    cancel_requested: j.pending !== null,
    // BE #398 과 동일하게 — pause 는 요청 접수 시점(running)부터 심기고 paused 로 전이한
    // 뒤에도 남는다(종료 사유가 아니다). 취소만 종료 시 'operator'.
    stop_reason:
      j.pending === "pause" || j.status === "paused"
        ? "pause"
        : j.status === "cancelled"
          ? "operator"
          : null,
    error: null,
    triggered_by: "mock-admin",
    started_at: new Date(j.createdAt).toISOString(), // 트랙 S 는 '요청 생성 시각'
    updated_at: at,
    progress_at: at,
    finished_at: j.finishedAt ? new Date(j.finishedAt).toISOString() : null,
    exclude_industries: "",
    exclude_listed: false,
    batch: 100,
    workers: 3,
    max_batches: 20,
    min_queue: 0,
    resolved: 0,
  };
}

// 확정/거부 요청 본문(BE 스키마와 같은 snake_case) — 필드 부재·null = 변경 없음.
interface ConfirmBody {
  selected?: string | null;
  homepage?: string | null;
  has_form?: boolean | null;
  note?: string | null;
  remove_emails?: string[] | null;
  has_attachment?: boolean | null; // 첨부파일 유무(#382)
  manager?: string | null; // 담당자명(#382, 최대 64자·빈 문자열=지움)
  phone?: string | null; // 대표 전화(#432, 최대 64자·빈 문자열=지움·숫자 0개면 422)
}

function setStatus(
  id: string,
  status: ReviewItem["status"],
  body: ConfirmBody = {},
): ReviewItem | null {
  const { selected, homepage, has_form: hasForm, note, remove_emails: removeEmails } = body;
  const it = db.find((x) => x.id === id);
  if (!it) return null;
  // 실존하지 않는 이메일 삭제(BE PR#314) — 후보에서 제거(대소문자 무시). selected 반영보다
  // 먼저 실행해 "죽은 주소 삭제 + 남은 후보 선택"이 한 요청에 처리된다. 지운 주소가 현재
  // 선택이면 해제(FE 가 선택에서 제외해 보내므로 아래 selected 로 남은 후보가 승계된다).
  if (removeEmails && removeEmails.length) {
    const rm = new Set(removeEmails.map((e) => e.toLowerCase()));
    it.candidates = it.candidates.filter((c) => !rm.has(c.value.toLowerCase()));
    if (it.selected && rm.has(it.selected.toLowerCase())) it.selected = null;
  }
  it.status = status;
  if (selected !== undefined) it.selected = selected;
  if (homepage) it.homepage = homepage; // null=변경 없음(FE 확정 계약과 동일)
  // ponytail: 문의폼 URL 자체는 모른다(체크박스는 유무만 교정) — 있음 표기는 사이트 홈으로
  // 대체 링크, 없음 표기는 null. 실 BE 는 URL 미상 상태를 어떻게 저장/노출할지 별도 결정 필요.
  if (hasForm !== undefined && hasForm !== null) it.form = hasForm ? (it.form ?? it.homepage) : null;
  // note: null=변경 없음(BE 계약과 동일), 빈 문자열=메모 삭제.
  if (note !== undefined && note !== null) it.note = note.trim() === "" ? null : note.trim();
  // 첨부파일 유무(#382): null=변경 없음 — 유/무만 반영된다(미확인으로 되돌리기 불가).
  if (body.has_attachment !== undefined && body.has_attachment !== null)
    it.has_attachment = body.has_attachment;
  // 담당자(#382): null=변경 없음, 빈 문자열=지움. 64자 초과는 BE 가 422 — mock 도 동일히 막는다.
  if (body.manager !== undefined && body.manager !== null)
    it.manager = body.manager.trim() === "" ? null : body.manager.trim();
  // 대표 전화(#432): null=변경 없음, 빈 문자열=지움, 값=사람 입력으로 교체(BE 는 크롤/등록처
  // 연락처를 지우고 manual·1.0 1건으로 대체 — 화면에는 큐 DTO 의 phone 으로 같게 보인다).
  if (body.phone !== undefined && body.phone !== null)
    it.phone = body.phone.trim() === "" ? null : body.phone.trim();
  it.assignee = "mock-admin";
  it.reviewed_at = new Date().toISOString();
  claimedIds.delete(id); // 처리 완료 — 점유 종료.
  // 검증 이력 기록 — 관리자 '최근 검증 이력' 표에 즉시 반영(최신이 앞).
  audit.unshift({
    id: `audit-${audit.length}-${id}`,
    review_id: id,
    actor_username: "mock-admin",
    action: status,
    selected: it.selected,
    company_name: it.name,
    at: it.reviewed_at,
  });
  return it;
}

// --- 보유 데이터 대시보드(#378) ----------------------------------------
// 큐 카운트는 메모리 db 실측(점유 여부까지 반영), 회사 분포는 그 db 를 회사 목록으로 간주해
// 집계한다. 국가 미상('')·업종 '미분류' 폴백 표기를 화면에서 확인할 수 있도록, 두 축 어디에도
// 값이 없는 회사를 UNKNOWN_COMPANIES 건 함께 싣는다(총계와 분포 합은 그대로 일치).
const UNKNOWN_COMPANIES = 7;
// 원장 백로그 — mock 엔 discovered_company 개념이 없어 고정 데모값(실 BE 는 group by 집계).
const MOCK_LEDGER_BACKLOG = { domained: 1_240, undomained: 3_105, absorbed: 418 };

// n 내림차순 → 키 오름차순(BE _ranked 와 동일 정렬).
function ranked(counts: Map<string, number>): { key: string; n: number }[] {
  return [...counts]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([key, n]) => ({ key, n }));
}

function dashboardSummaryJson(): DashboardSummary {
  const queue = { pending_unclaimed: 0, pending_claimed: 0, confirmed: 0, rejected: 0 };
  const countries = new Map<string, number>([["", UNKNOWN_COMPANIES]]);
  const industries = new Map<string, number>([["미분류", UNKNOWN_COMPANIES]]);
  for (const it of db) {
    if (it.status === "pending") {
      queue[claimedIds.has(it.id) ? "pending_claimed" : "pending_unclaimed"] += 1;
    } else {
      queue[it.status] += 1;
    }
    countries.set(it.country, (countries.get(it.country) ?? 0) + 1);
    industries.set(it.industry, (industries.get(it.industry) ?? 0) + 1);
  }
  const total = db.length + UNKNOWN_COMPANIES;
  const b = MOCK_LEDGER_BACKLOG;
  return {
    ledger: {
      // BE 불변식 — total = 승격 + 도메인확정 미승격 + 도메인 미확정 + 흡수분.
      total: total + b.domained + b.undomained + b.absorbed,
      promoted: total,
      domained_unpromoted: b.domained,
      undomained_unpromoted: b.undomained,
      absorbed: b.absorbed,
    },
    companies: {
      total,
      with_email: db.filter((it) => it.candidates.length > 0).length,
      by_country: ranked(countries).map(({ key, n }) => ({ country: key, n })),
      by_industry: ranked(industries).map(({ key, n }) => ({ industry: key, n })),
    },
    queue,
  };
}

// --- 회사 DB 검색(#418) -------------------------------------------------
// BE 와 같은 매칭 범위(회사명·홈페이지·이메일/문의폼 URL·발견 원장 보관 상호)·이름순 정렬·
// 서버 페이지네이션을 흉내낸다. 큐(db) 회사 전량 + 큐 미적재 3건으로 구성해, 검증 큐에 없는
// 회사(review_* = null)·배제 role 이메일·비활성 회사까지 화면에서 확인할 수 있게 한다.

// 원장 보관 상호(name_eng) — 응답 스키마엔 없고 **매칭에만** 쓰이는 값이라 별도 맵으로 둔다.
// 표기는 소스마다 다르다: DART·NPS 는 국내 기업의 영문명, EDINET 은 일본 기업의 일문 원문
// (BE #412 로 표시명 name 이 영문 상호가 되면서 자리를 맞바꿨다). 두 방향을 다 깔아 둬야
// 안내 문구("국내 영문명, 일본 일문 원문")를 목에서 그대로 확인할 수 있다.
const MOCK_NAME_ENG: Record<string, string> = {
  "cs-jp1": "ビーホールディングス",
  "cs-kr1": "Hanbit Precision Co., Ltd.",
};

const MOCK_UNQUEUED: CompanySearchItem[] = [
  {
    id: "cs-jp1",
    canonical_key: "dom:bee-holdings.jp",
    // EDINET 표시명은 공식 영문 상호(BE #412) — 일문 원문은 MOCK_NAME_ENG 에서 검색만 걸린다.
    name: "Bee Holdings Co., Ltd.",
    country: "JP",
    industry: "기계·산업장비",
    homepage: "https://bee-holdings.jp",
    is_active: true,
    site_alive: true,
    listed: "listed",
    market: null,
    emails: [],
    form: "https://bee-holdings.jp/contact",
    phone: "+81-3-0000-0000",
    review_id: null,
    review_status: null,
    review_assignee: null,
  },
  {
    id: "cs-kr1",
    canonical_key: "dom:hanbit-precision.co.kr",
    name: "한빛정밀",
    country: "KR",
    industry: "기계·산업장비",
    homepage: "https://hanbit-precision.co.kr",
    is_active: true,
    site_alive: true,
    listed: "unlisted",
    market: null,
    // 채용 주소만 있는 회사 — 배제 role 이라 큐에 안 올라간다(제약 §3).
    emails: [{ value: "recruit@hanbit-precision.co.kr", role: "hr", status: null }],
    form: null,
    phone: "031-000-0000",
    review_id: null,
    review_status: null,
    review_assignee: null,
  },
  {
    id: "cs-kr2",
    canonical_key: "name:kr:대성산업기계",
    name: "대성산업기계",
    country: "KR",
    industry: "기계·산업장비",
    homepage: "https://daesung-machine.co.kr",
    is_active: false, // 소멸 처리분 — 지금 연락하면 안 되는 회사
    site_alive: false,
    listed: "unknown",
    market: null,
    emails: [],
    form: null,
    phone: null, // 연락처가 아무것도 없는 회사 — 전화 컬럼 '—' 확인용
    review_id: null,
    review_status: null,
    review_assignee: null,
  },
];

// 큐 항목 → 회사 검색 행. canonical_key 는 BE dedup.canonical_key() 티어 형식(dom:/name:)을
// 형태만 흉내낸다(mock 은 원장이 없어 실제 키를 재현할 수 없다). 합성 샘플은 홈페이지 호스트가
// 전부 example.com 이라 dom: 키가 겹쳐 보이는데, 실DB(회사마다 다른 도메인)에선 안 생기는
// mock 데이터 특성이다.
function companyRow(it: ReviewItem): CompanySearchItem {
  let key = `name:${it.country.toLowerCase()}:${it.name}`;
  try {
    if (it.homepage) key = `dom:${new URL(it.homepage).hostname.replace(/^www\./, "")}`;
  } catch {
    // 무효 URL — name: 티어 유지.
  }
  return {
    id: it.company_id,
    canonical_key: key,
    name: it.name,
    country: it.country,
    industry: it.industry,
    homepage: it.homepage,
    is_active: true,
    site_alive: it.site_alive,
    listed: it.listed,
    market: it.market,
    emails: it.candidates.map((c) => ({
      value: c.value,
      role: c.value.toLowerCase().startsWith("ir@") ? "ir" : "general",
      status: c.email_status,
    })),
    form: it.form,
    phone: it.phone ?? null,
    review_id: it.id,
    review_status: it.status,
    review_assignee: it.assignee,
  };
}

// q 부분일치(대소문자 무시) — BE 와 동일하게 %·_ 는 와일드카드가 아닌 문자 그대로 취급된다
// (JS includes 는 애초에 패턴 해석을 안 하므로 자연히 같은 결과).
function companyMatches(c: CompanySearchItem, needle: string): boolean {
  const hay = [
    c.name,
    c.homepage ?? "",
    c.form ?? "",
    MOCK_NAME_ENG[c.id] ?? "",
    ...c.emails.map((e) => e.value),
  ];
  return hay.some((h) => h.toLowerCase().includes(needle));
}

function companySearchJson(u: URL): Response {
  const q = (u.searchParams.get("q") ?? "").trim();
  if (!q) return jsonRes({ detail: [{ loc: ["query", "q"], msg: "검색어가 필요합니다" }] }, 422);
  // 검증 큐 상태 필터(BE #424) — 선택 파라미터. enum 밖 값은 BE 와 같이 422.
  const rs = u.searchParams.get("review_status");
  if (rs !== null && !["pending", "confirmed", "rejected"].includes(rs))
    return jsonRes(
      { detail: [{ loc: ["query", "review_status"], msg: "허용되지 않는 상태" }] },
      422,
    );
  const needle = q.toLowerCase();
  const all = [...db.map(companyRow), ...MOCK_UNQUEUED]
    .filter((c) => companyMatches(c, needle))
    // 큐 미적재(review_status=null)는 어떤 상태 값에도 매치되지 않는다(BE 계약).
    .filter((c) => !rs || c.review_status === rs)
    .sort((a, b) => a.name.localeCompare(b.name, "ko") || a.id.localeCompare(b.id));
  const limit = Number(u.searchParams.get("limit") ?? "50") || 50;
  const offset = Number(u.searchParams.get("offset") ?? "0") || 0;
  return jsonRes({ items: all.slice(offset, offset + limit), total: all.length, limit, offset });
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
  const regions = csvSet(f.region);
  if (regions.size && !regions.has((regionById.get(it.id) ?? "").toLowerCase())) return false;
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
    region: u.searchParams.get("region") ?? body.region ?? "",
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

  // 보유 데이터 대시보드 스냅샷(#378) — 진입 1회 조회라 mock 도 매 호출 즉석 집계.
  if (path === "/dashboard/summary" && method === "GET") return jsonRes(dashboardSummaryJson());

  // 검증 큐 필터 옵션 — 국가(countries.py) 전량 + 구분 택소노미 42+미분류(#115, BE 와 동일).
  if (path === "/queue/filters" && method === "GET") {
    return jsonRes({
      countries: MOCK_COUNTRIES,
      industries: MOCK_QUEUE_INDUSTRIES,
      listed: ["listed", "unlisted", "unknown"],
      // 시장 어휘 — BE 계약(distinct market)과 동일하게 데이터 실측분만 정렬해 내려준다.
      markets: [...new Set(db.map((x) => x.market).filter((m): m is string => !!m))].sort(),
      // 지역 어휘 — BE 계약(distinct region)과 동일하게 실제 배정된 KR 시/도만 정렬해 내려준다.
      regions: [...new Set(regionById.values())].sort(),
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
  // status=confirmed|rejected 면 내(mock-admin) 처리 내역, 최신 처리 먼저(계약 확장분과 동일).
  if (path === "/queue/mine" && method === "GET") {
    const status = u.searchParams.get("status");
    if (status === "confirmed" || status === "rejected")
      return jsonRes(
        db
          .filter((x) => x.status === status && x.assignee === "mock-admin")
          .sort((a, b) => (b.reviewed_at ?? "").localeCompare(a.reviewed_at ?? "")),
      );
    return jsonRes(pending().filter((x) => claimedIds.has(x.id)));
  }

  const confirm = path.match(/^\/queue\/([^/]+)\/confirm$/);
  if (confirm && method === "POST") {
    let body: ConfirmBody = {};
    try {
      body = JSON.parse(String(init?.body ?? "{}")) as ConfirmBody;
    } catch {
      // 본문 없음/파싱 실패 — 선택 없이 확정.
    }
    const selected = body.selected ?? null;
    // 삭제 대상을 selected 로 함께 보내면 모순 — BE 처럼 400(FE 는 선택에서 제외해 보낸다).
    if (selected && body.remove_emails?.some((e) => e.toLowerCase() === selected.toLowerCase()))
      return jsonRes({ detail: "삭제 대상 이메일을 선택할 수 없습니다" }, 400);
    // 담당자 64자 초과 — BE(#381)와 동일하게 422(입력란 maxLength 로 정상 경로에선 안 걸린다).
    if (body.manager && body.manager.length > 64)
      return jsonRes({ detail: "담당자는 64자를 넘을 수 없습니다" }, 422);
    // 전화 검증(#432) — BE 와 동일 순서·동일 사유로 422: 64자 초과 / NUL / 숫자 0개.
    // 정상 경로에선 입력란 maxLength + normPhone 이 먼저 걸러 여기까지 오지 않는다.
    if (body.phone != null) {
      if (body.phone.length > 64)
        return jsonRes({ detail: "전화번호는 64자를 넘을 수 없습니다" }, 422);
      if (body.phone.includes("\0")) return jsonRes({ detail: "NUL 문자는 쓸 수 없습니다" }, 422);
      if (body.phone.trim() && !/\d/.test(body.phone))
        return jsonRes({ detail: "전화번호에 숫자가 없습니다" }, 422);
    }
    const it = setStatus(confirm[1], "confirmed", body);
    return it ? jsonRes(it) : jsonRes({ detail: "검증 항목을 찾을 수 없습니다" }, 404);
  }
  const reject = path.match(/^\/queue\/([^/]+)\/reject$/);
  if (reject && method === "POST") {
    // 거부 본문도 remove_emails 를 지원(선택) — BE RejectRequest 와 동일. 없으면 상태만 변경.
    let removeEmails: string[] | null = null;
    try {
      const body = JSON.parse(String(init?.body ?? "{}")) as { remove_emails?: string[] | null };
      removeEmails = body.remove_emails ?? null;
    } catch {
      // 본문 없음/파싱 실패 — 삭제 없이 거부.
    }
    const it = setStatus(reject[1], "rejected", { remove_emails: removeEmails });
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
  if (path === "/admin/audit")
    return jsonRes(audit.slice(0, Number(u.searchParams.get("limit") ?? 100) || 100));
  // 직원별 일일 처리량(#279·#303) — audit 로그를 처리일(로컬 날짜) 기준으로 담당자별 집계.
  // 시드 audit 이 최근 25시간에 걸쳐 mock-admin/worker1/worker2 로 흩어져 있어 날짜를 바꿔가며
  // 다중 담당자·빈 날짜 케이스를 mock 만으로 확인할 수 있다(BE 계약과 동일: 처리량 많은 순 정렬).
  if (path === "/admin/stats/review-daily" && method === "GET") {
    const date = u.searchParams.get("date") ?? new Date().toLocaleDateString("sv-SE");
    const localDate = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString("sv-SE") : "");
    const byUser = new Map<string, { confirmed: number; rejected: number }>();
    audit
      .filter((a) => (a.action === "confirmed" || a.action === "rejected") && localDate(a.at) === date)
      .forEach((a) => {
        const stat = byUser.get(a.actor_username) ?? { confirmed: 0, rejected: 0 };
        if (a.action === "confirmed") stat.confirmed++;
        else stat.rejected++;
        byUser.set(a.actor_username, stat);
      });
    const items = [...byUser.entries()]
      .map(([username, stat]) => ({ username, ...stat }))
      .sort((a, b) => b.confirmed + b.rejected - (a.confirmed + a.rejected));
    return jsonRes({ date, items });
  }
  // 크롤 타깃 픽커 옵션 — BE 와 동일하게 국가/업종 표준 목록 전량(/queue/filters 와 같은 출처).
  // 회사 DB 검색(#418) — 큐 상태와 무관한 전체 조회라 db 외 미적재 회사도 함께 대상.
  if (path === "/admin/companies" && method === "GET") return companySearchJson(u);
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
  // --- 백필 제어(#352) ---------------------------------------------------
  if (path === "/admin/backfill/overview") {
    const countries = csvSet(u.searchParams.get("countries"));
    const included = csvSet(u.searchParams.get("industries"));
    const excluded = csvSet(u.searchParams.get("exclude_industries"));
    if (included.size && excluded.size) return backfillIndustryConflict();
    const exclListed = u.searchParams.get("exclude_listed") === "true";
    // 대략치 축소 계수 — 실 BE 의 조인 결과가 아니라 필터 반응을 눈으로 보기 위한 흉내.
    let f = 1;
    if (countries.size) f *= Math.min(1, (countries.size * 2) / MOCK_COUNTRIES.length);
    // 포함식은 고른 업종만 남고(택소노미 비중), 제외식은 고른 만큼 깎인다 — 방향 반대.
    if (included.size) f *= Math.min(1, included.size / MOCK_QUEUE_INDUSTRIES.length);
    f *= Math.max(0.1, 1 - excluded.size * 0.07);
    if (exclListed) f *= 0.8;
    // 검증 큐는 BE 계약대로 **국가 조건만** 반영한다(업종은 포함·제외 어느 쪽도 미반영).
    const queue = pending().filter(
      (x) => !countries.size || countries.has(x.country.toLowerCase()),
    ).length;
    return jsonRes({
      resolve_pending: Math.round(backfillPool.C * f),
      fill_pending: Math.round(backfillPool.A * f),
      queue_pending: queue,
    });
  }
  if (path === "/admin/backfill/status") return jsonRes(backfillStatusJson());
  if (path === "/admin/backfill/start" && method === "POST") {
    let body: {
      countries?: string;
      industries?: string;
      exclude_industries?: string;
      exclude_listed?: boolean;
    } = {};
    try {
      body = JSON.parse(String(init?.body ?? "{}"));
    } catch {
      // 본문 없음 — 전부 기본값(전세계 전체).
    }
    // 422(포함·제외 배타)를 409(활성 존재)보다 먼저 — BE 와 같은 순서(잘못된 요청은
    // 서버 상태와 무관하게 항상 같은 응답).
    if (csvSet(body.industries).size && csvSet(body.exclude_industries).size)
      return backfillIndustryConflict();
    const s = backfillStatusJson();
    if (s.resolve.status === "running" || s.fill.status === "running")
      return jsonRes({ detail: "활성 백필 존재(트랙 C, A)" }, 409);
    backfillJob = {
      id: `mock-backfill-${Date.now()}`,
      countries: body.countries ?? "",
      industries: body.industries ?? "",
      excludeIndustries: body.exclude_industries ?? "",
      excludeListed: !!body.exclude_listed,
      startedAt: Date.now(),
      initial: { C: backfillPool.C, A: backfillPool.A },
      cancelRequestedAt: null,
    };
    backfillDrained = false;
    return jsonRes(backfillStatusJson(), 202);
  }
  if (path === "/admin/backfill/stop" && method === "POST") {
    const s = backfillStatusJson();
    if (s.resolve.status !== "running" && s.fill.status !== "running")
      return jsonRes({ detail: "활성 백필이 없습니다" }, 404);
    if (backfillJob && backfillJob.cancelRequestedAt === null)
      backfillJob.cancelRequestedAt = Date.now();
    return jsonRes(backfillStatusJson());
  }

  // --- 세그먼트 작업 큐(#403) -------------------------------------------
  if (path === "/admin/segment-jobs/preview") {
    const countries = csvSet(u.searchParams.get("countries"));
    const industries = csvSet(u.searchParams.get("industries"));
    if (!countries.size || !industries.size)
      return jsonRes({ detail: "국가·업종은 각각 1개 이상 지정해야 합니다" }, 422);
    const regions = u.searchParams.get("regions") ?? "";
    // 세그먼트 = 국가 × 업종 × 지역(전 지역이면 KR 17개, 직접 선택이면 고른 수).
    const regionFactor = regions === "all" ? 17 : Math.max(1, csvSet(regions).size);
    return jsonRes({
      segments: countries.size * industries.size * regionFactor,
      promote_pending: 2_413,
      max_segments: 300,
    });
  }
  if (path === "/admin/segment-jobs" && method === "GET") {
    segTick();
    // 정렬은 BE 계약 그대로 — running → queued(priority, 요청시각) → 나머지 최신순.
    const rank = (j: SegJob) => (j.status === "running" ? 0 : j.status === "queued" ? 1 : 2);
    const sorted = [...segJobs].sort(
      (a, b) =>
        rank(a) - rank(b) ||
        (rank(a) === 1 ? a.priority - b.priority || a.createdAt - b.createdAt : 0) ||
        b.createdAt - a.createdAt,
    );
    const offset = Number(u.searchParams.get("offset") ?? "0");
    const limit = Number(u.searchParams.get("limit") ?? "50");
    return jsonRes({
      items: sorted.slice(offset, offset + limit).map(segInfo),
      total: sorted.length,
    });
  }
  if (path === "/admin/segment-jobs" && method === "POST") {
    let body: { countries?: string; industries?: string; listed?: Listed; regions?: string; priority?: number } = {};
    try {
      body = JSON.parse(String(init?.body ?? "{}"));
    } catch {
      // 본문 없음 — 아래 필수값 검증에서 422.
    }
    if (!csvSet(body.countries).size || !csvSet(body.industries).size)
      return jsonRes({ detail: "국가·업종은 각각 1개 이상 지정해야 합니다" }, 422);
    if (body.regions && !csvSet(body.countries).has("kr"))
      return jsonRes({ detail: "지역은 국가에 KR 이 포함될 때만 지정할 수 있습니다" }, 422);
    segTick();
    const job: SegJob = {
      id: `bf_mock${(++segSeq).toString().padStart(4, "0")}`,
      countries: body.countries ?? "",
      industries: body.industries ?? "",
      listed: body.listed ?? "unknown",
      regions: body.regions ?? "",
      priority: body.priority ?? 100,
      createdAt: Date.now(),
      startedAt: null,
      progressMs: 0,
      status: "queued",
      pending: null,
      pendingAt: 0,
      finishedAt: null,
    };
    segJobs.push(job);
    segTick(); // 실행 중이 없으면 이 자리에서 바로 running 으로 올라간다
    return jsonRes(segInfo(job), 201);
  }
  if (path.startsWith("/admin/segment-jobs/")) {
    segTick();
    const [id, action] = path.slice("/admin/segment-jobs/".length).split("/");
    const job = segJobs.find((j) => j.id === id);
    if (!job) return jsonRes({ detail: "작업을 찾을 수 없습니다" }, 404);
    if (!action && method === "GET") return jsonRes(segInfo(job));
    // 우선순위 변경 — queued·paused 만(그 외 409).
    if (!action && method === "PATCH") {
      if (job.status !== "queued" && job.status !== "paused")
        return jsonRes({ detail: "대기·일시중지 상태에서만 우선순위를 바꿀 수 있습니다" }, 409);
      try {
        job.priority = JSON.parse(String(init?.body ?? "{}")).priority ?? job.priority;
      } catch {
        // 본문 없음 — 변경 없이 현재 상태 반환.
      }
      return jsonRes(segInfo(job));
    }
    if (method !== "POST") return jsonRes({ detail: "지원하지 않는 요청" }, 404);
    if (action === "pause" || action === "cancel") {
      if (job.status === "queued") {
        // 대기 중이면 즉시 전이(실행 중일 때만 지연 반영).
        job.status = action === "pause" ? "paused" : "cancelled";
        if (job.status === "cancelled") job.finishedAt = Date.now();
        segTick();
        return jsonRes(segInfo(job));
      }
      if (action === "cancel" && job.status === "paused") {
        job.status = "cancelled";
        job.finishedAt = Date.now();
        return jsonRes(segInfo(job));
      }
      if (job.status !== "running")
        return jsonRes({ detail: "이미 종료된 작업입니다" }, 409);
      if (!job.pending) {
        job.pending = action;
        job.pendingAt = Date.now();
      }
      return jsonRes(segInfo(job));
    }
    if (action === "resume") {
      // BE requeue_segment_job 허용 집합 = paused·failed·budget_exhausted·cancelled(#407).
      // 목은 failed·budget_exhausted 로 전이하지 않아 SegStatus 에 없다 — 나머지 둘만 검사.
      if (job.status !== "paused" && job.status !== "cancelled")
        return jsonRes({ detail: "재개할 수 없는 상태입니다" }, 409);
      job.status = "queued";
      // BE 는 재개 시 finished_at·stop_reason·cancel_requested 를 지운다. segInfo 가 이 값들을
      // finishedAt·pending 에서 파생하므로 함께 풀어야 재개 후 '종료' 흔적이 남지 않는다.
      job.finishedAt = null;
      job.pending = null;
      segTick();
      return jsonRes(segInfo(job));
    }
    return jsonRes({ detail: "지원하지 않는 요청" }, 404);
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
    return jsonRes({
      dry_run: true,
      recipients: 0,
      attempted: 0,
      sent: 0,
      failed: 0,
      uncertain: 0,
      skipped: 0,
      capped: 0,
    });
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
