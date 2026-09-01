import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { fetchDashboardSummary } from "../api";
import { errMsg } from "../format";
import { useQueueFilterOpts } from "../filterOptions";
import type { PickerOption } from "./MultiPicker";
import type { DashboardSummary, ReviewStatus } from "../types";
import { ErrorBox } from "./ErrorBox";
import { BTN } from "../ui";
import { SECTION_H2 } from "./admin/shared";

// 대시보드 숫자 → 전체 큐 되짚기 파라미터. 국가·업종 어휘가 큐 필터와 동일해(BE 계약)
// 값을 그대로 넘길 수 있다. 지정 안 한 축은 호출부가 비운다(= 전체).
export interface QueueJump {
  status?: ReviewStatus | "";
  country?: string;
  industry?: string;
}

const n = (v: number): string => v.toLocaleString();

// 분모 0 방어 — 비율 표기는 전부 이 함수를 거친다.
function pct(part: number, whole: number): number {
  return whole > 0 ? (part / whole) * 100 : 0;
}

// 비율 문구 — 정수 반올림하되 0 이 아닌 값이 "0%"로 사라지지 않게 <1% 는 그대로 밝힌다.
function pctText(part: number, whole: number): string {
  const p = pct(part, whole);
  if (p === 0) return "0%";
  return p < 1 ? "<1%" : `${Math.round(p)}%`;
}

// 분포 리스트 기본 노출 개수 — 국가는 71개국까지 늘 수 있어(#374) 상위만 펼쳐 보여주고
// 나머지는 '기타'로 접는다. 전량은 토글로.
const TOP_N = 8;

// 진입 시 1회 조회 + 수동 새로고침. **폴링하지 않는다** — 원장 group by 를 여러 번 도는
// 집계라 BE docstring 이 폴링을 금지한다(#378 계약). 그래서 화면 상단에 기준 시각을 박아
// "지금 값"이 아니라 "이 시각의 스냅샷"임을 드러낸다.
export function Dashboard({ onJumpToQueue }: { onJumpToQueue: (jump: QueueJump) => void }) {
  const [data, setData] = useState<DashboardSummary | null>(null);
  const [at, setAt] = useState<Date | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // 국가 코드(ISO2) → 한글 라벨. 실패해도 코드 그대로 표시되므로 화면은 살아 있다.
  const { countryOpts } = useQueueFilterOpts();

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      setData(await fetchDashboardSummary());
      setAt(new Date());
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h2 className={`${SECTION_H2} mb-0`}>보유 데이터 현황</h2>
        <div className="flex items-center gap-2.5 text-muted text-xs">
          <span className="tabular-nums">
            {at ? `기준 ${at.toLocaleTimeString()}` : "불러오는 중…"}
          </span>
          <button
            className={`${BTN} py-1! px-2.5! text-xs`}
            type="button"
            disabled={loading}
            onClick={() => void load()}
            title="집계를 다시 계산합니다 (대형 조회라 자동 갱신하지 않습니다)"
          >
            <span className="inline-flex items-center gap-1">
              <RefreshCw size={12} className={loading ? "animate-spin" : undefined} aria-hidden />
              새로고침
            </span>
          </button>
        </div>
      </div>

      {err && <ErrorBox>{err}</ErrorBox>}

      {!data ? (
        <LoadingBlocks />
      ) : (
        <>
          <KpiRow data={data} onJumpToQueue={onJumpToQueue} />
          <div className="grid gap-4 grid-cols-1 xl:grid-cols-2">
            <LedgerCard data={data} />
            <QueueCard data={data} onJumpToQueue={onJumpToQueue} />
          </div>
          <div className="grid gap-4 grid-cols-1 xl:grid-cols-2">
            <DistCard
              title="국가별 회사"
              total={data.companies.total}
              rows={data.companies.by_country.map((c) => ({
                key: c.country,
                ...countryLabel(c.country, countryOpts),
                n: c.n,
                // 국가 미상('')은 큐 필터로 되짚을 수 없다(필터 어휘에 '미상' 토큰 없음).
                jump: c.country ? { country: c.country } : null,
              }))}
              otherUnit="개국"
              onJumpToQueue={onJumpToQueue}
            />
            <DistCard
              title="업종별 회사"
              total={data.companies.total}
              rows={data.companies.by_industry.map((i) => ({
                key: i.industry,
                label: i.industry,
                n: i.n,
                jump: { industry: i.industry },
              }))}
              otherUnit="개 업종"
              onJumpToQueue={onJumpToQueue}
            />
          </div>
        </>
      )}
    </section>
  );
}

