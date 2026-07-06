// 폼 요소 공용 클래스 — 섹션 컨테이너·필드 라벨·입력·셀.
export const SECTION_H2 = "text-base mt-0 mb-3";
export const FIELD = "flex flex-col gap-1 text-muted text-[13px]";
export const FIELD_INLINE = "flex flex-row items-center gap-1.5 text-muted text-[13px]";
export const INPUT = "bg-canvas border border-line text-ink py-[7px] px-2.5 rounded-md";
export const INPUT_WIDE = `${INPUT} min-w-[200px]`;
// items-start: 칩(선택 토큰) 증가로 피커가 자라도 위쪽 검색 input·라벨은 고정(아래로만 확장).
export const CRAWL_TARGET = "flex flex-wrap items-start gap-3";

// ISO8601 → 로컬 표시(분 단위). 없으면 대시.
export function fmt(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}
