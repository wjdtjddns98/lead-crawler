import { memo, useCallback, useMemo, useRef, useState, type MouseEvent } from "react";
import { ArrowDown, ArrowUp, ChevronsUpDown, ExternalLink, FileText } from "lucide-react";
import type { ReviewItem } from "../types";
import { BTN_CONFIRM, BTN_REJECT, EMPTY, LINK_FOCUS, TD, TH } from "../ui";
import { EmailBadge, StatusBadge } from "./StatusBadge";
import { SiteExplorer, type SiteTab } from "./SiteExplorer";

// 상태별 행 좌측 색 띠(첫 칸에 inset 그림자로 표현) — 한눈에 스캔.
const STRIPE: Record<ReviewItem["status"], string> = {
  pending: "shadow-[inset_3px_0_0_var(--color-warn)]",
  confirmed: "shadow-[inset_3px_0_0_var(--color-ok)]",
  rejected: "shadow-[inset_3px_0_0_var(--color-danger)]",
};

// 컬럼 폭(table-auto) — 고정 % 대신 핵심 컬럼만 최소폭을 주고 나머지는 내용 길이에
// 맞춰 늘어난다. 사이트는 줄바꿈 금지(whitespace-nowrap)로 '↗ 도메인 / 📝 문의폼'이
// 4줄로 접히지 않고 문자열 길이만큼 칸이 넓어진다.
const COL_W = [
  "min-w-[120px]", // 업체명
  "", // 국가
  "", // 구분
  "min-w-[240px]", // 이메일 후보(편집 입력 포함 — 넓게 유지)
  "", // 메일
  "whitespace-nowrap", // 사이트(내용 길이만큼 유동 확장)
  "", // 상태
  "min-w-[120px]", // 액션(버튼 2개 가로 유지)
];
const HEADERS = ["업체명", "국가", "구분", "이메일 후보", "메일", "사이트", "상태", "액션"];

// 상태 정렬 순서(pending 먼저 = 처리해야 할 것 위로). 알파벳순보다 업무 흐름에 맞다.
const STATUS_RANK: Record<ReviewItem["status"], number> = {
  pending: 0,
  confirmed: 1,
  rejected: 2,
};

// 컬럼별 정렬 키. 없는 인덱스(이메일 후보·메일·사이트·액션)는 정렬 불가
// — 메일/사이트는 뱃지·링크라 자연스러운 대소 순서가 없어 제외.
const SORT_KEY: Record<number, (it: ReviewItem) => string | number> = {
  0: (it) => it.name,
  1: (it) => it.country,
  2: (it) => it.industry,
  6: (it) => STATUS_RANK[it.status],
};

type Sort = { col: number; dir: "asc" | "desc" };

interface Props {
  items: ReviewItem[];
  busyIds: Set<string>;
  doneCount: number; // 이번 세션 처리 건수(진행률 바 분자)
  remaining: number; // 남은 대기 건수(필터 반영 total) — 분모 = doneCount + remaining
  // 성공(처리 완료) 시 true 를 resolve — 팝업에서 '성공해야 다음 행 전진' 판단에 쓴다.
  onConfirm: (id: string, selected?: string) => Promise<boolean>;
  onReject: (id: string) => Promise<boolean>;
}

// 크롤된 신뢰불가 URL 의 스킴을 검증한다 — http(s) 만 허용(javascript:/data: 등 XSS 차단).
function safeHref(url: string | null): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

// URL 에서 표시용 호스트(도메인)를 뽑는다(www. 제거). 실패 시 원문.
function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

// 불리언/널 3-state 를 O/X/— 로 표시.
function tri(v: boolean | null): string {
  if (v === null) return "—";
  return v ? "O" : "X";
}

// 사이트 링크 클릭 — 수정키(Ctrl/Cmd/Shift)·가운데 클릭은 브라우저 기본(새 탭)을 살리고,
// 평범한 좌클릭만 가로채 페이지 내 미리보기 창을 연다.
function openOrTab(e: MouseEvent, open: () => void) {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
  e.preventDefault();
  open();
}

