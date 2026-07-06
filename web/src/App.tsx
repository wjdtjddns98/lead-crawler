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
import { LISTED_FILTER_OPTIONS, useQueueFilterOpts } from "./filterOptions";
import { Admin } from "./components/Admin";
import { MyWork } from "./components/MyWork";
import { FilterPopover, pickSummary } from "./components/FilterPopover";
import { MultiPicker, type PickerOption } from "./components/MultiPicker";
import { QueueTable } from "./components/QueueTable";
import { TableSkeleton } from "./components/TableSkeleton";
import { Login } from "./components/Login";
import { ChevronLeft, ChevronRight, Settings } from "lucide-react";
import { BTN, INPUT, tabCls } from "./ui";
import { Toaster } from "sonner";
import { ErrorBox } from "./components/ErrorBox";
import type { Listed, ReviewItem, ReviewStatus, Role } from "./types";

type Filter = ReviewStatus | "";
type View = "mine" | "browse" | "admin";
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
    if (saved === "browse") return "browse";
    if (saved === "admin" && isAdmin) return "admin";
    if (saved === "mine") return "mine";
    return isAdmin ? "admin" : "mine";
  });
  const setView = (v: View) => {
    localStorage.setItem("wb.view", v);
    setViewState(v);
  };
  const [filter, setFilter] = useState<Filter>("pending");
  // 전체 큐 국가·업종 필터 — total 이 이 조건 반영분으로 내려와 '해당 건수'를 그대로 보여준다.
  const [country, setCountry] = useState("");
  const [industry, setIndustry] = useState("");
  const [listed, setListed] = useState<"" | Listed>("");
  const [market, setMarket] = useState("");
  // 국가·업종·시장 셀렉트 옵션 — worker 접근 가능한 경로로 한 번 로드(실패해도 큐 조회는 가능).
  const { countryOpts, industryOpts, markets } = useQueueFilterOpts();
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

  const load = useCallback(async () => {
    const myReq = ++reqRef.current;
    setLoading(true);
    setError(null);
    try {
      const res = await fetchQueue({
        status: filter,
        limit: PAGE,
        offset,
        filter: { country, industry, listed, market },
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
  }, [filter, offset, country, industry, listed, market]);

  useEffect(() => {
    void load();
  }, [load]);

  // 성공(처리 완료)이면 true — 팝업의 '성공 시에만 다음 행 전진' 판단에 쓰인다.
  const act = async (
    id: string,
    kind: "confirm" | "reject",
    selected?: string,
  ): Promise<boolean> => {
    setBusyIds((prev) => new Set(prev).add(id));
    setError(null);
    let ok = false;
    try {
      // 담당자는 서버가 로그인 사용자로 자동 기록. 확정 시 사람이 고른 이메일을 보낸다.
      const updated =
        kind === "confirm" ? await confirmReview(id, selected) : await rejectReview(id);
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
          // 전체 큐 액션은 admin 전용 — worker 는 읽기 전용(진행 확인·검색용). 여기 목록은
          // 전부 미점유 항목이라 worker 가 직접 처리하면 claim(내 작업) 격리를 우회해
          // 동시 중복 검토가 나므로, 직접 처리는 '내 작업' 경유만 허용한다.
          readOnly={!isAdmin}
          onConfirm={(id, selected) => act(id, "confirm", selected)}
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
    <div className="mx-auto max-w-[1400px] p-6">
      <header className="flex items-center justify-between mb-5">
        <h1 className="text-xl font-semibold tracking-tight m-0">검증 워크벤치</h1>
        <div className="flex items-center gap-2.5 text-muted">
          {/* 작업 뷰(내 작업·전체 큐)와 관리자 콘솔은 위계가 다르다 — 구분선·기어 아이콘으로
              "플로어를 떠나 콘솔로 간다"를 표시. worker 에겐 구분선째 안 보인다. */}
          <nav className="flex items-center gap-1 mr-2">
            <button className={tabCls(view === "mine")} onClick={() => setView("mine")}>
              내 작업
            </button>
            <button className={tabCls(view === "browse")} onClick={() => setView("browse")}>
              전체 큐
            </button>
            {isAdmin && (
              <>
                <span className="w-px h-5 bg-line mx-1.5" aria-hidden />
                <button className={tabCls(view === "admin")} onClick={() => setView("admin")}>
                  <span className="inline-flex items-center gap-1">
                    <Settings size={14} aria-hidden /> 관리자
                  </span>
                </button>
              </>
            )}
          </nav>
          <span className="text-muted">
            {user}
            {isAdmin && " · 관리자"}
          </span>
          <button className={BTN} onClick={() => void doLogout()}>
            로그아웃
          </button>
        </div>
      </header>

      {view === "admin" && isAdmin ? <Admin /> : view === "browse" ? queueView : <MyWork />}
    </div>
  );
}
