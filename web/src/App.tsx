import { useCallback, useEffect, useRef, useState } from "react";
import {
  confirmReview,
  exportConfirmed,
  fetchQueue,
  getUser,
  logout,
  rejectReview,
  setAuthErrorHandler,
} from "./api";
import { QueueTable } from "./components/QueueTable";
import { Login } from "./components/Login";
import type { ReviewItem, ReviewStatus } from "./types";

type Filter = ReviewStatus | "";
const PAGE = 50;

const FILTERS: { value: Filter; label: string }[] = [
  { value: "", label: "전체" },
  { value: "pending", label: "대기" },
  { value: "confirmed", label: "확정" },
  { value: "rejected", label: "거부" },
];

export default function App() {
  const [user, setUser] = useState<string | null>(getUser());

  // 어떤 요청이든 401 이면 로그인 화면으로 되돌린다(세션 만료·토큰 무효).
  useEffect(() => {
    setAuthErrorHandler(() => setUser(null));
    return () => setAuthErrorHandler(null);
  }, []);

  if (!user) return <Login onLogin={setUser} />;
  return <Workbench user={user} onLogout={() => setUser(null)} />;
}

function Workbench({ user, onLogout }: { user: string; onLogout: () => void }) {
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

  const act = async (id: string, kind: "confirm" | "reject", selected?: string) => {
    setBusyIds((prev) => new Set(prev).add(id));
    setError(null);
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
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
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

  return (
    <div className="app">
      <header>
        <h1>검증 워크벤치</h1>
        <div className="session">
          <span className="muted">{user}</span>
          <button className="btn export" onClick={() => void doExport()}>
            전체 확정분 엑셀 ↓
          </button>
          <button className="btn" onClick={() => void doLogout()}>
            로그아웃
          </button>
        </div>
      </header>

      <div className="toolbar">
        <div className="filters">
          {FILTERS.map((f) => (
            <button
              key={f.value}
              className={`tab ${filter === f.value ? "active" : ""}`}
              onClick={() => changeFilter(f.value)}
            >
              {f.label}
            </button>
          ))}
        </div>
        <button className="btn" onClick={() => void load()} disabled={loading}>
          새로고침
        </button>
      </div>

      {error && <div className="error">⚠ {error}</div>}

      <p className="count">
        총 {total}건 {filter && `(${FILTERS.find((f) => f.value === filter)?.label})`}
        {loading && " · 불러오는 중…"}
      </p>

      <QueueTable
        items={items}
        busyIds={busyIds}
        onConfirm={(id, selected) => void act(id, "confirm", selected)}
        onReject={(id) => void act(id, "reject")}
      />

      <div className="pager">
        <button
          className="btn"
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - PAGE))}
        >
          ← 이전
        </button>
        <span>
          {page} / {pages}
        </span>
        <button
          className="btn"
          disabled={offset + PAGE >= total}
          onClick={() => setOffset(offset + PAGE)}
        >
          다음 →
        </button>
      </div>
    </div>
  );
}
