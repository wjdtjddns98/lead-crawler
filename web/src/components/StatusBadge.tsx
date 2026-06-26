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

// 영문 상태값을 한눈에 인지되는 한글 라벨로 — 알 수 없는 값은 원문 그대로 노출(방어).
const EMAIL_LABEL: Record<string, string> = {
  valid: "유효",
  risky: "주의",
  invalid: "무효",
  unknown: "미확인",
};

export function EmailBadge({ status }: { status: string | null }) {
  if (!status || !status.trim()) return <span className="text-muted">—</span>;
  // 알려진 어휘만 색상 클래스 적용, 그 외는 중립 처리(예상 밖 값 방어).
  const cls = EMAIL_CLS[status] ?? "text-muted border-line";
  const label = EMAIL_LABEL[status] ?? status;
  return <span className={`${BADGE} ${cls}`} title={status}>{label}</span>;
}
