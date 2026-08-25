import { useCallback, useEffect, useMemo, useState } from "react";
import { Pause, Play, RefreshCw, Square } from "lucide-react";
import { toast } from "sonner";
import {
  UNCLASSIFIED_INDUSTRY_OPTION,
  cancelSegmentJob,
  createSegmentJob,
  errStatus,
  fetchSegmentJobPreview,
  fetchSegmentJobs,
  pauseSegmentJob,
  resumeSegmentJob,
  updateSegmentJobPriority,
} from "../../api";
import type { Listed, SegmentJobInfo, SegmentJobPreview, SegmentJobStatus } from "../../types";
import { errMsg } from "../../format";
import { krInScope, useQueueFilterOpts } from "../../filterOptions";
import { MultiPicker } from "../MultiPicker";
import { ErrorBox } from "../ErrorBox";
import { BTN, BTN_CONFIRM, BTN_REJECT, EMPTY, INPUT } from "../../ui";
import {
  SECTION_H2,
  FIELD,
  INPUT_WIDE,
  CRAWL_TARGET,
  KR_REGION_OPTS,
  LISTED_TARGET_OPTIONS,
  fmt,
  segCls,
} from "./shared";
import { ConfirmDialog } from "./ConfirmDialog";

// 세그먼트 작업 상태 → 한글 라벨. 백필과 달리 대상이 유한해 '완료'가 있고, 실행 전 '대기'가 있다.
const STATUS_LABEL: Record<SegmentJobStatus, string> = {
  queued: "대기",
  running: "진행 중",
  paused: "일시중지",
  done: "완료",
  cancelled: "취소됨",
  failed: "실패",
  budget_exhausted: "예산 소진",
};

// budget_exhausted 는 월 예산에 의한 **정상 종료**라 실패(danger)와 구분해 경고 톤으로 둔다
// (백필 섹션과 같은 규칙 — 빨갛게 칠하면 장애로 오인된다).
const STATUS_TONE: Record<SegmentJobStatus, string> = {
  queued: "text-muted",
  running: "text-ink",
  paused: "text-warn",
  done: "text-ok-fg",
  cancelled: "text-muted",
  failed: "text-danger-fg",
  budget_exhausted: "text-warn",
};

// 여러 건을 텍스트 없이도 스캔하기 위한 상태 점(크롤 이력과 같은 어휘). running 만 깜빡인다.
const STATUS_DOT: Record<SegmentJobStatus, string> = {
  queued: "bg-muted",
  running: "bg-ok crawl-dot-blink",
  paused: "bg-warn",
  done: "bg-ok",
  cancelled: "bg-muted",
  failed: "bg-danger",
  budget_exhausted: "bg-warn",
};

// stop_reason → 보조 문구. 알 수 없는 값은 원문 노출(BE 확장 시 화면이 조용히 비지 않게).
const STOP_REASON_LABEL: Record<string, string> = {
  operator: "운영자 요청",
  monthly_budget: "월 예산 한도 도달",
  cancelled_before_resume: "재개 전 취소 확인",
};

// 액션 활성 조건 = BE 409 규칙 그대로. 서버가 거부할 버튼을 눌러보게 두면 409 토스트만 쌓인다.
const canPause = (s: SegmentJobStatus): boolean => s === "running" || s === "queued";
const canResume = (s: SegmentJobStatus): boolean =>
  s === "paused" || s === "failed" || s === "budget_exhausted";
// 취소는 '이미 종료'면 409 인데 계약에 종료 집합이 명시돼 있지 않다 — 확실히 살아 있는 세
// 상태에만 노출한다(그래도 경합으로 409 가 오면 안내 토스트로 처리).
const canCancel = (s: SegmentJobStatus): boolean =>
  s === "running" || s === "queued" || s === "paused";
const canPriority = (s: SegmentJobStatus): boolean => s === "queued" || s === "paused";

// 지역 지정 방식 — 계약이 ""(없음)/"all"(전 지역)/CSV(직접 선택) 3분기라 픽커 하나로는
// 표현이 안 된다('all' 을 픽커 옵션에 섞으면 "all,서울" 같은 무효 CSV 가 만들어진다).
type RegionMode = "none" | "all" | "pick";

