import { useCallback, useEffect, useRef, useState } from "react";
import {
  confirmReview,
  exportConfirmed,
  fetchQueue,
  getRole,
  getUser,
  logout,
  rejectReview,
  setAuthErrorHandler,
} from "./api";
import { Admin } from "./components/Admin";
import { MyWork } from "./components/MyWork";
import { QueueTable } from "./components/QueueTable";
import { TableSkeleton } from "./components/TableSkeleton";
import { Login } from "./components/Login";
import { ChevronLeft, ChevronRight, Download } from "lucide-react";
import { BTN, BTN_EXPORT, tabCls } from "./ui";
import { ErrorBox } from "./components/ErrorBox";
import type { ReviewItem, ReviewStatus, Role } from "./types";

type Filter = ReviewStatus | "";
type View = "mine" | "browse" | "admin";
const PAGE = 50;

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
  const [view, setView] = useState<View>("mine");
  const [filter, setFilter] = useState<Filter>("pending");
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  // 요청 시퀀스 — 늦게 도착한 옛 응답이 현재 화면을 덮어쓰지 않게 한다(필터 연타 레이스).
  const reqRef = useRef(0);

  const load = useCallback(async () => {
    const myReq = ++reqRef.current;
    setLoading(true);
    setError(null);
    try {
      const res = await fetchQueue({ status: filter, limit: PAGE, offset });
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
  }, [filter, offset]);

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

  const doExport = async () => {
    setError(null);
    try {
      await exportConfirmed();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const doLogout = async () => {
    await logout();
    onLogout();
  };

  const changeFilter = (f: Filter) => {
    setFilter(f);
    setOffset(0);
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
          onConfirm={(id, selected) => act(id, "confirm", selected)}
          onReject={(id) => act(id, "reject")}
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
        <h1 className="text-[22px] m-0">검증 워크벤치</h1>
        <div className="flex items-center gap-2.5 text-muted">
          <nav className="flex gap-1 mr-2">
            <button className={tabCls(view === "mine")} onClick={() => setView("mine")}>
              내 작업
            </button>
            <button className={tabCls(view === "browse")} onClick={() => setView("browse")}>
              전체 큐
            </button>
            {isAdmin && (
              <button className={tabCls(view === "admin")} onClick={() => setView("admin")}>
                관리자
              </button>
            )}
          </nav>
          <span className="text-muted">
            {user}
            {isAdmin && " · 관리자"}
          </span>
          {isAdmin && (
            <button className={BTN_EXPORT} onClick={() => void doExport()}>
              <span className="inline-flex items-center gap-1">
                전체 확정분 엑셀 <Download size={14} aria-hidden />
              </span>
            </button>
          )}
          <button className={BTN} onClick={() => void doLogout()}>
            로그아웃
          </button>
        </div>
      </header>

      {view === "admin" && isAdmin ? <Admin /> : view === "browse" ? queueView : <MyWork />}
    </div>
  );
}
