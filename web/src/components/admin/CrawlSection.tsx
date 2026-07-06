import { useEffect, useState } from "react";
import {
  cancelCrawl,
  fetchCountries,
  fetchCrawlStatus,
  fetchCrawlTarget,
  fetchIndustries,
  saveCrawlTarget,
  startCrawl,
} from "../../api";
import type { CrawlJob, CrawlTarget, Listed } from "../../types";
import { Square } from "lucide-react";
import { toast } from "sonner";
import { errMsg } from "../../format";
import { toCountryOpts } from "../../filterOptions";
import { MultiPicker, type PickerOption } from "../MultiPicker";
import { ErrorBox } from "../ErrorBox";
import { BTN_CONFIRM, BTN_REJECT } from "../../ui";
import { SECTION_H2, FIELD, FIELD_INLINE, INPUT_WIDE, CRAWL_TARGET, fmt } from "./shared";
import { ConfirmDialog } from "./ConfirmDialog";

const LISTED_OPTIONS: { value: Listed; label: string }[] = [
  { value: "unknown", label: "전체" },
  { value: "listed", label: "상장" },
  { value: "unlisted", label: "비상장" },
];

// 크롤 작업 상태 → 한글 라벨. cancelled 는 UI 액션명(중지)과 워딩을 맞춘다.
const CRAWL_STATUS_LABEL: Record<CrawlJob["status"], string> = {
  idle: "대기",
  running: "진행 중",
  done: "완료",
  failed: "실패",
  cancelled: "중지됨",
};

// 전 업종 CSV ↔ 빈값(=전체) 접기 — BE 는 빈 업종을 422 로 거부하므로(과도 발견 방지)
// 빈 선택은 전송 시 전 업종 CSV 로 확장하고, 표시할 땐 전 업종 일치 시 빈값으로 되돌려
// 발송/추출 섹션과 같은 '선택 안 함 = 전체' UI 를 유지한다.
function collapseAllIndustries(csv: string, opts: PickerOption[]): string {
  const picked = new Set(
    csv
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  );
  // 상위집합도 '전체'로 접는다 — 현 옵션을 모두 덮으면 의미상 전체이고, 어휘가 줄어든
  // 뒤 남은 구 라벨 잔여분도 다음 재저장에서 자연 정리된다(정확일치 요구 시 회복 불가).
  const isAll = opts.length > 0 && opts.every((o) => picked.has(o.value));
  return isAll ? "" : csv;
}

// KR 17개 시/도(표준 축약형) — BE region.KR_REGIONS 와 동일 목록·순서(#139).
// 지역 팬아웃은 KR 세그먼트 전용이라 조회 API 없이 고정 목록으로 둔다.
const KR_REGION_OPTS: PickerOption[] = [
  "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
  "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
].map((r) => ({ value: r, label: r }));

