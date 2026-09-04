import { useEffect, useRef, useState } from "react";
import { RefreshCw, Square } from "lucide-react";
import { toast } from "sonner";
import {
  UNCLASSIFIED_INDUSTRY_OPTION,
  errStatus,
  fetchBackfillOverview,
  fetchBackfillStatus,
  startBackfill,
  stopBackfill,
} from "../../api";
import type { BackfillJob, BackfillJobStatus, BackfillOverview, BackfillStatus } from "../../types";
import { errMsg } from "../../format";
import { useQueueFilterOpts } from "../../filterOptions";
import { MultiPicker } from "../MultiPicker";
import { ErrorBox } from "../ErrorBox";
import { BTN, BTN_CONFIRM, BTN_REJECT } from "../../ui";
import { SECTION_H2, FIELD, FIELD_INLINE, CRAWL_TARGET, fmt, segCls } from "./shared";
import { ConfirmDialog } from "./ConfirmDialog";

// 백필 상태 → 한글 라벨. 지속형 consumer 라 '완료'가 없다(대상 소진=대기).
const STATUS_LABEL: Record<BackfillJobStatus, string> = {
  idle: "대기",
  running: "진행 중",
  failed: "실패",
  cancelled: "중지됨",
  budget_exhausted: "예산 소진",
};

// 헤드라인 색 — budget_exhausted 는 월 예산에 의한 **정상 종료**라 실패(danger)와 구분해
// 경고(warn) 톤으로 둔다. 빨갛게 칠하면 장애로 오인된다.
const STATUS_TONE: Record<BackfillJobStatus, string> = {
  idle: "text-muted",
  running: "text-ink",
  failed: "text-danger-fg",
  cancelled: "text-muted",
  budget_exhausted: "text-warn",
};

// 종료 상태 심각도 순 — 앞에 있을수록 헤드라인 우선.
const TERMINAL_ORDER: BackfillJobStatus[] = ["failed", "budget_exhausted", "cancelled", "idle"];

// stop_reason → 보조 문구. 알 수 없는 값은 원문 노출(BE 확장 시 화면이 조용히 비지 않게).
const STOP_REASON_LABEL: Record<string, string> = {
  operator: "운영자 중지",
  monthly_budget: "월 예산 한도 도달",
  cancelled_before_resume: "재개 전 취소 확인",
};

// 업종 조건 방향(#372) — BE 가 포함식(industries)·제외식(exclude_industries) 동시 지정을
// 422 로 거부하므로, 값 하나에 모드를 붙여 **구조적으로 한쪽만** 전송한다.
type IndustryMode = "include" | "exclude";

// 모드별 문구 — 라벨·플레이스홀더·요약을 통째로 갈아끼워 의미 반전을 삼중 명시한다.
// 같은 픽커·같은 어휘라 표기가 약하면 정반대 조건으로 대량 백필이 도는 사고가 난다.
// summaryAll 은 어휘를 전부 고른 경우 전용 — 그때는 '나머지'가 없어 summary 가 사실과
// 반대가 된다(전 업종을 제외해놓고 "나머지 전 업종 대상").
const INDUSTRY_MODE: Record<
  IndustryMode,
  {
    seg: string;
    label: string;
    hint: string;
    placeholder: string;
    emptyHint: string;
    summary: (picked: string[]) => string;
    summaryAll: string;
  }
> = {
  include: {
    seg: "선택 업종만",
    label: "대상 업종",
    hint: "(선택 안 함 = 전 업종)",
    placeholder: "대상 업종 검색 (예: 반도체·디스플레이, 미분류)",
    emptyHint: "전 업종 대상",
    summary: (p) => `${p.join(", ")}만 대상 — 나머지 업종 제외`,
    summaryAll: "전 업종 대상 — 제외되는 업종 없음",
  },
  exclude: {
    seg: "선택 업종 제외",
    label: "제외할 업종",
    hint: "(선택 안 함 = 제외 없음)",
    placeholder: "제외할 업종 검색 (예: 건설·엔지니어링)",
    emptyHint: "제외 없음 — 전 업종 대상",
    summary: (p) => `${p.join(", ")} 제외 — 나머지 전 업종 대상`,
    summaryAll: "전 업종 제외 — 남는 대상 없음",
  },
};

