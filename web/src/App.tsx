import { useCallback, useEffect, useRef, useState } from "react";
import {
  confirmReview,
  fetchQueue,
  getRole,
  getUser,
  logout,
  rejectReview,
  setAuthErrorHandler,
} from "./api";
import { errMsg } from "./format";
import { krInScope, LISTED_FILTER_OPTIONS, useQueueFilterOpts } from "./filterOptions";
import { Admin } from "./components/Admin";
import { Dashboard, type QueueJump } from "./components/Dashboard";
import { MyWork } from "./components/MyWork";
import { FilterPopover, pickSummary } from "./components/FilterPopover";
import { MultiPicker, type PickerOption } from "./components/MultiPicker";
import { QueueTable } from "./components/QueueTable";
import { TableSkeleton } from "./components/TableSkeleton";
import { Login } from "./components/Login";
import { BarChart3, ChevronLeft, ChevronRight, Settings } from "lucide-react";
import { BTN, INPUT, tabCls } from "./ui";
import { Toaster } from "sonner";
import { ErrorBox } from "./components/ErrorBox";
import type { ConfirmEdits, Listed, ReviewItem, ReviewStatus, Role } from "./types";

type Filter = ReviewStatus | "";
type View = "mine" | "browse" | "dashboard" | "admin";

// 히스토리 엔트리 한 칸 = 뷰 + 전체 큐 필터 한 벌(뒤로/앞으로 가기로 복원되는 범위).
interface NavState {
  view: View;
  filter: Filter;
  country: string;
  industry: string;
  listed: "" | Listed;
  market: string;
  region: string;
}
const PAGE = 50;

// 시장 보드 검색용 한글 별칭 — BE 어휘엔 라벨/별칭이 없어 FE 가 표기만 보강한다.
const MARKET_ALIASES: Record<string, string[]> = {
  KOSPI: ["코스피"],
  KOSDAQ: ["코스닥"],
  KONEX: ["코넥스"],
  NASDAQ: ["나스닥"],
  NYSE: ["뉴욕증권거래소"],
};

// 시장 보드 어휘 → 픽커 옵션. 라벨은 큐 테이블 표기(item.market 원문)와 동일한 보드 코드.
function toMarketOpts(markets: string[]): PickerOption[] {
  return markets.map((m) => ({ value: m, label: m, aliases: MARKET_ALIASES[m] }));
}

// 폴백 어휘 — /queue/filters 가 markets 를 아직 안 주면(BE 계약 확장 전) 대표 보드로 표시.
// 별칭 맵과 단일 소스(키 = 보드 코드) — 한쪽만 갱신돼 별칭이 새는 사고 방지(리뷰 반영).
const FALLBACK_MARKETS = Object.keys(MARKET_ALIASES);

const FILTERS: { value: Filter; label: string }[] = [
  { value: "", label: "전체" },
  { value: "pending", label: "대기" },
  { value: "confirmed", label: "확정" },
  { value: "rejected", label: "거부" },
];

export default function App() {
  const [user, setUser] = useState<string | null>(getUser());
  const [role, setRole] = useState<Role | null>(getRole());

  // 어떤 요청이든 401 이면 로그인 화면으로 되돌린다(세션 만료·토큰 무효).
  useEffect(() => {
    const reset = () => {
      setUser(null);
      setRole(null);
    };
    setAuthErrorHandler(reset);
    return () => setAuthErrorHandler(null);
  }, []);

  const onLogin = (who: string, r: Role) => {
    setUser(who);
    setRole(r);
  };
  const onLogout = () => {
    setUser(null);
    setRole(null);
  };

  return (
    <>
      {/* 전역 토스트 — 앱 다크 토큰(panel/line/ink)에 맞춰 기본 스타일 오버라이드. */}
      <Toaster
        theme="dark"
        toastOptions={{
          style: {
            background: "var(--color-panel)",
            border: "1px solid var(--color-line)",
            color: "var(--color-ink)",
          },
        }}
      />
      {user ? (
        <Workbench user={user} role={role ?? "worker"} onLogout={onLogout} />
      ) : (
        <Login onLogin={onLogin} />
      )}
    </>
  );
}