// ISO2 → 표시 라벨(+코드 병기). 미등록국은 BE 가 원문을 그대로 주므로 그대로 쓰고,
// 빈값은 '국가 미상'(큐 재고 화면과 같은 어휘).
function countryLabel(iso2: string, opts: PickerOption[]): { label: string; code?: string } {
  if (!iso2) return { label: "국가 미상" };
  const hit = opts.find((o) => o.value === iso2);
  return hit ? { label: hit.label, code: iso2 } : { label: iso2 };
}

const CARD = "p-4 border border-line rounded-lg bg-panel";

function LoadingBlocks() {
  return (
    <div className="flex flex-col gap-4" aria-busy="true" aria-label="현황 불러오는 중">
      <div className="grid gap-3 grid-cols-2 lg:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className={`${CARD} h-[86px] animate-pulse`} />
        ))}
      </div>
      <div className="grid gap-4 grid-cols-1 xl:grid-cols-2">
        <div className={`${CARD} h-[168px] animate-pulse`} />
        <div className={`${CARD} h-[168px] animate-pulse`} />
      </div>
    </div>
  );
}

// KPI 타일 — 규모를 먼저 읽히는 자리. 클릭 가능한 타일은 큐 필터로 이어진다.
function KpiRow({
  data,
  onJumpToQueue,
}: {
  data: DashboardSummary;
  onJumpToQueue: (jump: QueueJump) => void;
}) {
  const { companies, queue } = data;
  const pending = queue.pending_unclaimed + queue.pending_claimed;
  return (
    <div className="grid gap-3 grid-cols-2 lg:grid-cols-4">
      <Kpi label="보유 기업" value={companies.total} sub="실존 확인·DB 등재분" />
      <Kpi
        label="이메일 보유"
        value={companies.with_email}
        sub={`전체의 ${pctText(companies.with_email, companies.total)}`}
      />
      <Kpi
        label="검증 확정"
        value={queue.confirmed}
        sub="엑셀 추출 대상"
        onClick={() => onJumpToQueue({ status: "confirmed" })}
      />
      <Kpi
        label="검증 대기"
        value={pending}
        sub={`미배정 ${n(queue.pending_unclaimed)} · 작업 중 ${n(queue.pending_claimed)}`}
        onClick={() => onJumpToQueue({ status: "pending" })}
      />
    </div>
  );
}

function Kpi({
  label,
  value,
  sub,
  onClick,
}: {
  label: string;
  value: number;
  sub: string;
  onClick?: () => void;
}) {
  const body = (
    <>
      <span className="text-muted text-xs">{label}</span>
      <span className="text-[26px] leading-tight tabular-nums">{n(value)}</span>
      <span className="text-muted text-xs">{sub}</span>
    </>
  );
  const cls = `${CARD} flex flex-col gap-0.5 text-left`;
  return onClick ? (
    <button
      type="button"
      className={`${cls} cursor-pointer transition-colors hover:border-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-canvas`}
      onClick={onClick}
      title="전체 큐에서 보기"
    >
      {body}
    </button>
  ) : (
    <div className={cls}>{body}</div>
  );
}

