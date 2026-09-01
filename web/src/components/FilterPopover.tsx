import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { BTN, BTN_FILTER_ACTIVE } from "../ui";
import type { PickerOption } from "./MultiPicker";

// CSV 선택값 → 트리거 버튼 요약("전체" / "미국" / "미국 외 2").
// 옵션이 아직 안 왔으면(로드 실패 포함) 저장 토큰 원문으로 폴백해 요약이 비지 않게 한다.
export function pickSummary(value: string, options: PickerOption[]): string {
  const tokens = value
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  if (tokens.length === 0) return "전체";
  const lower = new Set(tokens.map((t) => t.toLowerCase()));
  const selected = options.filter((o) => lower.has(o.value.toLowerCase()));
  const first = selected[0]?.label ?? tokens[0];
  // 옵션 어휘와 어긋난 저장 토큰이 섞여도 개수는 토큰 기준으로 정확히.
  const n = Math.max(selected.length, tokens.length);
  return n === 1 ? first : `${first} 외 ${n - 1}`;
}

// 필터 트리거 + 팝오버 — 접혀 있어도 트리거에 '라벨 + 선택 요약'이 보여 활성 조건을 잃지
// 않는다(요약 없이 접으면 "왜 3건뿐이지?" 사고). 멀티 선택 중엔 열어둔 채 연속 토글 가능,
// 바깥 클릭/Esc 로 닫힘(Esc 는 트리거로 포커스 복귀). z-30 — 모달(z-50) 아래.
export function FilterPopover({
  label,
  summary,
  active,
  children,
}: {
  label: string;
  summary: string;
  active: boolean; // 필터 적용 중 — accent 테두리 + 요약 강조(색+텍스트 이중 표시)
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        btnRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="relative" ref={rootRef}>
      <button
        type="button"
        ref={btnRef}
        className={active ? BTN_FILTER_ACTIVE : BTN}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="inline-flex items-center gap-1.5">
          <span className="text-muted">{label}</span>
          <span className={active ? "text-accent-fg font-medium" : ""}>{summary}</span>
          <ChevronDown
            size={13}
            className={`flex-none transition-transform ${open ? "rotate-180" : ""}`}
            aria-hidden
          />
        </span>
      </button>
      {open && (
        <div
          role="dialog"
          aria-label={`${label} 필터`}
          className="absolute left-0 top-full mt-1.5 z-30 bg-panel border border-line rounded-md p-3 shadow-lg shadow-black/40"
        >
          {children}
        </div>
      )}
    </div>
  );
}