// 폼 상태 → 전송 조건. 모드가 고르지 않은 쪽은 **항상 빈 문자열**이라 포함·제외 동시
// 지정(422)이 구조적으로 불가능하다. overview·start 가 같은 함수를 써 미리보기와 실제
// 실행 대상이 어긋나지 않게 한다.
function backfillFilters(f: {
  countries: string;
  mode: IndustryMode;
  industries: string;
  excludeListed: boolean;
}) {
  const csv = f.industries.trim();
  return {
    countries: f.countries.trim(),
    industries: f.mode === "include" ? csv : "",
    exclude_industries: f.mode === "exclude" ? csv : "",
    exclude_listed: f.excludeListed,
  };
}

const jobs = (s: BackfillStatus): BackfillJob[] => [s.resolve, s.fill];

// 액션 게이트 — 버튼 노출/잠금은 오직 이것으로 판단한다. 표시 상태와 분리하지 않으면
// '한쪽 실패 + 다른 쪽 진행 중'에서 중지 버튼이 사라져 멈출 수단이 없어진다.
const isRunning = (s: BackfillStatus): boolean => jobs(s).some((j) => j.status === "running");

// 표시 상태 — 진행 중이면 진행 중, 아니면 두 트랙 중 가장 심각한 종료 상태.
function headlineStatus(s: BackfillStatus): BackfillJobStatus {
  if (isRunning(s)) return "running";
  return TERMINAL_ORDER.find((st) => jobs(s).some((j) => j.status === st)) ?? "idle";
}

// 천단위 구분 — 잔여가 수만 단위라 구분자 없으면 자릿수를 못 읽는다.
const n = (v: number): string => v.toLocaleString();