// ISO8601 → 월-일 시:분(처리 시각 축약 표기). 파싱 실패면 빈 문자열.
function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

interface RowProps {
  item: ReviewItem;
  busy: boolean;
  choice: string | undefined;
  onPick: (id: string, value: string) => void;
  onConfirm: (id: string, selected?: string) => void;
  onReject: (id: string) => void;
  onOpen: (id: string, tab: SiteTab) => void;
}

// 행은 memo — item·busy·choice 가 같으면 리렌더를 건너뛴다(한 행 처리 시 나머지 행이
// 불필요하게 다시 그려지지 않음). 핸들러 동일성은 비교에서 제외한다: 부모가 인라인
// 핸들러를 매 렌더 새로 만들어도 논리는 동일하고, 행은 item 이 바뀔 때 최신 핸들러로
// 갱신되므로 안전하다(필터 변경 시 item 객체가 새로 와 자연히 재렌더).
const QueueRow = memo(
  function QueueRow({ item, busy, choice, onPick, onConfirm, onReject, onOpen }: RowProps) {
    const done = item.status !== "pending";
    const href = safeHref(item.homepage);
    const formHref = safeHref(item.form);
    return (
      <tr className={`hover:bg-white/[0.03] ${done ? "opacity-60" : ""}`}>
        <td className={`${TD} ${COL_W[0]} font-semibold ${STRIPE[item.status]}`} title={item.name}>
          {item.name}
        </td>
        <td className={`${TD} ${COL_W[1]}`}>{item.country}</td>
        <td className={`${TD} ${COL_W[2]}`}>{item.industry}</td>
        <td className={`${TD} ${COL_W[3]} font-mono text-[13px]`}>
          {item.candidates.length > 1 && (
            <div className="flex flex-col gap-1">
              {item.candidates.map((c) => (
                <label
                  key={c.value}
                  className="flex items-start gap-1.5 cursor-pointer"
                  title={c.value}
                >
                  <input
                    type="radio"
                    className="cursor-pointer flex-none mt-0.5"
                    name={`sel-${item.id}`}
                    checked={choice === c.value}
                    disabled={done}
                    onChange={() => onPick(item.id, c.value)}
                  />
                  <span className="font-mono [overflow-wrap:anywhere]">{c.value}</span>
                  <EmailBadge status={c.email_status} />
                </label>
              ))}
            </div>
          )}
          {/* 확정 전 이메일 직접 수정/입력 — 후보 선택값을 채우되 사람이 덮어쓸 수 있다.
              폼만 있던(후보 0) 행엔 새 이메일 추가도 가능. */}
          <input
            className="w-full mt-1 bg-canvas border border-line text-ink font-mono text-xs py-1 px-1.5 rounded focus:outline-none focus:border-accent disabled:opacity-50"
            type="email"
            value={choice ?? ""}
            disabled={done}
            placeholder="이메일 직접 입력/수정"
            onChange={(e) => onPick(item.id, e.target.value)}
            title="확정 전 이메일을 수정하거나 직접 입력할 수 있습니다"
          />
        </td>
        <td className={`${TD} ${COL_W[4]} text-xs`}>
          <EmailBadge status={item.email_status} />
          <span className="text-muted ml-2 whitespace-nowrap">MX {tri(item.email_mx)}</span>
          <span className="text-muted ml-2 whitespace-nowrap">SMTP {tri(item.email_smtp)}</span>
        </td>
        <td className={`${TD} ${COL_W[5]}`}>
          {href ? (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              className={`${LINK_FOCUS} ${item.site_alive ? "text-accent" : "text-muted line-through"}`}
              title={item.site_alive ? "클릭: 미리보기 창 (Ctrl+클릭: 새 탭)" : "사이트 미응답"}
              onClick={(e) => openOrTab(e, () => onOpen(item.id, "home"))}
            >
              <span className="inline-flex items-center gap-1">
                <ExternalLink size={13} className="flex-none" aria-hidden /> {hostOf(href)}
              </span>
            </a>
          ) : (
            <span className="text-muted">—</span>
          )}
          {formHref && (
            <div>
              <a
                href={formHref}
                target="_blank"
                rel="noreferrer"
                className={`${LINK_FOCUS} text-accent`}
                title="클릭: 미리보기 창 (Ctrl+클릭: 새 탭)"
                onClick={(e) => openOrTab(e, () => onOpen(item.id, "form"))}
              >
                <span className="inline-flex items-center gap-1">
                  <FileText size={13} className="flex-none" aria-hidden /> 문의폼
                </span>
              </a>
            </div>
          )}
        </td>
        <td className={`${TD} ${COL_W[6]}`}>
          <StatusBadge status={item.status} />
          {item.assignee && (
            <span className="text-muted text-xs" title={item.reviewed_at ?? undefined}>
              {" · "}
              {item.assignee}
              {item.reviewed_at && (
                <small className="text-muted text-[11px] ml-0.5"> {fmtTime(item.reviewed_at)}</small>
              )}
            </span>
          )}
        </td>
        <td className={`${TD} ${COL_W[7]}`}>
          <div className="flex gap-1.5 flex-wrap">
            <button
              className={BTN_CONFIRM}
              disabled={busy || item.status === "confirmed"}
              onClick={() => onConfirm(item.id, choice?.trim() ? choice.trim() : undefined)}
            >
              확정
            </button>
            <button
              className={BTN_REJECT}
              disabled={busy || item.status === "rejected"}
              onClick={() => onReject(item.id)}
            >
              거부
            </button>
          </div>
        </td>
      </tr>
    );
  },
  (prev, next) =>
    prev.item === next.item && prev.busy === next.busy && prev.choice === next.choice,
);

