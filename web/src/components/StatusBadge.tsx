import type { ReviewStatus } from "../types";

const LABEL: Record<ReviewStatus, string> = {
  pending: "대기",
  confirmed: "확정",
  rejected: "거부",
};

// 뱃지 공통 — 테두리 색은 각 상태가 지정(base 에는 폭만 둬 색 충돌 방지).
const BADGE = "inline-block px-2 py-0.5 rounded-[10px] text-xs border whitespace-nowrap";

const STATUS_CLS: Record<ReviewStatus, string> = {
  pending: "text-warn border-warn",
  confirmed: "text-ok-fg border-ok",
  rejected: "text-danger-fg border-danger",
};

// 큐 상태를 색상 뱃지로 표시.
export function StatusBadge({ status }: { status: ReviewStatus }) {
  return <span className={`${BADGE} ${STATUS_CLS[status]}`}>{LABEL[status]}</span>;
}

// 이메일 검증 상태(valid/risky/invalid/unknown/null)를 뱃지로 표시.
const EMAIL_CLS: Record<string, string> = {
  valid: "text-ok-fg border-ok",
  risky: "text-warn border-warn",
  invalid: "text-danger-fg border-danger",
  unknown: "text-muted border-line",
};

export function EmailBadge({ status }: { status: string | null }) {
  if (!status || !status.trim()) return <span className="text-muted">—</span>;
  // 알려진 어휘만 색상 클래스 적용, 그 외는 중립 처리(예상 밖 값 방어).
  const cls = EMAIL_CLS[status] ?? "text-muted border-line";
  return <span className={`${BADGE} ${cls}`}>{status}</span>;
}