const REGION_MODE: Record<RegionMode, { seg: string; hint: string }> = {
  none: { seg: "지정 안 함", hint: "지역 구분 없이 발견" },
  all: { seg: "전 지역", hint: "KR 17개 시/도로 나눠 발견" },
  pick: { seg: "직접 선택", hint: "고른 시/도만 발견" },
};

// 천단위 구분 — 대상이 수만 단위라 구분자 없으면 자릿수를 못 읽는다.
const n = (v: number): string => v.toLocaleString();

// 0~1000 정수만 통과(BE 검증과 동일). 입력 중 빈값·비정수는 null → 제출 잠금.
function parsePriority(s: string): number | null {
  const v = Number(s.trim());
  return s.trim() !== "" && Number.isInteger(v) && v >= 0 && v <= 1000 ? v : null;
}

// 조건 요약 한 줄 — 목록 카드에서 어떤 세그먼트를 요청한 건지 식별하는 유일한 단서.
// 기본값(상장 무필터·지역 없음)은 생략해 실제로 건 조건만 눈에 남긴다.
function targetOf(j: SegmentJobInfo): string {
  const parts = [j.countries, j.industries];
  if (j.listed !== "unknown") parts.push(j.listed === "listed" ? "상장" : "비상장");
  if (j.regions) parts.push(j.regions === "all" ? "전 지역" : j.regions);
  return parts.filter(Boolean).join(" · ");
}

// 승격 진행률 — initial_target 은 승격 단계 진입 시 확정된다. 아직 0 이면 '집계 중'이지
// '대상 없음'이 아니다(백필과 정반대 해석이라 문구를 분명히 가른다).
function promoteProgress(j: SegmentJobInfo): { text: string; pct: number | null } {
  if (j.initial_target <= 0) return { text: "승격 대상 집계 중", pct: null };
  const pct = Math.min(100, Math.round((j.processed / j.initial_target) * 100));
  return { text: `승격 ${n(j.processed)}/${n(j.initial_target)} (${pct}%)`, pct };
}

// 진행 표시 — status 를 먼저 보고, running 일 때만 stage 로 갈라진다.
// pct=null 이면 진행바를 그리지 않는다(discover 는 총량을 모르는 구간이라 진행률이 없다).
function progressOf(j: SegmentJobInfo): { text: string; pct: number | null } {
  if (j.status === "queued")
    return {
      text: j.queue_position !== null ? `대기열 ${j.queue_position}번째` : "대기 중",
      pct: null,
    };
  if (j.status === "running") {
    if (j.stage === "discover") return { text: `발견 중 · ${n(j.discovered)}건`, pct: null };
    if (j.stage === "promote") return promoteProgress(j);
    if (j.stage === "done") return { text: "마무리 중", pct: 100 };
    return { text: "시작 중…", pct: null };
  }
  // 종료·중지 — 승격까지 갔으면 그 시점 진행률을 남겨 어디까지 하고 멈췄는지 보이게 한다.
  return j.initial_target > 0 ? promoteProgress(j) : { text: `발견 ${n(j.discovered)}건`, pct: null };
}