// 원장 4분면 — total = 승격 + 도메인확정 미승격 + 도메인 미확정 + 흡수분(BE 불변식).
// 앞 3칸은 계열색(진행 단계), 흡수분은 중립색: dedup 으로 다른 키에 대표되는 행이라
// 승격 백로그가 아니다(같은 색 계열로 칠하면 남은 일감으로 오독된다).
function LedgerCard({ data }: { data: DashboardSummary }) {
  const l = data.ledger;
  const parts = [
    { key: "promoted", label: "승격 완료", v: l.promoted, color: "var(--color-chart-1)" },
    {
      key: "domained",
      label: "도메인 확정·미승격",
      v: l.domained_unpromoted,
      color: "var(--color-chart-2)",
      hint: "승격 백필 대상",
    },
    {
      key: "undomained",
      label: "도메인 미확정",
      v: l.undomained_unpromoted,
      color: "var(--color-chart-3)",
      hint: "도메인 해석 대기",
    },
    {
      key: "absorbed",
      label: "중복 흡수",
      v: l.absorbed,
      // 계열색이 아닌 중립 — 다만 경계선색(--color-line)까지 내리면 패널 배경에 묻혀
      // '없는 칸'처럼 보인다. 눈에 덜 띄되 읽히는 회색(muted)까지만 내린다.
      color: "var(--color-muted)",
      hint: "다른 키로 대표됨 — 승격 대상 아님",
    },
  ];
  const backlog = l.domained_unpromoted + l.undomained_unpromoted;

  return (
    <div className={CARD}>
      <div className="flex items-baseline justify-between gap-2 mb-3">
        <h3 className="text-sm font-semibold m-0">발견 원장</h3>
        <span className="text-muted text-xs tabular-nums">전체 {n(l.total)}건</span>
      </div>

      {/* 세그먼트 사이 2px 배경 틈 — 인접 색이 맞닿아 경계가 뭉개지는 것을 막는다.
          폭은 flex-grow 비율이라 틈이 총폭을 넘치게 하지 않는다. 값 0 인 칸은 렌더하지
          않는다(폭 0 세그먼트가 틈만 차지해 눈금처럼 보이는 현상 방지). */}
      <div
        className="flex gap-[2px] h-3 w-full mb-3"
        role="img"
        aria-label={`발견 원장 ${n(l.total)}건 구성 — ${parts
          .map((p) => `${p.label} ${n(p.v)}건`)
          .join(", ")}`}
      >
        {parts
          .filter((p) => p.v > 0)
          .map((p) => (
            <div
              key={p.key}
              className="rounded-sm min-w-[2px]"
              style={{ flexGrow: p.v, flexBasis: 0, background: p.color }}
              title={`${p.label} ${n(p.v)}건 (${pctText(p.v, l.total)})`}
            />
          ))}
      </div>

      {/* 범례 = 표 역할 겸용 — 색만으로 식별하지 않도록 라벨·값·비율을 모두 글자로 둔다. */}
      <ul className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1.5 m-0 p-0 list-none">
        {parts.map((p) => (
          <li key={p.key} className="flex items-baseline gap-2 text-[13px]">
            <span
              className="inline-block w-2.5 h-2.5 rounded-sm shrink-0 translate-y-px"
              style={{ background: p.color }}
              aria-hidden
            />
            <span className="text-muted">{p.label}</span>
            <span className="ml-auto tabular-nums">{n(p.v)}</span>
            <span className="text-muted tabular-nums w-10 text-right">{pctText(p.v, l.total)}</span>
          </li>
        ))}
      </ul>

      <p className="text-muted text-xs mt-3 mb-0">
        승격 백로그 <span className="text-ink tabular-nums">{n(backlog)}</span>건 (흡수분 제외)
        {/* 원장 승격 수와 보유 기업 수는 등치가 아니다 — 원장 없는 고아 company 가 있으면
            회사 쪽이 더 크다(BE 계약 주석). 두 수가 어긋나 보일 때 버그로 오인되지 않게 명시. */}
        {data.companies.total !== l.promoted && (
          <>
            {" · "}보유 기업 {n(data.companies.total)}건과의 차이는 원장 없는 회사(가져오기분)
          </>
        )}
      </p>
    </div>
  );
}

// 검증 큐 — 상태별 카운트. pending 은 점유 여부로 갈린다. '작업 중'은 전체 큐 조회에서
// 서버가 제외하므로(다른 직원 점유분) 되짚기를 걸지 않는다.
function QueueCard({
  data,
  onJumpToQueue,
}: {
  data: DashboardSummary;
  onJumpToQueue: (jump: QueueJump) => void;
}) {
  const q = data.queue;
  const total = q.pending_unclaimed + q.pending_claimed + q.confirmed + q.rejected;
  const rows: {
    key: string;
    label: string;
    v: number;
    color: string;
    jump: QueueJump | null;
    hint?: string;
  }[] = [
    {
      key: "unclaimed",
      label: "미배정 대기",
      v: q.pending_unclaimed,
      color: "var(--color-chart-3)",
      jump: { status: "pending" },
    },
    {
      key: "claimed",
      label: "작업 중(점유)",
      v: q.pending_claimed,
      color: "var(--color-chart-2)",
      jump: null,
      hint: "직원이 점유한 건 — 전체 큐 목록에는 나오지 않습니다",
    },
    {
      key: "confirmed",
      label: "확정",
      v: q.confirmed,
      color: "var(--color-chart-1)",
      jump: { status: "confirmed" },
    },
    {
      key: "rejected",
      label: "거부",
      v: q.rejected,
      // 거부는 '처리 끝난 탈락분' — 성과(확정)·잔여(대기)와 나란히 강조할 값이 아니라 중립.
      color: "var(--color-muted)",
      jump: { status: "rejected" },
    },
  ];

  return (
    <div className={CARD}>
      <div className="flex items-baseline justify-between gap-2 mb-3">
        <h3 className="text-sm font-semibold m-0">검증 큐</h3>
        <span className="text-muted text-xs tabular-nums">전체 {n(total)}건</span>
      </div>
      <ul className="flex flex-col gap-2 m-0 p-0 list-none">
        {rows.map(({ key, label, v, color, jump, hint }) => (
          <li key={key}>
            <BarRow
              label={label}
              value={v}
              ratio={pct(v, total)}
              color={color}
              title={`${label} ${n(v)}건 (${pctText(v, total)})${hint ? ` — ${hint}` : ""}`}
              onClick={jump ? () => onJumpToQueue(jump) : undefined}
            />
          </li>
        ))}
      </ul>
    </div>
  );
}

