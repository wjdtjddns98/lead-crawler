import { useEffect, type ReactNode } from "react";
import { BTN, BTN_CONFIRM, BTN_REJECT } from "../../ui";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  danger?: boolean;
  confirmLabel: string;
  busy?: boolean;
  /** busy 중 확인 버튼 문구(예: "발송 중…"). 없으면 confirmLabel 유지. */
  busyLabel?: string;
  confirmDisabled?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  children?: ReactNode;
}

// 범용 확인 다이얼로그 — SendSection 발송 확인에서 추출.
// 보존 조건: (a) autoFocus=취소, (b) Esc=닫기(busy 차단), (c) backdrop 클릭=닫기(busy 차단),
// (d) role="dialog"+aria-modal+aria-label, (e) confirmDisabled 로 확인 버튼 잠금.
export function ConfirmDialog({
  open,
  title,
  danger,
  confirmLabel,
  busy,
  busyLabel,
  confirmDisabled,
  onConfirm,
  onCancel,
  children,
}: ConfirmDialogProps) {
  // Esc 닫기 — busy 중 차단(SendSection 에서 이동).
  useEffect(() => {
    if (!open || busy) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onCancel]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4"
      onClick={() => !busy && onCancel()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="bg-panel border border-line rounded-lg p-5 max-w-md w-full flex flex-col gap-4"
        onClick={(e) => e.stopPropagation()}
      >
        <p className="m-0 text-ink text-sm font-semibold">{title}</p>
        {children}
        <div className="flex gap-2">
          <button
            className={`${danger ? BTN_REJECT : BTN_CONFIRM} flex-1`}
            disabled={busy || confirmDisabled}
            onClick={onConfirm}
          >
            {busy ? (busyLabel ?? confirmLabel) : confirmLabel}
          </button>
          {/* 위험 액션이라 기본 포커스는 취소에 — Enter 오발송 방지 */}
          <button
            className={`${BTN} flex-1`}
            autoFocus
            disabled={busy}
            onClick={onCancel}
          >
            취소 (Esc)
          </button>
        </div>
      </div>
    </div>
  );
}