// 세그먼트 작업 요청(#403) — 관리자가 국가·업종·상장·지역을 지정하면 발견→승격까지
// 백그라운드 큐가 순차 처리한다. S 잡은 **한 번에 1건만 실행**되고 나머지는 우선순위 대기열로
// 들어가므로, 화면은 '요청 폼 + 대기열 목록' 두 덩어리로 구성한다.
export function SegmentJobSection() {
  const [countries, setCountries] = useState("");
  const [industries, setIndustries] = useState("");
  const [listed, setListed] = useState<Listed>("unknown");
  const [regionMode, setRegionMode] = useState<RegionMode>("none");
  const [regions, setRegions] = useState("");
  // 0~1000 정수(기본 100). 입력 중 빈값을 허용해야 해서 문자열로 들고 제출 시 파싱한다.
  const [priority, setPriority] = useState("100");
  const [preview, setPreview] = useState<SegmentJobPreview | null>(null);
  const [pvLoading, setPvLoading] = useState(false);
  const [pvErr, setPvErr] = useState<string | null>(null);
  const [pvKey, setPvKey] = useState(0); // 수동 새로고침 트리거
  const [jobs, setJobs] = useState<SegmentJobInfo[] | null>(null);
  const [total, setTotal] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); // 요청 제출 왕복 중(이중 클릭 방지)
  const [actingId, setActingId] = useState<string | null>(null); // 행 액션 왕복 중
  const [cancelId, setCancelId] = useState<string | null>(null); // 취소 확인 다이얼로그 대상

  // 어휘 출처는 조회 필터와 동일(/queue/filters) — 원장 저장값과 같은 택소노미라야 매치된다.
  const { countryOpts, industryOpts } = useQueueFilterOpts(setErr);
  // '미분류'는 실업종이 아니라 분류 실패 폴백값 — BE 가 422 로 거부하므로 옵션에서 뺀다.
  const targetIndustryOpts = useMemo(
    () => industryOpts.filter((o) => o.value !== UNCLASSIFIED_INDUSTRY_OPTION.value),
    [industryOpts],
  );

  // 지역은 countries 에 KR 이 있을 때만 유효(아니면 422). 숨김 중엔 전송도 빈값으로 비워
  // 잔존 선택이 몰래 나가는 걸 막고, 상태는 유지해 KR 재선택 시 복원한다(크롤 섹션과 동일).
  const inKr = krInScope(countries);
  const regionsPayload = !inKr
    ? ""
    : regionMode === "all"
      ? "all"
      : regionMode === "pick"
        ? regions.trim()
        : "";

  const prio = parsePriority(priority);
  // 국가·업종은 CSV 필수 — 둘 다 차야 미리보기·제출이 의미를 갖는다(빈값은 BE 422).
  const ready = countries.trim() !== "" && industries.trim() !== "";
  const overLimit = preview !== null && preview.segments > preview.max_segments;

  const refresh = useCallback(async () => {
    try {
      const r = await fetchSegmentJobs();
      // 배열 가드 — mock 폴백·프록시 오응답 등 비배열 200 이 와도 렌더를 깨지 않는다.
      if (!Array.isArray(r?.items)) return;
      setJobs(r.items);
      setTotal(typeof r.total === "number" ? r.total : r.items.length);
    } catch (e) {
      setErr(errMsg(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 대기·진행 중인 작업이 있을 때만 3초 폴링(크롤·백필과 같은 주기). 목록은 저장 카운터만
  // 읽는 값싼 조회라 폴링이 허용된다 — 비싼 건 preview 쪽(아래)이다.
  const active = (jobs ?? []).some((j) => j.status === "running" || j.status === "queued");
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => void refresh(), 3000);
    return () => clearInterval(timer);
  }, [active, refresh]);

  // 규모 미리보기 — 원장 COUNT 라 **주기 폴링 금지**. 조건이 다 찼을 때 디바운스 1회만 돈다
  // (픽커 토글마다 나가는 걸 막으려 백필의 400ms 보다 길게 둔다).
  useEffect(() => {
    if (!ready) {
      setPreview(null);
      setPvErr(null);
      return;
    }
    let alive = true;
    const timer = setTimeout(() => {
      setPvLoading(true);
      fetchSegmentJobPreview({
        countries: countries.trim(),
        industries: industries.trim(),
        listed,
        regions: regionsPayload,
      })
        .then((p) => {
          if (!alive) return;
          setPreview(p);
          setPvErr(null);
        })
        .catch((e) => {
          if (!alive) return;
          // 미리보기는 생성과 같은 검증을 타므로 여기 422 = 그대로 제출해도 거부되는 조건.
          setPreview(null);
          setPvErr(errMsg(e));
        })
        .finally(() => {
          if (alive) setPvLoading(false);
        });
    }, 600);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [countries, industries, listed, regionsPayload, ready, pvKey]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (prio === null) {
      setErr("우선순위는 0~1000 사이 정수여야 합니다");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const job = await createSegmentJob({
        countries: countries.trim(),
        industries: industries.trim(),
        listed,
        regions: regionsPayload,
        priority: prio,
      });
      // 즉시 실행인지 대기인지는 그 순간 다른 S 잡 유무로 갈린다 — 결과를 그대로 알린다.
      toast.success(
        job.status === "running"
          ? "작업을 시작했습니다"
          : `대기열에 등록했습니다${job.queue_position !== null ? ` · ${job.queue_position}번째` : ""}`,
      );
      await refresh();
    } catch (e2) {
      setErr(errMsg(e2));
    } finally {
      setBusy(false);
    }
  };

  // 행 액션 공통 — 409/404 는 '화면이 본 상태가 이미 지난 것'이라 오류 박스 대신 안내 후
  // 최신 상태로 맞춘다. 성공·실패 무관하게 목록을 재조회해 버튼 활성 조건을 갱신한다.
  const act = async (id: string, run: () => Promise<SegmentJobInfo>, ok: string) => {
    setActingId(id);
    setErr(null);
    try {
      await run();
      toast.success(ok);
    } catch (e) {
      const st = errStatus(e);
      if (st === 409) toast.info("이미 상태가 바뀌어 처리할 수 없습니다");
      else if (st === 404) toast.info("작업을 찾을 수 없습니다");
      else setErr(errMsg(e));
    } finally {
      setActingId(null);
      setCancelId(null);
      await refresh();
    }
  };

  const cancelTarget = (jobs ?? []).find((j) => j.id === cancelId) ?? null;

  return (
    <section>
      <h2 className={SECTION_H2}>세그먼트 작업 요청</h2>
      {err && <ErrorBox>{err}</ErrorBox>}
      <ConfirmDialog
        open={cancelTarget !== null}
        title="이 작업을 취소할까요?"
        danger
        confirmLabel="취소 요청"
        busy={actingId !== null}
        busyLabel="취소 요청 중…"
        onConfirm={() =>
          cancelTarget &&
          void act(cancelTarget.id, () => cancelSegmentJob(cancelTarget.id), "취소 요청됨")
        }
        onCancel={() => setCancelId(null)}
      >
        <p className="m-0 text-muted text-sm">
          이미 적재된 분은 보존됩니다
          {cancelTarget?.status === "running" && " · 진행 중이라 실제 중지까지 수 초 걸립니다"}
        </p>
      </ConfirmDialog>

      <form className={CRAWL_TARGET} onSubmit={(e) => void submit(e)}>
        <div className={FIELD}>
          <span>
            국가 <span className="text-muted">(필수)</span>
          </span>
          <MultiPicker
            options={countryOpts}
            value={countries}
            onChange={setCountries}
            placeholder="국가 검색 (예: 미국, US, 일본)"
            emptyHint="국가를 1개 이상 선택하세요"
          />
        </div>
        <div className={FIELD}>
          <span>
            업종 <span className="text-muted">(필수)</span>
          </span>
          <MultiPicker
            options={targetIndustryOpts}
            value={industries}
            onChange={setIndustries}
            placeholder="업종 검색 (예: 은행, 반도체)"
            emptyHint="업종을 1개 이상 선택하세요"
          />
        </div>
        {inKr && (
          <div className={FIELD}>
            <div className="flex items-center gap-2">
              <span>
                지역 <span className="text-muted">(KR 전용)</span>
              </span>
              <span className="inline-flex gap-1" role="group" aria-label="지역 지정 방식">
                {(Object.keys(REGION_MODE) as RegionMode[]).map((m) => (
                  <button
                    key={m}
                    type="button"
                    className={segCls(regionMode === m)}
                    aria-pressed={regionMode === m}
                    onClick={() => setRegionMode(m)}
                  >
                    {REGION_MODE[m].seg}
                  </button>
                ))}
              </span>
            </div>
            {regionMode === "pick" ? (
              <MultiPicker
                options={KR_REGION_OPTS}
                value={regions}
                onChange={setRegions}
                placeholder="지역 검색 (예: 서울, 경기)"
                emptyHint="선택 없음 — 지역 구분 없이 발견"
              />
            ) : (
              <span className="text-muted text-xs flex items-center min-h-[24px]">
                {REGION_MODE[regionMode].hint}
              </span>
            )}
          </div>
        )}
        <div className="flex flex-col gap-2">
          <label className={FIELD}>
            상장여부
            <select
              className={INPUT_WIDE}
              value={listed}
              onChange={(e) => setListed(e.target.value as Listed)}
            >
              {LISTED_TARGET_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className={FIELD}>
            <span>
              우선순위 <span className="text-muted">(0~1000 · 낮을수록 먼저)</span>
            </span>
            <input
              className={`${INPUT} w-[120px] tabular-nums`}
              type="number"
              min={0}
              max={1000}
              step={1}
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
            />
            {prio === null && (
              <span className="text-danger-fg text-xs">0~1000 사이 정수를 입력하세요</span>
            )}
          </label>
          <button
            className={`${BTN_CONFIRM} mt-0.5 self-start`}
            type="submit"
            disabled={busy || !ready || prio === null || overLimit}
          >
            {busy ? "요청 중…" : "작업 요청"}
          </button>
        </div>
      </form>

      <PreviewPanel
        preview={preview}
        error={pvErr}
        loading={pvLoading}
        ready={ready}
        onRefresh={() => setPvKey((k) => k + 1)}
      />

      <div className="flex items-center justify-between gap-2 mt-5 mb-2">
        <h3 className="text-sm font-semibold text-ink m-0">
          작업 대기열
          {jobs !== null && jobs.length > 0 && (
            <span className="text-muted font-normal">
              {" "}
              · 총 {n(total)}건{total > jobs.length && ` (최근 ${n(jobs.length)}건 표시)`}
            </span>
          )}
        </h3>
        <button
          className={`${BTN} py-0.5! px-2! text-xs`}
          type="button"
          onClick={() => void refresh()}
        >
          <span className="inline-flex items-center gap-1">
            <RefreshCw size={12} aria-hidden />
            새로고침
          </span>
        </button>
      </div>
      {jobs === null ? (
        <p className={EMPTY}>불러오는 중…</p>
      ) : jobs.length === 0 ? (
        <p className={EMPTY}>요청된 세그먼트 작업이 없습니다.</p>
      ) : (
        <div className="flex flex-col gap-2">
          {jobs.map((j) => (
            <JobCard
              key={j.id}
              job={j}
              busy={actingId === j.id}
              onPause={() => void act(j.id, () => pauseSegmentJob(j.id), "일시중지 요청됨")}
              onResume={() => void act(j.id, () => resumeSegmentJob(j.id), "재개됨")}
              onCancel={() => setCancelId(j.id)}
              onPriority={(p) =>
                void act(j.id, () => updateSegmentJobPriority(j.id, p), "우선순위 변경됨")
              }
            />
          ))}
        </div>
      )}
    </section>
  );
}

// 요청 규모 — 조건 확정 시 1회만 조회한다(원장 COUNT). segments 가 상한을 넘으면 제출이
// 422 로 거부되므로 버튼을 잠그고 이유를 먼저 보여준다.
function PreviewPanel({
  preview,
  error,
  loading,
  ready,
  onRefresh,
}: {
  preview: SegmentJobPreview | null;
  error: string | null;
  loading: boolean;
  ready: boolean;
  onRefresh: () => void;
}) {
  const over = preview !== null && preview.segments > preview.max_segments;
  return (
    <div className="mt-3 p-3 border border-line rounded-md bg-panel">
      <div className="flex items-center justify-between gap-2 mb-2">
        <span className="text-muted text-xs">요청 규모 (조건 확정 시 1회 조회)</span>
        <button
          className={`${BTN} py-0.5! px-2! text-xs`}
          type="button"
          disabled={!ready || loading}
          onClick={onRefresh}
          title="규모 다시 계산 (원장 집계라 자동 갱신하지 않습니다)"
        >
          <span className="inline-flex items-center gap-1">
            <RefreshCw size={12} className={loading ? "animate-spin" : undefined} aria-hidden />
            새로고침
          </span>
        </button>
      </div>
      {!ready ? (
        <p className="text-muted text-xs m-0">국가·업종을 고르면 세그먼트 수를 계산합니다.</p>
      ) : error ? (
        <ErrorBox>{error}</ErrorBox>
      ) : (
        <>
          <div className="flex flex-wrap gap-x-6 gap-y-2">
            <div className="flex flex-col gap-0.5">
              <span className="text-muted text-xs">세그먼트</span>
              <span className={`text-lg tabular-nums ${over ? "text-danger-fg" : "text-ink"}`}>
                {preview ? `${n(preview.segments)} / ${n(preview.max_segments)}` : "—"}
              </span>
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="text-muted text-xs">승격 대기</span>
              <span className="text-lg tabular-nums text-ink">
                {preview ? n(preview.promote_pending) : "—"}
              </span>
            </div>
          </div>
          {over && (
            <p className="text-danger-fg text-xs mt-2 mb-0">
              세그먼트 수가 상한을 넘어 요청이 거부됩니다 — 국가·업종·지역 범위를 줄이세요.
            </p>
          )}
        </>
      )}
    </div>
  );
}

// 작업 1건 카드 — 목록 응답이 상세와 같은 스키마라 별도 상세 조회 없이 여기서 다 그린다.
// 상시 노출은 상태·조건·진행·깔때기까지, 프로세스 내부 지표는 '자세히'로 접는다.
function JobCard({
  job,
  busy,
  onPause,
  onResume,
  onCancel,
  onPriority,
}: {
  job: SegmentJobInfo;
  busy: boolean;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
  onPriority: (priority: number) => void;
}) {
  // null = 편집 안 함(표시만). 폴링이 3초마다 목록을 갈아끼워도 편집 중 입력은 유지된다.
  const [draft, setDraft] = useState<string | null>(null);
  const parsed = draft === null ? null : parsePriority(draft);
  const prog = progressOf(job);
  const stopping = job.cancel_requested && (job.status === "running" || job.status === "queued");

  return (
    <div className="p-3 border border-line rounded-md bg-panel">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
        <span
          className={`inline-block w-2 h-2 rounded-full ${STATUS_DOT[job.status]}`}
          aria-hidden
        />
        <span className={`font-semibold text-sm ${STATUS_TONE[job.status]}`}>
          {STATUS_LABEL[job.status]}
        </span>
        <span className="text-ink text-sm [overflow-wrap:anywhere]">{targetOf(job)}</span>
        {/* 트랙 S 의 started_at 은 실행 시작이 아니라 **요청 생성 시각**이다. */}
        <span className="text-muted text-xs tabular-nums">· 요청 {fmt(job.started_at)}</span>
        <span className="text-muted text-xs">· 우선순위 {job.priority}</span>
        {job.triggered_by && <span className="text-muted text-xs">· {job.triggered_by}</span>}
        {stopping && <span className="text-warn text-xs">· 중지 요청됨…</span>}
      </div>

      {prog.pct !== null && (
        <div
          className="w-full h-1.5 rounded-full bg-line my-2 overflow-hidden"
          role="progressbar"
          aria-valuenow={prog.pct}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div
            className="h-full bg-ok rounded-full transition-[width]"
            style={{ width: `${prog.pct}%` }}
          />
        </div>
      )}
      <p className="text-muted text-xs tabular-nums my-1">{prog.text}</p>

      {/* 깔때기 — 발견부터 이메일 확보까지 한 줄. 실패분은 0 이면 노이즈라 있을 때만 붙인다. */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-muted text-xs tabular-nums my-1">
        <Step label="발견" value={job.discovered} />
        <span aria-hidden>→</span>
        <Step label="처리" value={job.processed} />
        <span aria-hidden>→</span>
        <Step label="승격" value={job.promoted} />
        <span aria-hidden>→</span>
        <Step label="이메일" value={job.emails} />
        {job.failed_items > 0 && <span className="text-warn">· 실패 {n(job.failed_items)}</span>}
      </div>

      {job.error && <ErrorBox>{job.error}</ErrorBox>}
      {!job.error && job.stop_reason && (
        <p className="text-muted text-xs my-1">
          종료 사유: {STOP_REASON_LABEL[job.stop_reason] ?? job.stop_reason}
        </p>
      )}
      {job.finished_at && <p className="text-muted text-xs my-1">종료: {fmt(job.finished_at)}</p>}

      <div className="flex flex-wrap items-center gap-2 mt-2">
        {canPause(job.status) && (
          <button
            className={`${BTN} py-0.5! px-2! text-xs`}
            type="button"
            // 중지 요청이 이미 접수됐으면 재클릭 무의미 — BE 가 멈출 때까지 running 이 유지된다.
            disabled={busy || job.cancel_requested}
            onClick={onPause}
          >
            <span className="inline-flex items-center gap-1">
              <Pause size={12} aria-hidden />
              일시중지
            </span>
          </button>
        )}
        {canResume(job.status) && (
          <button
            className={`${BTN} py-0.5! px-2! text-xs`}
            type="button"
            disabled={busy}
            onClick={onResume}
          >
            <span className="inline-flex items-center gap-1">
              <Play size={12} aria-hidden />
              재개
            </span>
          </button>
        )}
        {canCancel(job.status) && (
          <button
            className={`${BTN_REJECT} py-0.5! px-2! text-xs`}
            type="button"
            disabled={busy || job.cancel_requested}
            onClick={onCancel}
          >
            <span className="inline-flex items-center gap-1">
              <Square size={12} aria-hidden />
              취소
            </span>
          </button>
        )}
        {canPriority(job.status) &&
          (draft === null ? (
            <button
              className={`${BTN} py-0.5! px-2! text-xs`}
              type="button"
              disabled={busy}
              onClick={() => setDraft(String(job.priority))}
            >
              우선순위 변경
            </button>
          ) : (
            <span className="inline-flex items-center gap-1">
              <input
                className={`${INPUT} py-0.5! px-1.5! w-[72px] text-xs tabular-nums`}
                type="number"
                min={0}
                max={1000}
                step={1}
                autoFocus
                aria-label="우선순위(0~1000, 낮을수록 먼저)"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
              />
              <button
                className={`${BTN_CONFIRM} py-0.5! px-2! text-xs`}
                type="button"
                disabled={busy || parsed === null || parsed === job.priority}
                onClick={() => {
                  if (parsed === null) return;
                  setDraft(null);
                  onPriority(parsed);
                }}
              >
                적용
              </button>
              <button
                className={`${BTN} py-0.5! px-2! text-xs`}
                type="button"
                disabled={busy}
                onClick={() => setDraft(null)}
              >
                취소
              </button>
            </span>
          ))}
      </div>

      {/* 프로세스 내부 지표 — 평상시엔 노이즈고, 재기동·크래시를 의심할 때만 필요하다. */}
      <details className="mt-1.5">
        <summary className="text-muted text-xs cursor-pointer">자세히</summary>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-muted text-xs tabular-nums mt-1.5">
          <span>잔여 {n(job.remaining)}</span>
          <span>배치 {n(job.batches_done)}회 완료</span>
          <span>워커 {job.workers}</span>
          <span>세대 {job.generation}</span>
          <span>재기동 {job.recycles}</span>
          <span>크래시 복구 {job.crash_restarts}</span>
          <span>PID {job.pid ?? "—"}</span>
          <span>갱신 {fmt(job.progress_at ?? job.updated_at)}</span>
          <span className="[overflow-wrap:anywhere]">ID {job.id}</span>
        </div>
      </details>
    </div>
  );
}

function Step({ label, value }: { label: string; value: number }) {
  return (
    <span>
      {label} <span className="text-ink">{n(value)}</span>
    </span>
  );
}