interface DistRow {
  key: string;
  label: string;
  code?: string;
  n: number;
  jump: QueueJump | null;
}

// 분포(국가·업종) — 단일 계열이라 색은 한 가지, 길이만 값을 나른다. 막대 길이는 1위 대비
// 정규화(전체 대비로 하면 상위 하나가 길고 나머지가 실선처럼 붙어 비교가 안 된다).
function DistCard({
  title,
  total,
  rows,
  otherUnit,
  onJumpToQueue,
}: {
  title: string;
  /** 회사 총계 — 각 행의 '전체 대비 비율' 툴팁 분모. */
  total: number;
  rows: DistRow[];
  otherUnit: string;
  onJumpToQueue: (jump: QueueJump) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? rows : rows.slice(0, TOP_N);
  const rest = rows.slice(TOP_N);
  const restSum = rest.reduce((s, r) => s + r.n, 0);
  const max = rows.length ? Math.max(...rows.map((r) => r.n)) : 0;

  return (
    <div className={CARD}>
      <div className="flex items-baseline justify-between gap-2 mb-3">
        <h3 className="text-sm font-semibold m-0">{title}</h3>
        <span className="text-muted text-xs tabular-nums">{n(rows.length)}종</span>
      </div>
      {rows.length === 0 ? (
        <p className="text-muted text-[13px] m-0">데이터 없음</p>
      ) : (
        <>
          <ul className="flex flex-col gap-2 m-0 p-0 list-none">
            {shown.map(({ key, label, code, n: count, jump }) => (
              <li key={key}>
                <BarRow
                  label={label}
                  code={code}
                  value={count}
                  ratio={pct(count, max)}
                  color="var(--color-chart-2)"
                  title={`${label} ${n(count)}개사 (전체의 ${pctText(count, total)})${
                    jump ? " — 클릭하면 전체 큐에서 봅니다" : ""
                  }`}
                  onClick={jump ? () => onJumpToQueue(jump) : undefined}
                />
              </li>
            ))}
          </ul>
          {rest.length > 0 && (
            <div className="flex items-center justify-between gap-2 mt-2.5 text-xs text-muted">
              <span className="tabular-nums">
                {expanded ? `${n(rows.length)}종 전체 표시 중` : `기타 ${n(rest.length)}${otherUnit} 합계 ${n(restSum)}`}
              </span>
              <button
                className={`${BTN} py-0.5! px-2! text-xs`}
                type="button"
                onClick={() => setExpanded((v) => !v)}
              >
                {expanded ? "접기" : "전체 보기"}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

// 라벨 + 막대 + 값 한 줄. 클릭 가능하면 버튼으로 감싸 되짚기(큐 필터)로 잇는다.
function BarRow({
  label,
  code,
  value,
  ratio,
  color,
  title,
  onClick,
}: {
  label: string;
  code?: string;
  value: number;
  /** 막대 폭(%) — 호출부가 정규화 기준(최댓값/전체)을 정한다. */
  ratio: number;
  color: string;
  title: string;
  onClick?: () => void;
}) {
  const inner = (
    <>
      <span className="w-[132px] shrink-0 truncate text-[13px]" title={label}>
        {label}
        {code && <span className="text-muted ml-1 text-xs">{code}</span>}
      </span>
      <span className="flex-1 h-2.5 rounded-sm bg-canvas overflow-hidden">
        <span
          className="block h-full rounded-sm"
          style={{ width: `${Math.max(ratio, value > 0 ? 1 : 0)}%`, background: color }}
        />
      </span>
      <span className="w-[72px] shrink-0 text-right tabular-nums text-[13px]">{n(value)}</span>
    </>
  );
  const cls = "flex items-center gap-2.5 w-full text-left";
  return onClick ? (
    <button
      type="button"
      className={`${cls} cursor-pointer rounded-sm -mx-1 px-1 py-0.5 transition-colors hover:bg-line focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-panel`}
      onClick={onClick}
      title={title}
    >
      {inner}
    </button>
  ) : (
    <div className={`${cls} -mx-1 px-1 py-0.5`} title={title}>
      {inner}
    </div>
  );
}