// 크롤 실행 — 국가·업종·상장여부·DB적재 타깃을 저장하고 즉시 크롤을 시작한다(#87 제거분
// 재통합). 타깃 저장을 함께 유지해 일일 스케줄러 타깃과 동기화 — 이후 '다음 크롤 예약'
// 확장도 이 저장 지점에 붙는다. 진행현황은 3초 폴링, 진행 중에는 '중지'로 협조적 취소.
export function CrawlTargetSection() {
  const [countryOpts, setCountryOpts] = useState<PickerOption[]>([]);
  const [industryOpts, setIndustryOpts] = useState<PickerOption[]>([]);
  const [countries, setCountries] = useState("");
  const [industries, setIndustries] = useState("");
  // KR 지역 팬아웃(#139) — 빈값=팬아웃 없음(BE 기본). 쿼터를 크게 늘리는 opt-in 이라
  // 업종의 '선택 안 함 = 전체' 확장을 따르지 않는다. 타깃 저장 계약에도 없음(실행 전용).
  const [regions, setRegions] = useState("");
  const [listed, setListed] = useState<Listed>("unknown");
  const [persist, setPersist] = useState(true);
  // 연속 크롤(#132) — 중지까지 라운드 반복. 쿼터를 계속 쓰므로 기본 꺼짐(명시적 opt-in).
  const [continuous, setContinuous] = useState(false);
  const [job, setJob] = useState<CrawlJob | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); // 실행/중지 요청 왕복 중(이중 클릭 방지)
  const [stopDialog, setStopDialog] = useState(false); // 중지 확인 다이얼로그

  const running = job?.status === "running";

  // 지역 픽커는 KR 이 크롤 범위에 있을 때만 노출 — BE 가 무시하는 값(KR 외 국가만 선택)을
  // 고르게 두면 오해만 남는다. 국가 미선택(=전체)은 KR 포함이므로 보인다. 숨김 중엔 전송도
  // 빈값으로 비워 잔존 선택이 몰래 나가는 걸 막고, 상태는 유지해 KR 재선택 시 복원한다.
  const krInScope =
    !countries.trim() || countries.split(",").some((c) => c.trim().toUpperCase() === "KR");

  const apply = (t: CrawlTarget) => {
    setCountries(t.countries);
    setIndustries(t.industries);
    setListed(t.listed);
    setPersist(t.persist);
  };

  useEffect(() => {
    let alive = true;
    Promise.all([fetchCrawlTarget(), fetchCountries(), fetchIndustries()])
      .then(([t, countryList, industryList]) => {
        if (!alive) return;
        setCountryOpts(toCountryOpts(countryList));
        setIndustryOpts(industryList);
        apply({ ...t, industries: collapseAllIndustries(t.industries, industryList) });
      })
      .catch((e) => alive && setErr(errMsg(e)));
    // 현황은 별도 조회 — 새로고침 시 진행 중이거나 최근 종료된 크롤을 이어서 보여주되,
    // 이 조회가 실패해도 폼(타깃·픽커) 로딩은 살린다(Promise.all 결합 회피).
    fetchCrawlStatus()
      .then((s) => alive && s.status !== "idle" && setJob(s))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  // 진행 중이면 3초마다 현황 폴링. 종료 상태가 되면 인터벌 해제.
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => {
      fetchCrawlStatus()
        .then(setJob)
        .catch((e) => setErr(errMsg(e)));
    }, 3000);
    return () => clearInterval(timer);
  }, [running]);

  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      // 빈 선택(=전체)은 전 업종 CSV 로 확장해 전송(BE 는 빈 업종 422).
      const inds = industries.trim() || industryOpts.map((o) => o.value).join(",");
      // 타깃 저장 → 즉시 실행. 저장이 스케줄러 타깃(=다음 크롤)도 갱신한다.
      const saved = await saveCrawlTarget({
        countries: countries.trim(),
        industries: inds,
        listed,
        persist,
      });
      apply({ ...saved, industries: collapseAllIndustries(saved.industries, industryOpts) });
      setJob(
        await startCrawl({
          countries: countries.trim(),
          industries: inds,
          listed,
          persist,
          continuous,
          regions: krInScope ? regions.trim() : "",
        }),
      );
      // 시작 피드백 — 휘발성 정보라 인라인 문구 대신 토스트(자동 소멸).
      toast.success("크롤 실행 시작");
    } catch (e2) {
      setErr(errMsg(e2));
      // 409(이미 진행 중) 대비 — 현황을 받아 진행 패널·중지 버튼으로 복구하고, 복구가
      // 됐으면(=실제로 running) 에러 박스는 걷는다. running 이 아니면(422 등) 에러 유지.
      fetchCrawlStatus()
        .then((s) => {
          if (s.status === "running") {
            setJob(s);
            setErr(null);
          }
        })
        .catch(() => undefined);
    } finally {
      setBusy(false);
    }
  };

  // 협조적 취소 — 즉시 멈추지 않고 cancel_requested 로 표시, 폴링이 종료를 확인한다.
  const doStop = async () => {
    setBusy(true);
    setErr(null);
    try {
      setJob(await cancelCrawl());
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
      setStopDialog(false);
    }
  };

  // window.confirm → ConfirmDialog 전환 — 버튼 클릭 시 다이얼로그만 열고, 실제 취소는
  // doStop 에서 수행. disabled(busy||cancel_requested) 조건은 버튼 측이 이미 막는다.
  const stop = () => setStopDialog(true);

  return (
    <section>
      <h2 className={SECTION_H2}>크롤 실행</h2>
      {err && <ErrorBox>{err}</ErrorBox>}
      <ConfirmDialog
        open={stopDialog}
        title="진행 중인 크롤을 중지할까요?"
        danger
        confirmLabel="중지"
        busy={busy}
        busyLabel="중지 요청 중…"
        onConfirm={() => void doStop()}
        onCancel={() => setStopDialog(false)}
      >
        <p className="m-0 text-muted text-sm">처리된 분은 보존됩니다</p>
      </ConfirmDialog>
      <form className={CRAWL_TARGET} onSubmit={(e) => void run(e)}>
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
        <div className={FIELD}>
          <span>
            업종 <span className="text-muted">(선택 안 함 = 전체)</span>
          </span>
          <MultiPicker
            options={industryOpts}
            value={industries}
            onChange={setIndustries}
            placeholder="업종 검색 (예: 건설, construction)"
            emptyHint="전체 업종"
          />
        </div>
        {krInScope && (
          <div className={FIELD}>
            <span>
              지역 <span className="text-muted">(KR 전용 · 선택 시 지역별 검색 팬아웃)</span>
            </span>
            <MultiPicker
              options={KR_REGION_OPTS}
              value={regions}
              onChange={setRegions}
              placeholder="지역 검색 (예: 서울, 경기)"
              emptyHint="지역 팬아웃 없음(기본)"
            />
          </div>
        )}
        <label className={FIELD}>
          상장여부
          <select
            className={INPUT_WIDE}
            value={listed}
            onChange={(e) => setListed(e.target.value as Listed)}
          >
            {LISTED_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        {/* DB적재 체크박스는 '저장 시 DB에 넣을지' — 저장 동작의 옵션이라 상장여부(필터)가
            아니라 크롤 실행 버튼과 한 그룹으로 묶는다. */}
        <div className="flex flex-col gap-2">
          <label className={FIELD_INLINE}>
            <input
              type="checkbox"
              checked={persist}
              onChange={(e) => setPersist(e.target.checked)}
            />
            DB 적재(검증 큐로)
          </label>
          <label className={FIELD_INLINE}>
            <input
              type="checkbox"
              checked={continuous}
              onChange={(e) => setContinuous(e.target.checked)}
            />
            연속 실행(중지까지 반복)
          </label>
          <div className="flex gap-2 mt-0.5">
            <button
              className={BTN_CONFIRM}
              type="submit"
              // 빈 선택은 전 업종 확장에 옵션 목록이 필요 — 미로드 상태만 잠깐 막는다.
              disabled={busy || running || (!industries.trim() && industryOpts.length === 0)}
            >
              {busy || running ? "실행 중…" : "크롤 실행"}
            </button>
            {running && (
              <button
                className={BTN_REJECT}
                type="button"
                // 중지 요청 후엔 비활성 — BE 가 멈출 때까지 running 이 유지되므로 재클릭(다이얼로그
                // 반복)을 막는다.
                disabled={busy || job?.cancel_requested}
                onClick={() => void stop()}
              >
                <span className="inline-flex items-center gap-1">
                  중지 <Square size={14} aria-hidden />
                </span>
              </button>
            )}
          </div>
        </div>
      </form>
      {job && job.status !== "idle" && <CrawlProgress job={job} />}
    </section>
  );
}