// 백필 실행(#352) — 딸깍 원칙: 조건 하나·버튼 하나. 내부적으로 도메인 해석·이메일 채우기
// 두 작업이 함께 돌지만 화면엔 트랙 개념을 노출하지 않고 하나의 진행 카드로 합쳐 보여준다.
// 잔여는 두 소스를 전환해 쓴다 — 대기 중엔 overview(대형 조인이라 폴링 금지), 진행 중엔
// status.remaining(3초 폴링). 검증 큐만 진행 중에도 overview 값이라 수동 새로고침을 둔다.
export function BackfillSection() {
  const [countries, setCountries] = useState("");
  // 업종 값은 하나, 방향만 모드로 가른다(기본=제외식 — 기존 동작 유지). 빈값이면 어느
  // 모드든 업종 조건 없음(전 업종 대상)이라 모드 전환이 무해하다.
  const [industryMode, setIndustryMode] = useState<IndustryMode>("exclude");
  const [industries, setIndustries] = useState("");
  const [excludeListed, setExcludeListed] = useState(false);
  const [status, setStatus] = useState<BackfillStatus | null>(null);
  const [overview, setOverview] = useState<BackfillOverview | null>(null);
  const [ovLoading, setOvLoading] = useState(false);
  const [ovKey, setOvKey] = useState(0); // 수동 새로고침·종료 직후 1회 재조회 트리거
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); // 시작/중지 요청 왕복 중(이중 클릭 방지)
  const [stopDialog, setStopDialog] = useState(false);

  const running = status ? isRunning(status) : false;

  // 백필 필터는 원장 행의 저장 어휘(구분 택소노미+미분류)와 일치해야 매치된다 —
  // 크롤 타깃용 /admin/industries(코드 매핑되는 라벨만)가 아니라 /queue/filters 를
  // 출처로 쓴다(추출·발송 섹션과 동일 사유). 국가 어휘는 양쪽이 같은 supported_countries().
  const { countryOpts, industryOpts } = useQueueFilterOpts(setErr);

  // 진행 중 백필 복원(새로고침 대비). 현황 조회가 실패해도 폼은 살린다.
  useEffect(() => {
    let alive = true;
    fetchBackfillStatus()
      .then((s) => alive && setStatus(s))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  // 잔여 미리보기 — 조건 변경 시 400ms 디바운스로 1회. **주기 폴링 금지**(수십만 행 조인).
  useEffect(() => {
    let alive = true;
    const timer = setTimeout(() => {
      setOvLoading(true);
      fetchBackfillOverview(
        backfillFilters({ countries, mode: industryMode, industries, excludeListed }),
      )
        .then((o) => alive && setOverview(o))
        .catch((e) => alive && setErr(errMsg(e)))
        .finally(() => {
          if (alive) setOvLoading(false);
        });
    }, 400);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [countries, industryMode, industries, excludeListed, ovKey]);

  // 진행 중에만 3초 폴링(크롤 섹션과 동일 주기). 종료 상태가 되면 해제.
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => {
      fetchBackfillStatus()
        .then(setStatus)
        .catch((e) => setErr(errMsg(e)));
    }, 3000);
    return () => clearInterval(timer);
  }, [running]);

  // 진행 중 → 종료 전이에서 잔여를 1회 재조회(백필이 줄여놓은 실제 잔여 반영).
  // 폴링이 아니라 전이 시점 단발이라 overview 비용 제약을 지킨다.
  const prevRunning = useRef(false);
  useEffect(() => {
    if (prevRunning.current && !running) setOvKey((k) => k + 1);
    prevRunning.current = running;
  }, [running]);

  const start = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      setStatus(
        await startBackfill(
          backfillFilters({ countries, mode: industryMode, industries, excludeListed }),
        ),
      );
      toast.success("백필 시작");
    } catch (e2) {
      // 409(이미 활성) 대비 — 현황을 받아 진행 카드·중지 버튼으로 복구한다. 중지 직후엔
      // 마감이 비동기라 409 인데도 아직 running 이 아닐 수 있어 재시도 안내로 가른다.
      const fresh = await fetchBackfillStatus().catch(() => null);
      if (fresh) setStatus(fresh);
      if (fresh && isRunning(fresh)) {
        toast.info("이미 백필이 진행 중입니다");
      } else if (errStatus(e2) === 409) {
        setErr("이전 실행을 정리하는 중입니다. 잠시 후 다시 시도하세요.");
      } else {
        setErr(errMsg(e2));
      }
    } finally {
      setBusy(false);
    }
  };

  // 중지 — 취소 요청만 기록되고 실제 마감은 수 초 내 비동기. 폴링이 종료를 확인한다.
  const doStop = async () => {
    setBusy(true);
    setErr(null);
    try {
      setStatus(await stopBackfill());
      toast.success("중지 요청됨");
    } catch (e) {
      if (errStatus(e) === 404) {
        // 활성 없음 — 이미 끝난 것이라 오류가 아니다. 현황만 맞춘다.
        await fetchBackfillStatus()
          .then(setStatus)
          .catch(() => undefined);
        toast.info("이미 종료되었습니다");
      } else {
        setErr(errMsg(e));
      }
    } finally {
      setBusy(false);
      setStopDialog(false);
    }
  };

  // 중지 요청이 이미 접수된 상태 — 재클릭(다이얼로그 반복)을 막는다.
  const stopping =
    status !== null &&
    running &&
    jobs(status)
      .filter((j) => j.status === "running")
      .every((j) => j.cancel_requested);

  const picked = industries
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const mode = INDUSTRY_MODE[industryMode];
  // 어휘를 전부 고른 상태 — '나머지'가 없어 일반 요약이 정반대 사실을 말하게 된다.
  // 옵션이 아직 안 왔으면(길이 0) 판정하지 않는다(빈 목록 vs 전량 선택 혼동 방지).
  const allPicked = industryOpts.length > 0 && picked.length >= industryOpts.length;
  // 제외식의 '미분류'는 라벨이 문자열 '미분류'인 행만 빼고 **라벨 빈값 행은 남는다**
  // (포함식만 빈값까지 대칭 매칭 — BE #372). 조용히 남으면 제외한 줄 알고 넘어간다.
  const unclassifiedExcludeGap =
    industryMode === "exclude" && picked.includes(UNCLASSIFIED_INDUSTRY_OPTION.value);

  return (
    <section>
      <h2 className={SECTION_H2}>백필 실행</h2>
      {err && <ErrorBox>{err}</ErrorBox>}
      <ConfirmDialog
        open={stopDialog}
        title="진행 중인 백필을 중지할까요?"
        danger
        confirmLabel="중지"
        busy={busy}
        busyLabel="중지 요청 중…"
        onConfirm={() => void doStop()}
        onCancel={() => setStopDialog(false)}
      >
        <p className="m-0 text-muted text-sm">처리된 분은 보존됩니다</p>
      </ConfirmDialog>

      <form className={CRAWL_TARGET} onSubmit={(e) => void start(e)}>
        <div className={FIELD}>
          <span>
            국가 <span className="text-muted">(선택 안 함 = 전체)</span>
          </span>
          <MultiPicker
            options={countryOpts}
            value={countries}
            onChange={setCountries}
            placeholder="국가 검색 (예: 미국, US, 일본)"
            emptyHint="전체 국가"
          />
        </div>
        {/* 업종 조건 — 포함식/제외식을 모드로 고른다(BE 는 동시 지정 422, #372). 같은 픽커
            모양이라 라벨·플레이스홀더·요약을 모드에 따라 통째로 갈아끼워 삼중 명시한다. */}
        <div className={FIELD}>
          <div className="flex items-center gap-2">
            <span>
              {mode.label} <span className="text-muted">{mode.hint}</span>
            </span>
            <span className="inline-flex gap-1" role="group" aria-label="업종 조건 방향">
              {(Object.keys(INDUSTRY_MODE) as IndustryMode[]).map((m) => (
                <button
                  key={m}
                  type="button"
                  className={segCls(industryMode === m)}
                  aria-pressed={industryMode === m}
                  onClick={() => setIndustryMode(m)}
                >
                  {INDUSTRY_MODE[m].seg}
                </button>
              ))}
            </span>
          </div>
          <MultiPicker
            options={industryOpts}
            value={industries}
            onChange={setIndustries}
            placeholder={mode.placeholder}
            emptyHint={mode.emptyHint}
          />
          {picked.length > 0 && (
            <span className="text-warn text-xs">
              {allPicked ? mode.summaryAll : mode.summary(picked)}
            </span>
          )}
          {unclassifiedExcludeGap && (
            <span className="text-muted text-xs">
              '{UNCLASSIFIED_INDUSTRY_OPTION.value}' 제외는 라벨이 비어 있는 회사까지 빼지는
              않습니다
            </span>
          )}
        </div>
        <div className="flex flex-col gap-2">
          <label className={FIELD_INLINE}>
            <input
              type="checkbox"
              checked={excludeListed}
              onChange={(e) => setExcludeListed(e.target.checked)}
            />
            상장사 제외
          </label>
          <div className="flex gap-2 mt-0.5">
            <button className={BTN_CONFIRM} type="submit" disabled={busy || running}>
              {busy || running ? "실행 중…" : "백필 시작"}
            </button>
            {running && (
              <button
                className={BTN_REJECT}
                type="button"
                disabled={busy || stopping}
                onClick={() => setStopDialog(true)}
              >
                <span className="inline-flex items-center gap-1">
                  중지 <Square size={14} aria-hidden />
                </span>
              </button>
            )}
          </div>
        </div>
      </form>

      <RemainingPanel
        overview={overview}
        live={running ? status : null}
        loading={ovLoading}
        onRefresh={() => setOvKey((k) => k + 1)}
      />
      {status && headlineStatus(status) !== "idle" && <BackfillProgress status={status} />}
    </section>
  );
}

