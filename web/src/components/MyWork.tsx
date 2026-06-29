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

  // 성공(처리 완료)이면 true — 팝업의 '성공 시에만 다음 행 전진' 판단에 쓰인다.
  const act = async (id: string, kind: "confirm" | "reject", selected?: string): Promise<boolean> => {
    setBusyIds((p) => new Set(p).add(id));
    setError(null);
    let ok = false;
    try {
      if (kind === "confirm") await confirmReview(id, selected);
      else await rejectReview(id);
      ok = true;
    } catch (e) {
      // 409(타인 점유 중)·400(형식 오류) 등 — 메시지 표시. ok=false 라 전진하지 않는다.
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyIds((p) => {
        const n = new Set(p);
        n.delete(id);
        return n;
      });
      await refill(); // 처리(또는 충돌) 후 자동 리필 — 항상 배치 크기 유지.
    }
    return ok;
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
          onConfirm={(id, selected) => act(id, "confirm", selected)}
          onReject={(id) => act(id, "reject")}
        />
      )}
    </>
  );
}
