import { INPUT } from "../../ui";
import type { PickerOption } from "../MultiPicker";
import type { Listed } from "../../types";

// 폼 요소 공용 클래스 — 섹션 컨테이너·필드 라벨·입력·셀. (INPUT 은 앱 공용이라 ui.ts 로 이동.)
// h1(text-xl) 한 단계 아래 — 본문(13~14px)과 크기·굵기 양쪽으로 구분해 섹션 스캔성 확보.
export const SECTION_H2 = "text-lg font-semibold tracking-tight mt-0 mb-3";
export const FIELD = "flex flex-col gap-1 text-muted text-[13px]";
export const FIELD_INLINE = "flex flex-row items-center gap-1.5 text-muted text-[13px]";
export const INPUT_WIDE = `${INPUT} min-w-[200px]`;
// items-start: 칩(선택 토큰) 증가로 피커가 자라도 위쪽 검색 input·라벨은 고정(아래로만 확장).
export const CRAWL_TARGET = "flex flex-wrap items-start gap-3";

// 크롤 타깃 계열 폼의 상장여부 선택지 — 조회 필터(LISTED_FILTER_OPTIONS)와 달리 빈값이
// 없고 unknown 이 '무필터=전체' 자리를 맡는다(크롤 실행·세그먼트 작업 요청 공용).
export const LISTED_TARGET_OPTIONS: { value: Listed; label: string }[] = [
  { value: "unknown", label: "전체" },
  { value: "listed", label: "상장" },
  { value: "unlisted", label: "비상장" },
];

// KR 17개 시/도(표준 축약형) — BE region.KR_REGIONS 와 동일 목록·순서(#139).
// 지역 지정은 KR 세그먼트 전용이라 조회 API 없이 고정 목록으로 둔다(크롤 실행·세그먼트
// 작업 요청 공용). 조회 필터 쪽 지역 어휘는 실측 distinct 라 별도(/queue/filters regions).
export const KR_REGION_OPTS: PickerOption[] = [
  "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
  "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
].map((r) => ({ value: r, label: r }));

// 폼 내부 소형 모드 선택(세그먼트 버튼) — 같은 픽커·같은 어휘를 쓰는 필드에서 '의미 방향'을
// 고르게 한다(백필 업종 포함/제외, 세그먼트 지역 지정 방식). 활성만 accent 로 또렷하게.
const SEG_BASE =
  "py-0.5 px-2 rounded-md border text-xs cursor-pointer transition-colors " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent " +
  "focus-visible:ring-offset-2 focus-visible:ring-offset-canvas";
export const segCls = (active: boolean): string =>
  active
    ? `${SEG_BASE} border-accent text-ink bg-accent/15`
    : `${SEG_BASE} border-line text-muted bg-panel`;

// ISO8601 → 로컬 표시(분 단위). 없으면 대시.
export function fmt(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}
