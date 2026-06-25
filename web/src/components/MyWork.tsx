import { useCallback, useEffect, useState } from "react";
import { claimWork, confirmReview, rejectReview, releaseWork } from "../api";
import { QueueTable } from "./QueueTable";
import { BTN, EMPTY, ERROR_BOX } from "../ui";
import type { ReviewItem } from "../types";

// 내 작업 뷰(당겨가기) — 내 작업분만 보여 6명 동시 검증 충돌을 막는다. 처리하면 자동 리필.
export function MyWork() {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());

  const refill = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setItems(await claimWork());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refill();
  }, [refill]);

  const act = async (id: string, kind: "confirm" | "reject", selected?: string) => {
    setBusyIds((p) => new Set(p).add(id));
    setError(null);
    try {
      if (kind === "confirm") await confirmReview(id, selected);
      else await rejectReview(id);
    } catch (e) {
      // 409(타인 점유 중) 등 — 메시지 표시 후 작업분 새로고침으로 그 항목을 비운다.
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyIds((p) => {
        const n = new Set(p);
        n.delete(id);
        return n;
      });
      await refill(); // 처리(또는 충돌) 후 자동 리필 — 항상 배치 크기 유지.
    }
  };

  const release = async () => {
    setError(null);
    try {
      await releaseWork();
      setItems([]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <>
      <div className="flex items-center gap-4 mb-4 flex-wrap">
        <p className="text-muted my-2">
          내 작업 {items.length}건{loading && " · 불러오는 중…"}
        </p>
        <div className="flex gap-1">
          <button className={BTN} onClick={() => void refill()} disabled={loading}>
            더 받기 / 새로고침
          </button>
          <button className={BTN} onClick={() => void release()} disabled={items.length === 0}>
            작업 종료(반납)
          </button>
        </div>
      </div>

      {error && <div className={ERROR_BOX}>⚠ {error}</div>}

      {items.length === 0 && !loading ? (
        <p className={EMPTY}>받을 작업분이 없습니다 — 큐가 비었거나 모두 처리되었습니다.</p>
      ) : (
        <QueueTable
          items={items}
          busyIds={busyIds}
          onConfirm={(id, selected) => void act(id, "confirm", selected)}
          onReject={(id) => void act(id, "reject")}
        />
      )}
    </>
  );
}
