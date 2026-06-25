import { memo, useCallback, useState } from "react";
import type { ReviewItem } from "../types";
import { BTN_CONFIRM, BTN_REJECT, EMPTY, TD, TH } from "../ui";
import { EmailBadge, StatusBadge } from "./StatusBadge";

// 상태별 행 좌측 색 띠(첫 칸에 inset 그림자로 표현) — 한눈에 스캔.
const STRIPE: Record<ReviewItem["status"], string> = {
  pending: "shadow-[inset_3px_0_0_var(--color-warn)]",
  confirmed: "shadow-[inset_3px_0_0_var(--color-ok)]",
  rejected: "shadow-[inset_3px_0_0_var(--color-danger)]",
};

// 컬럼 폭(table-fixed 기준, 합 100%) — 업체명·이메일은 넓게, 단문 컬럼은 좁게.
const COL_W = ["w-[16%]", "w-[6%]", "w-[8%]", "w-[27%]", "w-[14%]", "w-[8%]", "w-[11%]", "w-[10%]"];
const HEADERS = ["업체명", "국가", "구분", "이메일 후보", "메일", "사이트", "상태", "액션"];

interface Props {
  items: ReviewItem[];
  busyIds: Set<string>;
  onConfirm: (id: string, selected?: string) => void;
  onReject: (id: string) => void;
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
}

// 행은 memo — item·busy·choice 가 같으면 리렌더를 건너뛴다(한 행 처리 시 나머지 행이
// 불필요하게 다시 그려지지 않음). 핸들러 동일성은 비교에서 제외한다: 부모가 인라인
// 핸들러를 매 렌더 새로 만들어도 논리는 동일하고, 행은 item 이 바뀔 때 최신 핸들러로
// 갱신되므로 안전하다(필터 변경 시 item 객체가 새로 와 자연히 재렌더).
const QueueRow = memo(
  function QueueRow({ item, busy, choice, onPick, onConfirm, onReject }: RowProps) {
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
              className={item.site_alive ? "text-accent" : "text-muted line-through"}
              title={item.site_alive ? "사이트 생존" : "사이트 미응답"}
            >
              ↗ {hostOf(href)}
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
                className="text-accent"
                title="사이트 내 문의폼"
              >
                📝 문의폼
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
export function QueueTable({ items, busyIds, onConfirm, onReject }: Props) {
  // 행별 선택(라디오) — 서버 selected 를 기본값으로, 사용자가 바꾸면 덮어쓴다.
  const [picked, setPicked] = useState<Record<string, string>>({});
  const onPick = useCallback((id: string, value: string) => {
    setPicked((p) => ({ ...p, [id]: value }));
  }, []);

  if (items.length === 0) {
    return <p className={EMPTY}>표시할 검증 항목이 없습니다.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse bg-panel border border-line rounded-lg overflow-hidden table-fixed">
        <thead>
          <tr>
            {HEADERS.map((h, i) => (
              <th key={h} className={`${TH} ${COL_W[i]}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((it) => (
            <QueueRow
              key={it.id}
              item={it}
              busy={busyIds.has(it.id)}
              choice={picked[it.id] ?? it.selected ?? it.candidates[0]?.value}
              onPick={onPick}
              onConfirm={onConfirm}
              onReject={onReject}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
