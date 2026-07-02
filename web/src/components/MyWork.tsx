import { useCallback, useEffect, useRef, useState } from "react";
import {
  claimWork,
  confirmReview,
  fetchMyWork,
  fetchQueue,
  fetchQueueFilters,
  rejectReview,
  withUnclassified,
} from "../api";
import { QueueTable } from "./QueueTable";
import { TableSkeleton } from "./TableSkeleton";
import { MultiPicker, type PickerOption } from "./MultiPicker";
import { BTN, EMPTY } from "../ui";
import { ErrorBox } from "./ErrorBox";
import type { ClaimFilter, Listed, ReviewItem } from "../types";

const FILTER_KEY = "lc_claim_filter";
const EMPTY_FILTER: ClaimFilter = { country: "", industry: "", listed: "" };
// 한 계정 동시 점유 총량 상한 — BE review_claim_cap 과 동일값(계약: PRD-queue-claim-permanent §4.2).
const CLAIM_CAP = 100;

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
        // 손상값 방어 — string 아니면 전체(""). MultiPicker 가 value.split() 하므로 number 등은 throw.
        country: typeof p.country === "string" ? p.country : "",
        industry: typeof p.industry === "string" ? p.industry : "",
        listed: (LISTED_VALUES.has(String(p.listed)) ? p.listed : "") as "" | Listed,
      };
    }
  } catch {
    // 파싱 실패 — 전체로 시작.
  }
  return EMPTY_FILTER;
}