// 크롤 진행현황 패널 — 상태·세그먼트 진행바·발견/처리/저장 카운터(3초 폴링 반영).
function CrawlProgress({ job }: { job: CrawlJob }) {
  const total = job.segments_total || 0;
  const done = job.segments_done || 0;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const stopping = job.status === "running" && job.cancel_requested;
  return (
    <div className="mt-3 p-3 border border-line rounded-md bg-panel">
      {/* 상태 헤더 — 주요 상태 강조(text-ink), 보조 정보(연속·중지요청·트리거)는 muted */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 my-1">
        <span className="font-semibold text-ink text-sm">
          상태: {CRAWL_STATUS_LABEL[job.status]}
        </span>
        {/* 연속 모드 — 카운터는 현재 라운드 기준이라 몇 회차인지 함께 보여준다. */}
        {job.mode === "continuous" && (
          <span className="text-muted text-xs">
            연속 ·{" "}
            {job.status === "running"
              ? `라운드 ${job.rounds_done + 1}회차 진행 중`
              : job.rounds_done > 0
                ? `라운드 ${job.rounds_done}회 완료`
                : "첫 라운드에서 종료"}
          </span>
        )}
        {stopping && <span className="text-muted text-xs">· 중지 요청됨…</span>}
        {job.triggered_by && <span className="text-muted text-xs">· {job.triggered_by}</span>}
      </div>
      {/* 진행바 — bg-line 트랙 + bg-ok 채움. 기존 토큰만 사용 */}
      <div
        className="w-full h-1.5 rounded-full bg-line my-2 overflow-hidden"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className="h-full bg-ok rounded-full transition-[width]" style={{ width: `${pct}%` }} />
      </div>
      {/* 카운터 — tabular-nums 으로 숫자 흔들림 방지, 항목별 gap 으로 가독성 확보 */}
      <div className="flex flex-wrap gap-x-4 text-muted text-xs tabular-nums my-1">
        <span>세그먼트 {done}/{total} ({pct}%)</span>
        <span>발견 <span className="text-ink">{job.discovered}</span></span>
        <span>처리 <span className="text-ink">{job.enriched}</span></span>
        <span>저장(실존) <span className="text-ink">{job.saved}</span></span>
      </div>
      {job.error && <ErrorBox>{job.error}</ErrorBox>}
      {job.finished_at && <p className="text-muted text-xs my-1">종료: {fmt(job.finished_at)}</p>}
    </div>
  );
}
