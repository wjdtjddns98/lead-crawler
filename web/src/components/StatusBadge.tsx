import type { ReviewStatus } from "../types";

const LABEL: Record<ReviewStatus, string> = {
  pending: "대기",
  confirmed: "확정",
  rejected: "거부",
};

// 큐 상태를 색상 뱃지로 표시.
export function StatusBadge({ status }: { status: ReviewStatus }) {
  return <span className={`badge badge-${status}`}>{LABEL[status]}</span>;
}

// 이메일 검증 상태(valid/risky/invalid/unknown/null)를 뱃지로 표시.
const EMAIL_KNOWN = new Set(["valid", "risky", "invalid", "unknown"]);

export function EmailBadge({ status }: { status: string | null }) {
  if (!status || !status.trim()) return <span className="muted">—</span>;
  // 알려진 어휘만 색상 클래스 적용, 그 외는 중립 처리(예상 밖 값 방어).
  const cls = EMAIL_KNOWN.has(status) ? `email-${status}` : "email-unknown";
  return <span className={`badge ${cls}`}>{status}</span>;
}
