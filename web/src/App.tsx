import { useCallback, useEffect, useRef, useState } from "react";
import {
  confirmReview,
  fetchQueue,
  fetchQueueFilters,
  getRole,
  getUser,
  logout,
  rejectReview,
  setAuthErrorHandler,
  withUnclassified,
} from "./api";
import { Admin } from "./components/Admin";
import { MyWork } from "./components/MyWork";
import { MultiPicker, type PickerOption } from "./components/MultiPicker";
import { QueueTable } from "./components/QueueTable";
import { TableSkeleton } from "./components/TableSkeleton";
import { Login } from "./components/Login";
import { ChevronLeft, ChevronRight, Settings } from "lucide-react";
import { BTN, tabCls } from "./ui";
import { ErrorBox } from "./components/ErrorBox";
import type { Listed, ReviewItem, ReviewStatus, Role } from "./types";

type Filter = ReviewStatus | "";
type View = "mine" | "browse" | "admin";
const PAGE = 50;

// 상장여부 필터 — 빈값=전체 + Listed 3값(MyWork 와 동일).
const LISTED_OPTIONS: { value: "" | Listed; label: string }[] = [
  { value: "", label: "전체" },
  { value: "listed", label: "상장" },
  { value: "unlisted", label: "비상장" },
  { value: "unknown", label: "미상" },
];

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

  if (!user) return <Login onLogin={onLogin} />;
  return <Workbench user={user} role={role ?? "worker"} onLogout={onLogout} />;
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
  const [countryOpts, setCountryOpts] = useState<PickerOption[]>([]);
  const [industryOpts, setIndustryOpts] = useState<PickerOption[]>([]);
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
        filter: { country, industry, listed },
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
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (myReq === reqRef.current) setLoading(false);
    }
  }, [filter, offset, country, industry, listed]);

  useEffect(() => {
    void load();
  }, [load]);

  // 전체 큐 국가·업종 셀렉트 옵션 — worker 접근 가능한 경로로 한 번 로드.
  useEffect(() => {
    fetchQueueFilters()
      .then((f) => {
        setCountryOpts(
          f.countries.map((c) => ({ value: c.iso2, label: c.label, code: c.iso2, aliases: c.aliases })),
        );
        // '미분류'(분류 실패 폴백값)도 조회 대상 — BE 옵션에 없을 때만 덧붙인다(#115 중복 방지).
        setIndustryOpts(
          withUnclassified(f.industries).map((i) => ({ value: i.value, label: i.label, aliases: i.aliases })),
        );
      })
      .catch(() => {
        // 옵션 로드 실패해도 큐 조회는 가능 — 픽커만 빈 채로 둔다.
      });
  }, []);

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
      setError(e instanceof Error ? e.message : String(e));
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
        <button className={BTN} onClick={() => void load()} disabled={loading}>
          새로고침
        </button>
      </div>

      {/* 국가·업종 필터 — 선택 시 total(총 N건)이 해당 조건 건수로 바뀐다. */}
      <div className="flex flex-wrap items-start gap-3 mb-4 p-3 border border-line rounded-md bg-[rgba(127,127,127,0.06)]">
        <div className="flex flex-col gap-1 text-muted text-[13px]">
          <span>국가 <span className="text-muted">(선택 안 함 = 전체)</span></span>
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
        </div>
        <div className="flex flex-col gap-1 text-muted text-[13px]">
          <span>업종 <span className="text-muted">(선택 안 함 = 전체)</span></span>
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
        </div>
        <label className="flex flex-col gap-1 text-muted text-[13px]">
          상장여부
          <select
            className="bg-canvas border border-line text-ink py-[7px] px-2.5 rounded-md min-w-[120px]"
            value={listed}
            onChange={(e) => {
              setListed(e.target.value as "" | Listed);
              setOffset(0);
            }}
          >
            {LISTED_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
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
    <div className="mx-auto max-w-[1200px] p-6">
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