// 잔여 3칸 — 대기 중엔 overview, 진행 중엔 status.remaining(라이브)로 앞 2칸을 갈아끼운다.
// 검증 큐는 status 에 대응 필드가 없어 진행 중에도 overview 값(직전 조회)을 유지한다.
function RemainingPanel({
  overview,
  live,
  loading,
  onRefresh,
}: {
  overview: BackfillOverview | null;
  /** 실행 중이면 라이브 잔여의 출처(status), 대기 중이면 null — 호출부가 걸러 넘긴다. */
  live: BackfillStatus | null;
  loading: boolean;
  onRefresh: () => void;
}) {
  const cells: { label: string; value: number | undefined; live: boolean; hint?: string }[] = [
    {
      label: "도메인 해석 대기",
      value: live ? live.resolve.remaining : overview?.resolve_pending,
      live: !!live,
    },
    {
      label: "이메일 채우기 대기",
      value: live ? live.fill.remaining : overview?.fill_pending,
      live: !!live,
    },
    {
      label: "검증 큐 대기",
      value: overview?.queue_pending,
      live: false,
      // BE 계약 — 큐 카운트만 업종 조건(포함·제외 어느 쪽도)을 반영하지 않고 국가만 반영한다.
      hint: "국가 조건만 반영",
    },
  ];

  return (
    <div className="mt-3 p-3 border border-line rounded-md bg-panel">
      <div className="flex items-center justify-between gap-2 mb-2">
        <span className="text-muted text-xs">
          잔여 {live ? "(실행 중 실시간)" : "(조건 기준 미리보기)"}
        </span>
        <button
          className={`${BTN} py-0.5! px-2! text-xs`}
          type="button"
          disabled={loading}
          onClick={onRefresh}
          title="잔여 다시 계산 (대형 조회라 자동 갱신하지 않습니다)"
        >
          <span className="inline-flex items-center gap-1">
            <RefreshCw size={12} className={loading ? "animate-spin" : undefined} aria-hidden />
            새로고침
          </span>
        </button>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-2">
        {cells.map((c) => (
          <div key={c.label} className="flex flex-col gap-0.5">
            <span className="text-muted text-xs">
              {c.label}
              {c.hint && (
                <span className="ml-1 cursor-help" title={c.hint} aria-label={c.hint}>
                  ⓘ
                </span>
              )}
            </span>
            <span className={`text-lg tabular-nums ${c.live ? "text-ink" : "text-muted"}`}>
              {c.value === undefined ? "—" : n(c.value)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// 통합 진행 카드 — 두 트랙을 하나의 상태·진행바·깔때기로 합친다.
function BackfillProgress({ status }: { status: BackfillStatus }) {
  const { resolve: r, fill: f } = status;
  const head = headlineStatus(status);
  const running = head === "running";

  // 진행바는 remaining 이 아니라 processed 기준 — remaining 은 신규 크롤 유입으로 늘어
  // 진행률이 역행한다. processed 는 단조증가, initial_target 은 시작 시점 스냅샷이라 고정.
  const target = r.initial_target + f.initial_target;
  // 두 트랙의 모집단은 배타(도메인 없음 / 도메인 있고 이메일 없음)이고 단위가 같은
  // '회사 1건'이라 처리량 합산은 의미가 성립한다. resolved·emails 는 의미가 달라 안 합친다.
  const done = r.processed + f.processed;
  const pct = target > 0 ? Math.min(100, Math.round((done / target) * 100)) : 0;

  // 한쪽만 비정상 종료했는데 다른 쪽은 계속 도는 경우 — 헤드라인은 진행 중을 유지하되
  // 사실을 숨기지 않는다.
  const strayed = running
    ? jobs(status).find((j) => j.status === "failed" || j.status === "budget_exhausted")
    : undefined;
  const stopped = jobs(status).find((j) => j.stop_reason);
  const failed = jobs(status).find((j) => j.error);
  // 전부 끝났을 때만 마지막 종료 시각을 표시한다 — 한쪽만 끝난 상태에서 '종료'를 띄우면
  // 헤드라인(진행 중)과 모순된다(ISO8601 이라 문자열 정렬로 최신 선택 가능).
  const finishedAt = running
    ? null
    : ([r.finished_at, f.finished_at].filter((v): v is string => !!v).sort().pop() ?? null);

  return (
    <div className="mt-3 p-3 border border-line rounded-md bg-panel">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 my-1">
        {running && (
          <span className="crawl-dot-blink inline-block w-2 h-2 rounded-full bg-ok" aria-hidden />
        )}
        <span className={`font-semibold text-sm ${STATUS_TONE[head]}`}>
          상태: {STATUS_LABEL[head]}
        </span>
        {strayed && (
          <span className="text-warn text-xs">
            · 일부 작업 중단됨({STATUS_LABEL[strayed.status]})
          </span>
        )}
        {running && jobs(status).some((j) => j.cancel_requested) && (
          <span className="text-muted text-xs">· 중지 요청됨…</span>
        )}
        {!running && stopped?.stop_reason && (
          <span className="text-muted text-xs">
            · {STOP_REASON_LABEL[stopped.stop_reason] ?? stopped.stop_reason}
          </span>
        )}
        {r.triggered_by && <span className="text-muted text-xs">· {r.triggered_by}</span>}
      </div>

      <div
        className="w-full h-1.5 rounded-full bg-line my-2 overflow-hidden"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className="h-full bg-ok rounded-full transition-[width]" style={{ width: `${pct}%` }} />
      </div>
      <p className="text-muted text-xs tabular-nums my-1">
        {/* initial_target 은 시작 시점에 확정되므로 0 = 대상 없음(집계 중이 아니다).
            지속형이라 대상이 0이어도 신규 유입분을 기다리며 계속 돈다. */}
        {target > 0
          ? `이번 실행 대상 ${n(target)} 중 ${n(done)} 처리 (${pct}%)`
          : "이번 실행 대상 없음 — 신규 유입분 대기"}
        {/* 지속형이라 100% 가 '완료'가 아니다 — 소진 후에도 신규 유입분을 계속 처리한다. */}
        {running && pct >= 100 && <span className="text-warn"> · 대상 소진 · 신규 유입분 처리 중</span>}
      </p>

      {/* 깔때기 — 트랙 이름 대신 '일의 단계'로 라벨링(딸깍 원칙: 내부 트랙 개념 비노출). */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-muted text-xs tabular-nums my-1">
        <Step label="처리" value={done} />
        <span aria-hidden>→</span>
        <Step label="도메인 찾음" value={r.resolved} />
        <span aria-hidden>→</span>
        <Step label="DB 등재" value={r.promoted} />
        <span aria-hidden>→</span>
        <Step label="이메일" value={f.emails} />
      </div>

      {failed?.error && <ErrorBox>{failed.error}</ErrorBox>}
      {finishedAt && <p className="text-muted text-xs my-1">종료: {fmt(finishedAt)}</p>}

      {/* 프로세스 내부 지표 — 평상시엔 노이즈고, 반복 재기동을 의심할 때만 필요하다. */}
      <details className="mt-1">
        <summary className="text-muted text-xs cursor-pointer">자세히</summary>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-muted text-xs tabular-nums mt-1.5">
          <span>배치 {n(r.batches_done + f.batches_done)}회 완료</span>
          <span>워커 {r.workers}/{f.workers}</span>
          <span>세대 {r.generation}/{f.generation}</span>
          <span>재기동 {r.recycles + f.recycles}</span>
          <span>크래시 복구 {r.crash_restarts + f.crash_restarts}</span>
          <span>PID {r.pid ?? "—"}/{f.pid ?? "—"}</span>
          <span>갱신 {fmt(r.progress_at ?? r.updated_at)}</span>
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
