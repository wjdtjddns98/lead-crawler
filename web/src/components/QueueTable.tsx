import { useState } from "react";
import type { ReviewItem } from "../types";
import { EmailBadge, StatusBadge } from "./StatusBadge";

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

// 검증 큐 표 — 회사/이메일 후보(다중 선택)/검증 신호/상태/액션.
export function QueueTable({ items, busyIds, onConfirm, onReject }: Props) {
  // 행별 선택(라디오) — 서버 selected 를 기본값으로, 사용자가 바꾸면 덮어쓴다.
  const [picked, setPicked] = useState<Record<string, string>>({});
  const choiceOf = (it: ReviewItem) =>
    picked[it.id] ?? it.selected ?? it.candidates[0]?.value;

  if (items.length === 0) {
    return <p className="empty">표시할 검증 항목이 없습니다.</p>;
  }
  return (
    <table className="queue">
      <thead>
        <tr>
          <th>업체명</th>
          <th>국가</th>
          <th>구분</th>
          <th>이메일 후보</th>
          <th>검증</th>
          <th>MX</th>
          <th>SMTP</th>
          <th>사이트</th>
          <th>상태</th>
          <th>액션</th>
        </tr>
      </thead>
      <tbody>
        {items.map((it) => {
          const busy = busyIds.has(it.id);
          const done = it.status !== "pending";
          const href = safeHref(it.homepage);
          return (
            <tr key={it.id} className={done ? "done" : ""}>
              <td className="name">{it.name}</td>
              <td>{it.country}</td>
              <td>{it.industry}</td>
              <td className="emails">
                {it.candidates.length === 0 ? (
                  <span className="muted">—</span>
                ) : it.candidates.length === 1 ? (
                  <span className="cand-single">
                    {it.candidates[0].value} <EmailBadge status={it.candidates[0].email_status} />
                  </span>
                ) : (
                  <div className="cands">
                    {it.candidates.map((c) => (
                      <label key={c.value} className="cand">
                        <input
                          type="radio"
                          name={`sel-${it.id}`}
                          checked={choiceOf(it) === c.value}
                          disabled={done}
                          onChange={() => setPicked((p) => ({ ...p, [it.id]: c.value }))}
                        />
                        <span className="cand-val">{c.value}</span>
                        <EmailBadge status={c.email_status} />
                      </label>
                    ))}
                  </div>
                )}
              </td>
              <td>
                <EmailBadge status={it.email_status} />
              </td>
              <td>{tri(it.email_mx)}</td>
              <td>{tri(it.email_smtp)}</td>
              <td>
                {href ? (
                  <a href={href} target="_blank" rel="noreferrer">
                    {it.site_alive ? "↗ 생존" : "↗"}
                  </a>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
              <td>
                <StatusBadge status={it.status} />
                {it.assignee && (
                  <span className="assignee" title={it.reviewed_at ?? undefined}>
                    {" · "}
                    {it.assignee}
                    {it.reviewed_at && <small className="muted"> {fmtTime(it.reviewed_at)}</small>}
                  </span>
                )}
              </td>
              <td className="actions">
                <button
                  className="btn confirm"
                  disabled={busy || it.status === "confirmed"}
                  onClick={() => onConfirm(it.id, choiceOf(it))}
                >
                  확정
                </button>
                <button
                  className="btn reject"
                  disabled={busy || it.status === "rejected"}
                  onClick={() => onReject(it.id)}
                >
                  거부
                </button>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
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
