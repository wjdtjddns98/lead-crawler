import { useCallback, useEffect, useRef, useState } from "react";
import {
  claimWork,
  confirmReview,
  fetchQueue,
  fetchQueueFilters,
  rejectReview,
  releaseWork,
} from "../api";
import { QueueTable } from "./QueueTable";
import { MultiPicker, type PickerOption } from "./MultiPicker";
import { BTN, EMPTY, ERROR_BOX } from "../ui";
import type { ClaimFilter, Listed, ReviewItem } from "../types";

const FILTER_KEY = "lc_claim_filter";
const EMPTY_FILTER: ClaimFilter = { country: "", industry: "", listed: "" };

// 상장여부 필터 — 빈값=전체 + Listed 3값(크롤타깃과 달리 "전체"는 ""이고 unknown 은 별개 상태).
const LISTED_OPTIONS: { value: "" | Listed; label: string }[] = [
  { value: "", label: "전체" },
  { value: "listed", label: "상장" },
  { value: "unlisted", label: "비상장" },
  { value: "unknown", label: "미상" },
];

const FIELD = "flex flex-col gap-1 text-muted text-[13px]";
const INPUT = "bg-canvas border border-line text-ink py-[7px] px-2.5 rounded-md min-w-[120px]";

const LISTED_VALUES = new Set<string>(["listed", "unlisted", "unknown"]);

// localStorage 에서 세션 필터 복원(손상/구버전 값은 무시). listed 화이트리스트 외 값은 전체("")로
// 강등 — 빈 셀렉트 렌더·BE 422 방지.
function loadFilter(): ClaimFilter {
  try {
    const raw = localStorage.getItem(FILTER_KEY);
    if (raw) {
      const p = JSON.parse(raw) as Partial<ClaimFilter>;
      return {
        country: p.country ?? "",
        industry: p.industry ?? "",
        listed: (LISTED_VALUES.has(String(p.listed)) ? p.listed : "") as "" | Listed,
      };
    }
  } catch {
    // 파싱 실패 — 전체로 시작.
  }
  return EMPTY_FILTER;
}

// 내 작업 뷰(당겨가기) — 내 작업분만 보여 6명 동시 검증 충돌을 막는다. 처리하면 자동 리필.
// 상단 작업범위 바에서 국가·업종·상장을 골라 두면 그 조건의 pending 만 당겨온다(관리자 구두지시 범위).
export function MyWork() {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<ClaimFilter>(loadFilter);
  const [countryOpts, setCountryOpts] = useState<PickerOption[]>([]);
  const [industryOpts, setIndustryOpts] = useState<PickerOption[]>([]);
  const [remaining, setRemaining] = useState<number | null>(null);
  const reqRef = useRef(0); // 최신 refill 토큰 — 빠른 필터 변경 시 뒤늦은 응답을 폐기(경쟁 방지).

  // 현재 필터로 작업분을 채우고 "이 범위 잔여 pending" 카운트를 갱신한다.
  const refill = useCallback(async (f: ClaimFilter) => {
    const token = ++reqRef.current;
    setLoading(true);
    setError(null);
    try {
      const claimed = await claimWork(f);
      // limit=1 — 카운트(total)만 필요. total 은 필터 반영분.
      const q = await fetchQueue({ status: "pending", limit: 1, offset: 0, filter: f });
      if (token !== reqRef.current) return; // 더 최신 요청이 진행 중 — 결과 버림.
      setItems(claimed);
      setRemaining(q.total);
    } catch (e) {
      if (token === reqRef.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (token === reqRef.current) setLoading(false);
    }
  }, []);

  // 최초: 필터 옵션 로드 + 저장된 필터로 첫 당겨가기.
  useEffect(() => {
    fetchQueueFilters()
      .then((f) => {
        setCountryOpts(
          f.countries.map((c) => ({ value: c.iso2, label: c.label, code: c.iso2, aliases: c.aliases })),
        );
        setIndustryOpts(f.industries.map((i) => ({ value: i.value, label: i.label, aliases: i.aliases })));
      })
      .catch(() => {
        // 옵션 로드 실패해도 작업 자체는 가능 — 무시(셀렉트만 빈 채로).
      });
    void refill(filter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 필터 변경 — 저장 후 현재 점유분을 전부 반납하고 새 범위로 재클레임(화면엔 항상 현재 범위만).
  const applyFilter = async (next: ClaimFilter) => {
    setFilter(next);
    localStorage.setItem(FILTER_KEY, JSON.stringify(next));
    try {
      await releaseWork();
    } catch {
      // 반납 실패해도 재클레임은 시도(claim 진입 시 비매칭은 어차피 안 내려옴).
    }
    await refill(next);
  };

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
      await refill(filter); // 처리(또는 충돌) 후 자동 리필 — 항상 배치 크기 유지.
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

  const hasFilter = Boolean(filter.country || filter.industry || filter.listed);

  return (
    <>
      <div className="flex flex-wrap items-end gap-3 mb-4 p-3 border border-line rounded-md bg-[rgba(127,127,127,0.06)]">
        <div className={FIELD}>
          <span>국가 <span className="text-muted">(선택 안 함=전체)</span></span>
          <MultiPicker
            options={countryOpts}
            value={filter.country}
            onChange={(csv) => void applyFilter({ ...filter, country: csv })}
            placeholder="국가 검색 (예: 미국, US)"
            emptyHint="전체 국가"
          />
        </div>
        <div className={FIELD}>
          <span>업종 <span className="text-muted">(선택 안 함=전체)</span></span>
          <MultiPicker
            options={industryOpts}
            value={filter.industry}
            onChange={(csv) => void applyFilter({ ...filter, industry: csv })}
            placeholder="업종 검색 (예: 금융)"
            emptyHint="전체 업종"
          />
        </div>
        <label className={FIELD}>
          상장여부
          <select
            className={INPUT}
            value={filter.listed}
            onChange={(e) => void applyFilter({ ...filter, listed: e.target.value as "" | Listed })}
          >
            {LISTED_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        {remaining !== null && (
          <span className="text-muted text-[13px] pb-1.5">
            현재 범위 잔여 <strong className="text-ink">{remaining}</strong>건
          </span>
        )}
      </div>

      <div className="flex items-center gap-4 mb-4 flex-wrap">
        <p className="text-muted my-2">
          내 작업 {items.length}건{loading && " · 불러오는 중…"}
        </p>
        <div className="flex gap-1">
          <button className={BTN} onClick={() => void refill(filter)} disabled={loading}>
            더 받기 / 새로고침
          </button>
          <button className={BTN} onClick={() => void release()} disabled={items.length === 0}>
            작업 종료(반납)
          </button>
        </div>
      </div>

      {error && <div className={ERROR_BOX}>⚠ {error}</div>}

      {items.length === 0 && !loading ? (
        <p className={EMPTY}>
          {hasFilter
            ? "이 범위에 남은 작업이 없습니다 — 필터를 넓히거나 해제해 보세요."
            : "받을 작업분이 없습니다 — 큐가 비었거나 모두 처리되었습니다."}
        </p>
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