// 검증 큐 표 — 회사/이메일 후보(다중 선택)/메일 검증 신호/상태/액션.
export function QueueTable({ items, busyIds, doneCount, remaining, onConfirm, onReject }: Props) {
  // 행별 선택(라디오) — 서버 selected 를 기본값으로, 사용자가 바꾸면 덮어쓴다.
  const [picked, setPicked] = useState<Record<string, string>>({});
  const onPick = useCallback((id: string, value: string) => {
    setPicked((p) => ({ ...p, [id]: value }));
  }, []);

  // 컬럼 정렬 — 같은 컬럼 재클릭 시 asc↔desc 토글, 3번째 클릭이면 해제(원본 순서 복귀).
  const [sort, setSort] = useState<Sort | null>(null);
  const onSort = useCallback((col: number) => {
    setSort((s) => {
      if (s?.col !== col) return { col, dir: "asc" };
      if (s.dir === "asc") return { col, dir: "desc" };
      return null;
    });
  }, []);
  const sorted = useMemo(() => {
    if (!sort) return items;
    const key = SORT_KEY[sort.col];
    const sign = sort.dir === "asc" ? 1 : -1;
    return [...items].sort((a, b) => {
      const va = key(a);
      const vb = key(b);
      const c =
        typeof va === "number" && typeof vb === "number"
          ? va - vb
          : String(va).localeCompare(String(vb), "ko");
      return c * sign;
    });
  }, [items, sort]);

  // 사이트 미리보기 창 — 열린 행 id 와 초기 탭(홈/문의폼). 닫히면 null.
  const [open, setOpen] = useState<{ id: string; tab: SiteTab } | null>(null);
  const onOpen = useCallback((id: string, tab: SiteTab) => setOpen({ id, tab }), []);

  // 항상 최신 items 를 가리키는 ref — 전진은 처리 await(목록 리필) 후에 일어나므로
  // 콜백 클로저의 옛 items 가 아닌 갱신된 목록에서 다음 행을 찾아야 한다.
  // 표시 순서(sorted)를 가리킨다 — 팝업 '다음 행 전진'이 화면에 보이는 순서를 따르도록.
  const itemsRef = useRef(sorted);
  itemsRef.current = sorted;

  // 팝업에서 완료(확정/거부) 시 성공했을 때만 다음 행으로 전진. 처리 전 목록에서 '다음
  // pending(홈/폼 링크 보유) 행' id 를 미리 잡아두고(처리 후엔 방금 행이 빠져 위치 기준이
  // 무너짐), await 후 그 행이 아직 pending 이면 연다. 없으면 닫는다. 실패면 현재 항목 유지.
  const advanceAfter = useCallback(
    async (id: string, run: () => Promise<boolean>) => {
      const before = itemsRef.current;
      const idx = before.findIndex((it) => it.id === id);
      const nextId =
        before
          .slice(idx + 1)
          .find((it) => it.status === "pending" && (safeHref(it.homepage) || safeHref(it.form)))
          ?.id ?? null;
      if (!(await run())) return; // 실패(400/409 등) — 전진하지 않고 현재 항목·에러 유지
      const after = itemsRef.current;
      const target = nextId
        ? after.find((it) => it.id === nextId && it.status === "pending")
        : undefined;
      setOpen(target ? { id: target.id, tab: "home" } : null);
    },
    [],
  );
  const popupConfirm = useCallback(
    (id: string, selected?: string) => advanceAfter(id, () => onConfirm(id, selected)),
    [advanceAfter, onConfirm],
  );
  const popupReject = useCallback(
    (id: string) => advanceAfter(id, () => onReject(id)),
    [advanceAfter, onReject],
  );

  if (items.length === 0) {
    return <p className={EMPTY}>표시할 검증 항목이 없습니다.</p>;
  }
  // 처리·필터 변경으로 항목이 목록에서 빠지면 창도 자연히 닫힌다(find 결과 없음).
  const openItem = open ? items.find((it) => it.id === open.id) : undefined;
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse bg-panel border border-line rounded-lg overflow-hidden">
        <thead>
          <tr>
            {HEADERS.map((h, i) => {
              const sortable = i in SORT_KEY;
              const active = sort?.col === i;
              return (
                <th
                  key={h}
                  className={`${TH} ${COL_W[i]}`}
                  aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
                >
                  {sortable ? (
                    <button
                      type="button"
                      className={`${LINK_FOCUS} inline-flex items-center gap-1 uppercase tracking-[0.04em] ${active ? "text-ink" : "hover:text-ink"}`}
                      onClick={() => onSort(i)}
                      title="클릭하여 정렬 (오름차순 → 내림차순 → 해제)"
                    >
                      {h}
                      {active ? (
                        sort.dir === "asc" ? (
                          <ArrowUp size={12} className="flex-none" aria-hidden />
                        ) : (
                          <ArrowDown size={12} className="flex-none" aria-hidden />
                        )
                      ) : (
                        <ChevronsUpDown size={12} className="flex-none opacity-40" aria-hidden />
                      )}
                    </button>
                  ) : (
                    h
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((it) => (
            <QueueRow
              key={it.id}
              item={it}
              busy={busyIds.has(it.id)}
              choice={picked[it.id] ?? it.selected ?? it.candidates[0]?.value}
              onPick={onPick}
              onConfirm={onConfirm}
              onReject={onReject}
              onOpen={onOpen}
            />
          ))}
        </tbody>
      </table>
      {open && openItem && (
        <SiteExplorer
          item={openItem}
          doneCount={doneCount}
          remaining={remaining}
          tab={open.tab}
          choice={picked[openItem.id] ?? openItem.selected ?? openItem.candidates[0]?.value}
          busy={busyIds.has(openItem.id)}
          onTab={(tab) => setOpen({ id: openItem.id, tab })}
          onPick={onPick}
          onConfirm={popupConfirm}
          onReject={popupReject}
          onClose={() => setOpen(null)}
        />
      )}
    </div>
  );
}
