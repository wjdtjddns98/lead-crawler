// 공용 Tailwind 클래스 묶음 — 반복되는 버튼·표 셀·오류 박스 스타일을 한 곳에 모은다.
// 인라인 유틸리티 방식이되 중복을 줄이려 문자열 상수로 공유한다(별도 CSS 클래스 없음).

// 버튼 — 색 변형(확정/거부/추출)은 테두리·글자색만 base 에 덧댄다. 테두리 색 충돌을
// 막으려 base 에는 border 폭만 두고 색은 각 변형이 지정한다. hover 는 모든 버튼 공통(accent).
const BTN_BASE =
  "inline-block bg-panel text-ink border py-1.5 px-3 rounded-md cursor-pointer no-underline " +
  "hover:enabled:border-accent disabled:opacity-45 disabled:cursor-not-allowed";
export const BTN = `${BTN_BASE} border-line`;
export const BTN_CONFIRM = `${BTN_BASE} border-ok text-ok-fg`;
export const BTN_REJECT = `${BTN_BASE} border-danger text-danger-fg`;
export const BTN_EXPORT = `${BTN_BASE} border-accent text-accent-fg`;

// 탭 — 활성/비활성에서 글자·테두리·배경이 모두 바뀌므로 상태별로 통째 구성(유틸 충돌 방지).
const TAB_BASE = "border py-1.5 px-3.5 rounded-md cursor-pointer";
export function tabCls(active: boolean): string {
  return active
    ? `${TAB_BASE} text-white border-accent bg-tab-active`
    : `${TAB_BASE} text-muted border-line bg-panel`;
}

// 오류 박스 + 빈 상태 안내 + 표 헤더/셀.
export const ERROR_BOX = "bg-err-bg border border-danger text-err-fg py-2.5 px-3.5 rounded-md mb-3";
export const EMPTY =
  "text-muted text-center p-10 bg-panel border border-dashed border-line rounded-lg";
export const TH =
  "px-2.5 py-2 border-b border-line align-top text-left text-muted font-semibold text-xs " +
  "uppercase tracking-[0.04em] whitespace-nowrap";
export const TD = "px-2.5 py-2 border-b border-line align-top text-left [overflow-wrap:anywhere]";