// 내 작업 뷰(영구 배정) — 받아간 항목은 확정/거부 전까지 내 계정 귀속(반납·TTL 없음).
// '작업 받기' 클릭 1회 = 상단 작업범위 조건으로 +30개 추가 배정(총량 100 상한, 선취 가능).
// 목록 복원·처리 후 갱신은 부작용 없는 GET /queue/mine 으로만 한다(claim 은 버튼 클릭 전용).
export function MyWork() {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<ClaimFilter>(loadFilter);
  const [countryOpts, setCountryOpts] = useState<PickerOption[]>([]);
  const [industryOpts, setIndustryOpts] = useState<PickerOption[]>([]);
  const [remaining, setRemaining] = useState<number | null>(null);
  // 이번 세션 처리(확정+거부) 건수 — 모달 하단 진행률 바의 분자. '작업 받기'로 새 배치를
  // 받으면 0 으로 리셋.
  const [sessionDone, setSessionDone] = useState(0);
  const reqRef = useRef(0); // 최신 요청 토큰 — 버튼 연타 시 뒤늦은 응답을 폐기(경쟁 방지).
  // refresh 가 useCallback([]) 안에서 최신 필터로 잔여 카운트를 조회하도록 ref 로 동행.
  const filterRef = useRef(filter);

  // 내 점유 목록(items)과 결과(mine 또는 claim 응답 — 둘 다 내 점유 전체)를 화면에 반영하고
  // "현재 범위 잔여 pending" 카운트를 갱신하는 공통부. 성공 시 반영된 목록을 반환(경쟁 폐기·
  // 오류면 null) — 호출부가 "실제로 뭔가 받았는지" 판단하는 데 쓴다.
  const sync = useCallback(
    async (fetchItems: () => Promise<ReviewItem[]>): Promise<ReviewItem[] | null> => {
      const token = ++reqRef.current;
      setLoading(true);
      setError(null);
      try {
        const mine = await fetchItems();
        // limit=1 — 카운트(total)만 필요. total 은 미점유 pending 중 필터 반영분(=받아갈 수 있는 수).
        const q = await fetchQueue({
          status: "pending",
          limit: 1,
          offset: 0,
          filter: filterRef.current,
        });
        if (token !== reqRef.current) return null; // 더 최신 요청이 진행 중 — 결과 버림.
        setItems(mine);
        setRemaining(q.total);
        return mine;
      } catch (e) {
        if (token === reqRef.current) setError(e instanceof Error ? e.message : String(e));
        return null;
      } finally {
        if (token === reqRef.current) setLoading(false);
      }
    },
    [],
  );

  // 부작용 없는 목록 갱신 — 페이지 로드·재로그인 복원·확정/거부 후. 점유는 절대 안 늘어난다.
  const refresh = useCallback(() => sync(fetchMyWork), [sync]);
  // '작업 받기' 전용 — 현재 필터 조건으로 +30개 추가 배정(응답 = 내 점유 전체).
  // 진행률 세션 리셋은 새 항목이 실제로 들어왔을 때만(풀 고갈·총량 100 도달이면 배정 0 — 유지).
  const claimMore = async () => {
    const before = items.length;
    const mine = await sync(() => claimWork(filterRef.current));
    if (mine && mine.length > before) setSessionDone(0);
  };

  // 최초: 필터 옵션 로드 + 내 작업분 복원(추가 배정 없음 — 로그아웃·새로고침해도 그대로).
  useEffect(() => {
    fetchQueueFilters()
      .then((f) => {
        setCountryOpts(
          f.countries.map((c) => ({ value: c.iso2, label: c.label, code: c.iso2, aliases: c.aliases })),
        );
        // '미분류'(분류 실패 폴백값)도 작업범위 대상 — BE 옵션에 없을 때만 덧붙인다(#115 중복 방지).
        setIndustryOpts(
          withUnclassified(f.industries).map((i) => ({ value: i.value, label: i.label, aliases: i.aliases })),
        );
      })
      .catch(() => {
        // 옵션 로드 실패해도 작업 자체는 가능 — 무시(셀렉트만 빈 채로).
      });
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 작업범위(픽커) 변경 — 조건만 상태·localStorage 에 저장한다(네트워크 없음). 필터는 '작업
  // 받기'의 신규 배정분에만 적용되고, 이미 받은 작업분은 필터를 바꿔도 그대로 유지된다.
  const setFilterValue = (next: ClaimFilter) => {
    setFilter(next);
    filterRef.current = next;
    localStorage.setItem(FILTER_KEY, JSON.stringify(next));
  };

  // 성공(처리 완료)이면 true — 팝업의 '성공 시에만 다음 행 전진' 판단에 쓰인다.
  const act = async (id: string, kind: "confirm" | "reject", selected?: string): Promise<boolean> => {
    setBusyIds((p) => new Set(p).add(id));
    setError(null);
    let ok = false;
    try {
      if (kind === "confirm") await confirmReview(id, selected);
      else await rejectReview(id);
      setSessionDone((n) => n + 1);
      ok = true;
    } catch (e) {
      // 409(타인 점유 중 — 관리자 회수 후 재배정된 경우)·400(형식 오류) 등 — 메시지 표시.
      // ok=false 라 전진하지 않고, 아래 refresh 가 내 점유가 아닌 항목을 목록에서 걷어낸다.
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyIds((p) => {
        const n = new Set(p);
        n.delete(id);
        return n;
      });
      await refresh(); // 처리(또는 충돌) 후 목록 갱신 — 부작용 없는 조회(추가 배정 없음).
    }
    return ok;
  };

  const hasFilter = Boolean(filter.country || filter.industry || filter.listed);

  return (
    <>
      <div className="flex flex-wrap items-start gap-3 mb-4 p-3 border border-line rounded-md bg-[rgba(127,127,127,0.06)]">
        {/* FE-3: 필터는 신규 배정에만 적용 — 기존 작업분 유지 안내를 필터 바 안에 상시 노출. */}
        <p className="w-full m-0 text-muted text-[12px]">
          작업범위는 <strong className="text-ink font-medium">새로 받아올 작업</strong>에만
          적용됩니다 — 바꿔도 이미 받은 작업분은 그대로 유지됩니다.
        </p>
        <div className={FIELD}>
          <span>국가 <span className="text-muted">(선택 안 함 = 전체)</span></span>
          <MultiPicker
            options={countryOpts}
            value={filter.country}
            onChange={(csv) => setFilterValue({ ...filter, country: csv })}
            placeholder="국가 검색 (예: 미국, US, 일본)"
            emptyHint="전체 국가"
          />
        </div>
        <div className={FIELD}>
          <span>업종 <span className="text-muted">(선택 안 함 = 전체)</span></span>
          <MultiPicker
            options={industryOpts}
            value={filter.industry}
            onChange={(csv) => setFilterValue({ ...filter, industry: csv })}
            placeholder="업종 검색 (예: 반도체, 미분류)"
            emptyHint="전체 업종"
          />
        </div>
        <label className={FIELD}>
          상장여부
          <select
            className={INPUT}
            value={filter.listed}
            onChange={(e) => setFilterValue({ ...filter, listed: e.target.value as "" | Listed })}
          >
            {LISTED_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        {remaining !== null && (
          <span className="text-muted text-[13px] pb-1.5 tabular-nums">
            현재 범위에서 받아갈 수 있는 작업 <strong className="text-ink">{remaining}</strong>건
          </span>
        )}
      </div>

      <div className="flex items-center gap-4 mb-4 flex-wrap">
        <p className="text-muted my-2 tabular-nums">
          내 작업 {items.length}건{loading && " · 불러오는 중…"}
        </p>
        <div className="flex gap-1">
          {/* 추가형(+30) — 자동 호출 금지, 이 버튼만 claim 을 부른다(그 외 갱신은 전부 mine).
              총량 100(CLAIM_CAP) 도달 시 서버가 0건 배정하므로 버튼을 막고 이유를 표시. */}
          <button
            className={BTN}
            onClick={() => void claimMore()}
            disabled={loading || items.length >= CLAIM_CAP}
            title={
              items.length >= CLAIM_CAP
                ? `동시 점유 상한(${CLAIM_CAP}건)에 도달했습니다 — 받아둔 작업을 먼저 처리하세요`
                : undefined
            }
          >
            작업 받기 (+30건)
          </button>
          <button className={BTN} onClick={() => void refresh()} disabled={loading}>
            새로고침
          </button>
        </div>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}

      {loading && items.length === 0 ? (
        <TableSkeleton />
      ) : items.length === 0 ? (
        <p className={EMPTY}>
          {remaining === 0
            ? hasFilter
              ? "받아둔 작업이 없고, 이 범위에 받아갈 작업도 없습니다 — 필터를 넓히거나 해제해 보세요."
              : "받아둔 작업이 없고, 받아갈 수 있는 작업도 없습니다 — 큐가 비었거나 모두 처리되었습니다."
            : "받아둔 작업이 없습니다 — '작업 받기'를 눌러 새 작업을 받아오세요."}
        </p>
      ) : (
        <QueueTable
          items={items}
          busyIds={busyIds}
          doneCount={sessionDone}
          // 진행률 분모 = 이번 세션 처리분 + 내 잔여 작업분(내 배치 기준). 전체큐 잔여(remaining)를
          // 쓰면 영구 배정에선 내 점유가 전체큐에서 빠져 있어 처리해도 분모가 계속 자란다.
          remaining={items.length}
          onConfirm={(id, selected) => act(id, "confirm", selected)}
          onReject={(id) => act(id, "reject")}
        />
      )}
    </>
  );
}
