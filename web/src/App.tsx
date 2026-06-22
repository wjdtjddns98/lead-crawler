import { useCallback, useEffect, useRef, useState } from "react";
import { confirmReview, exportUrl, fetchQueue, rejectReview } from "./api";
import { QueueTable } from "./components/QueueTable";
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
  const [filter, setFilter] = useState<Filter>("pending");
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [assignee, setAssignee] = useState("");
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

  const act = async (id: string, kind: "confirm" | "reject") => {
    setBusyIds((prev) => new Set(prev).add(id));
    setError(null);
    try {
      const who = assignee.trim() || undefined;
      const updated =
        kind === "confirm" ? await confirmReview(id, who) : await rejectReview(id, who);
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
        <a className="btn export" href={exportUrl()} title="필터와 무관하게 전체 확정분을 내보냅니다">
          전체 확정분 엑셀 ↓
        </a>
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
        <label className="assignee-input">
          담당자
          <input
            value={assignee}
            onChange={(e) => setAssignee(e.target.value)}
            placeholder="(선택) 확정/거부 기록"
          />
        </label>
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
        onConfirm={(id) => void act(id, "confirm")}
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