function Workbench({
  user,
  role,
  onLogout,
}: {
  user: string;
  role: Role;
  onLogout: () => void;
}) {
  const isAdmin = role === "admin";
  // 새로고침(F5) 시 탭이 유지되도록 localStorage 에 저장. 관리자 아니면 admin 탭 무시.
  // 저장된 뷰가 없으면(첫 방문) 역할별 착지 — admin 은 관리 전담이라 콘솔로, 그 외는 내 작업.
  const [view, setViewState] = useState<View>(() => {
    const saved = localStorage.getItem("wb.view") as View | null;
    // 전체 큐는 admin 전용 뷰 — worker 는 저장값이 남아 있어도(권한 강등·계정 교대) 내 작업으로.
    if (saved === "browse") return isAdmin ? "browse" : "mine";
    // 대시보드도 관제 화면이라 admin 전용(BE 는 로그인만 요구하지만 worker 업무와 무관).
    if (saved === "dashboard") return isAdmin ? "dashboard" : "mine";
    if (saved === "admin" && isAdmin) return "admin";
    if (saved === "mine") return "mine";
    return isAdmin ? "admin" : "mine";
  });
  const [filter, setFilter] = useState<Filter>("pending");
  // 전체 큐 국가·업종 필터 — total 이 이 조건 반영분으로 내려와 '해당 건수'를 그대로 보여준다.
  const [country, setCountry] = useState("");
  const [industry, setIndustry] = useState("");
  const [listed, setListed] = useState<"" | Listed>("");
  const [market, setMarket] = useState("");
  // 지역(KR 시/도) — 국가에 KR 이 있을 때만 노출·전송(#243). 숨김 중엔 선택값을 유지해
  // KR 재선택 시 복원하고, 전송만 비운다(크롤 실행 섹션의 지역 팬아웃과 같은 패턴).
  const [region, setRegion] = useState("");
  // 국가·업종·시장·지역 셀렉트 옵션 — worker 접근 가능한 경로로 한 번 로드(실패해도 큐 조회는 가능).
  const { countryOpts, industryOpts, markets, regionOpts } = useQueueFilterOpts();
  // 시장 어휘는 BE 계약 확장 대기 — 내려올 때만 폴백을 실측 목록으로 교체.
  const marketOpts = toMarketOpts(markets.length ? markets : FALLBACK_MARKETS);
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  // 이번 세션 처리(확정+거부) 건수 — 모달 하단 진행률 바의 분자. 필터 바꾸면(작업 대상이
  // 바뀌면) 0 으로 리셋. 페이지 이동은 같은 세션이라 유지.
  const [sessionDone, setSessionDone] = useState(0);
  // 요청 시퀀스 — 늦게 도착한 옛 응답이 현재 화면을 덮어쓰지 않게 한다(필터 연타 레이스).
  const reqRef = useRef(0);

  // 뷰 전환을 브라우저 히스토리에 얕게 싣는다 — 대시보드에서 숫자를 눌러 큐로 되짚은 뒤
  // 뒤로 가기로 대시보드에 돌아오기 위한 것(SPA 라 라우터 없이 pushState 로만 처리).
  // 담는 건 뷰와 전체 큐 필터 한 벌뿐 — 페이지(offset)는 복원 시 1페이지로 되돌린다.
  const applyNav = useCallback(
    (st: NavState) => {
      // 계정이 바뀌어(로그아웃 후 worker 로그인) 권한 없는 뷰가 히스토리에 남아 있을 수 있다.
      const v: View = !isAdmin && st.view !== "mine" ? "mine" : st.view;
      localStorage.setItem("wb.view", v);
      setViewState(v);
      setFilter(st.filter);
      setCountry(st.country);
      setIndustry(st.industry);
      setListed(st.listed);
      setMarket(st.market);
      setRegion(st.region);
      setSessionDone(0);
    },
    [isAdmin],
  );

  // 상태를 바꾸면서 히스토리 엔트리를 하나 쌓는다. patch 에 없는 축은 현재 값 그대로.
  const pushNav = (patch: Partial<NavState>) => {
    const st: NavState = { view, filter, country, industry, listed, market, region, ...patch };
    applyNav(st);
    history.pushState({ wb: st }, "");
  };

  const setView = (v: View) => pushNav({ view: v });

  // 현재 엔트리를 항상 화면과 같은 값으로 유지한다. 첫 엔트리에 뷰를 심는 역할(안 심으면
  // 되짚기 후 뒤로 가기가 state 없는 엔트리로 떨어져 화면이 멈춘 것처럼 보인다)과, 툴바에서
  // 직접 바꾼 필터(엔트리를 쌓지 않는다 — 클릭마다 쌓으면 히스토리가 필터 로그가 된다)를
  // 현재 엔트리에 반영하는 역할을 겸한다. pushNav·popstate 직후엔 같은 값을 덮어써 무해.
  useEffect(() => {
    const here: NavState = { view, filter, country, industry, listed, market, region };
    history.replaceState({ wb: here }, "");
  }, [view, filter, country, industry, listed, market, region]);

  useEffect(() => {
    const onPop = (e: PopStateEvent) => {
      const st = (e.state as { wb?: NavState } | null)?.wb;
      // 우리가 심지 않은 엔트리(앱 진입 이전)는 건드리지 않는다 — 브라우저가 이탈 처리.
      if (!st) return;
      applyNav(st);
      setOffset(0);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [applyNav]);

  const load = useCallback(async () => {
    const myReq = ++reqRef.current;
    setLoading(true);
    setError(null);
    try {
      const res = await fetchQueue({
        status: filter,
        limit: PAGE,
        offset,
        filter: { country, industry, listed, market, region: krInScope(country) ? region : "" },
      });
      if (myReq !== reqRef.current) return; // 더 새 요청이 진행 중 — 결과 폐기
      // 마지막 페이지의 마지막 항목을 처리해 페이지가 비면 한 페이지 앞으로 보정.
      if (res.items.length === 0 && offset > 0) {
        setOffset((o) => Math.max(0, o - PAGE));
        return;
      }
      setItems(res.items);
      setTotal(res.total);
    } catch (e) {
      if (myReq !== reqRef.current) return;
      setError(errMsg(e));
    } finally {
      if (myReq === reqRef.current) setLoading(false);
    }
  }, [filter, offset, country, industry, listed, market, region]);

  useEffect(() => {
    // 전체 큐 탭에 있을 때만 조회 — 탭 진입·복귀 시 재조회해 내 작업에서 처리한
    // 확정/거부가 낡은 스냅샷으로 남지 않게 한다(마운트 1회 조회의 stale 문제).
    if (view === "browse") void load();
  }, [load, view]);

  // 성공(처리 완료)이면 true — 팝업의 '성공 시에만 다음 행 전진' 판단에 쓰인다.
  const act = async (
    id: string,
    kind: "confirm" | "reject",
    edits: ConfirmEdits = {},
  ): Promise<boolean> => {
    setBusyIds((prev) => new Set(prev).add(id));
    setError(null);
    let ok = false;
    try {
      // 검증 담당자(assignee)는 서버가 로그인 사용자로 자동 기록. 확정 시 사람이 고른
      // 이메일·사이트·문의폼·메모·담당자·첨부 유무 교정분과 삭제할 이메일을 함께 보낸다.
      const updated =
        kind === "confirm" ? await confirmReview(id, edits) : await rejectReview(id);
      // 현재 필터에서 벗어난 항목은 목록에서 빠지므로 재조회, 아니면 제자리 갱신.
      if (filter && updated.status !== filter) {
        await load();
      } else {
        setItems((prev) => prev.map((it) => (it.id === id ? updated : it)));
      }
      setSessionDone((n) => n + 1);
      ok = true;
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusyIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
    return ok;
  };

  const doLogout = async () => {
    await logout();
    onLogout();
  };

  const changeFilter = (f: Filter) => {
    setFilter(f);
    setOffset(0);
    setSessionDone(0);
  };

  // 대시보드 숫자 → 전체 큐 되짚기(#378). 지정 안 한 축은 비워 "그 숫자가 곧 이 목록"이
  // 되게 한다 — 이전 화면에 남아 있던 필터가 섞이면 건수가 안 맞아 되짚기가 거짓말이 된다.
  const jumpToQueue = (jump: QueueJump) => {
    setOffset(0);
    // 축을 개별 setter 로 먼저 바꾸면 안 된다 — pushNav 는 현 렌더의 값으로 NavState 를
    // 만들므로 방금 세팅한 값이 옛 값으로 덮인다. 되짚기 한 벌을 통째로 넘긴다.
    pushNav({
      view: "browse",
      filter: jump.status ?? "",
      country: jump.country ?? "",
      industry: jump.industry ?? "",
      listed: "",
      market: "",
      region: "",
    });
  };

  const page = Math.floor(offset / PAGE) + 1;
  const pages = Math.max(1, Math.ceil(total / PAGE));

  // 큐 화면 JSX 를 상수로 둔다(별도 컴포넌트로 만들면 매 렌더 리마운트되어 QueueTable
  // 내부 선택 상태가 사라지므로 인라인 element 로 유지).
  const queueView = (
    <>
      {/* 상태 탭 + 국가·업종·상장·시장 필터(팝오버) + 새로고침 — 툴바 한 줄. 필터 선택 시
          total(총 N건)이 해당 조건 건수로 바뀌고, 트리거에 선택 요약이 상시 표시된다. */}
      <div className="flex items-center gap-4 mb-4 flex-wrap">
        <div className="flex gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.value}
              className={tabCls(filter === f.value)}
              onClick={() => changeFilter(f.value)}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1.5 flex-wrap">
          <FilterPopover
            label="국가"
            summary={pickSummary(country, countryOpts)}
            active={country !== ""}
          >
            <MultiPicker
              options={countryOpts}
              value={country}
              onChange={(csv) => {
                setCountry(csv);
                setOffset(0);
              }}
              placeholder="국가 검색 (예: 미국, US, 일본)"
              emptyHint="전체 국가"
            />
          </FilterPopover>
          <FilterPopover
            label="업종"
            summary={pickSummary(industry, industryOpts)}
            active={industry !== ""}
          >
            <MultiPicker
              options={industryOpts}
              value={industry}
              onChange={(csv) => {
                setIndustry(csv);
                setOffset(0);
              }}
              placeholder="업종 검색 (예: 반도체, 미분류)"
              emptyHint="전체 업종"
            />
          </FilterPopover>
          {/* 지역(KR 시/도) — 국가에 KR 을 선택했을 때만 노출(#243). */}
          {krInScope(country) && (
            <FilterPopover
              label="지역"
              summary={pickSummary(region, regionOpts)}
              active={region !== ""}
            >
              <MultiPicker
                options={regionOpts}
                value={region}
                onChange={(csv) => {
                  setRegion(csv);
                  setOffset(0);
                }}
                placeholder="지역 검색 (예: 서울, 경기)"
                emptyHint="전체 지역"
              />
            </FilterPopover>
          )}
          <label className="flex items-center gap-1.5 text-muted text-[13px]">
            상장여부
            <select
              className={INPUT}
              value={listed}
              onChange={(e) => {
                setListed(e.target.value as "" | Listed);
                setOffset(0);
              }}
            >
              {LISTED_FILTER_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          {/* 시장 보드(KOSPI/KOSDAQ…) — 상장여부와 독립 조건(AND). 비상장+시장 조합은
              모순이라 0건이 되지만, 트리거 요약이 둘 다 강조돼 원인이 화면에 보인다. */}
          <FilterPopover
            label="시장"
            summary={pickSummary(market, marketOpts)}
            active={market !== ""}
          >
            <MultiPicker
              options={marketOpts}
              value={market}
              onChange={(csv) => {
                setMarket(csv);
                setOffset(0);
              }}
              placeholder="시장 검색 (예: 코스피, KOSDAQ)"
              emptyHint="전체 시장"
            />
          </FilterPopover>
        </div>
        <button className={BTN} onClick={() => void load()} disabled={loading}>
          새로고침
        </button>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}

      <p className="text-muted my-2">
        총 {total}건 {filter && `(${FILTERS.find((f) => f.value === filter)?.label})`}
        {loading && " · 불러오는 중…"}
      </p>

      {loading && items.length === 0 ? (
        <TableSkeleton />
      ) : (
        <QueueTable
          items={items}
          busyIds={busyIds}
          doneCount={sessionDone}
          remaining={total}
          // 전체 큐는 admin 전용 뷰(탭 자체가 admin 에게만 노출) — worker 의 직접 처리는
          // claim(내 작업) 경유만이라 미점유 항목 동시 중복 검토가 원천 차단된다.
          onConfirm={(id, edits) => act(id, "confirm", edits)}
          onReject={(id) => act(id, "reject")}
          // 전체큐는 점유 항목이 서버에서 제외됨 — pending 0 = "받아갈 수 있는 작업 없음".
          emptyText={
            filter === "pending" ? "받아갈 수 있는 작업이 없습니다 — 모두 처리되었거나 다른 직원이 작업 중입니다." : undefined
          }
        />
      )}

      <div className="flex items-center gap-4 justify-center mt-[18px] text-muted">
        <button
          className={BTN}
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - PAGE))}
        >
          <span className="inline-flex items-center gap-1">
            <ChevronLeft size={14} aria-hidden /> 이전
          </span>
        </button>
        <span className="tabular-nums">
          {page} / {pages}
        </span>
        <button
          className={BTN}
          disabled={offset + PAGE >= total}
          onClick={() => setOffset(offset + PAGE)}
        >
          <span className="inline-flex items-center gap-1">
            다음 <ChevronRight size={14} aria-hidden />
          </span>
        </button>
      </div>
    </>
  );

  return (
    <div className="mx-auto max-w-[1680px] p-6">
      <header className="flex items-center justify-between mb-5">
        <h1 className="text-xl font-semibold tracking-tight m-0">검증 워크벤치</h1>
        <div className="flex items-center gap-2.5 text-muted">
          {/* 상단 nav 는 admin 전용 — worker 는 뷰가 내 작업 하나뿐이라 탭 하나짜리 nav 는
              죽은 UI(항상 활성·클릭 무의미)다. 전체 큐는 타인 작업까지 노출되는 admin 관제용이고,
              worker 의 처리 내역은 내 작업 안의 상태 탭(대기/확정/거부)에서 본다. 작업 뷰와
              관리자 콘솔의 위계는 구분선·기어 아이콘으로 표시("플로어를 떠나 콘솔로 간다"). */}
          {isAdmin && (
            <nav className="flex items-center gap-1 mr-2">
              <button className={tabCls(view === "mine")} onClick={() => setView("mine")}>
                내 작업
              </button>
              <button className={tabCls(view === "browse")} onClick={() => setView("browse")}>
                전체 큐
              </button>
              <span className="w-px h-5 bg-line mx-1.5" aria-hidden />
              {/* 대시보드·관리자는 구분선 오른쪽 — 큐를 처리하는 작업 뷰가 아니라 전사
                  현황을 보고 운영을 조작하는 관제 자리다. */}
              <button className={tabCls(view === "dashboard")} onClick={() => setView("dashboard")}>
                <span className="inline-flex items-center gap-1">
                  <BarChart3 size={14} aria-hidden /> 대시보드
                </span>
              </button>
              <button className={tabCls(view === "admin")} onClick={() => setView("admin")}>
                <span className="inline-flex items-center gap-1">
                  <Settings size={14} aria-hidden /> 관리자
                </span>
              </button>
            </nav>
          )}
          <span className="text-muted">
            {user}
            {isAdmin && " · 관리자"}
          </span>
          <button className={BTN} onClick={() => void doLogout()}>
            로그아웃
          </button>
        </div>
      </header>

      {/* 대시보드는 진입할 때마다 마운트돼 1회 조회한다(탭 전환=새 스냅샷). 숨김 유지가
          아닌 이유: 집계가 무거워 폴링이 금지된 API라, 낡은 값을 계속 보여주느니 진입
          시점에 새로 뜨는 편이 맞다. */}
      {view === "admin" && isAdmin ? (
        <Admin />
      ) : view === "dashboard" && isAdmin ? (
        <Dashboard onJumpToQueue={jumpToQueue} />
      ) : view === "browse" ? (
        queueView
      ) : (
        <MyWork />
      )}
    </div>
  );
}
