import type { ReactNode } from "react";
import { TriangleAlert } from "lucide-react";
import { ERROR_BOX } from "../ui";

// 오류 안내 박스 — 경고 아이콘 + 메시지. 흩어져 있던 `⚠ {msg}` 패턴을 한 곳으로 모아
// 이모지를 Lucide SVG 로 통일한다(플랫폼별 ⚠ 렌더 차이 제거). aria-hidden — 텍스트가 의미 전달.
// role=alert — 오류가 뜨면 스크린리더가 즉시 낭독(매초 바뀌는 값은 여기 넣지 말 것: 반복 낭독).
export function ErrorBox({ children }: { children: ReactNode }) {
  return (
    <div role="alert" className={`${ERROR_BOX} flex items-start gap-2`}>
      <TriangleAlert size={16} className="flex-none mt-0.5" aria-hidden />
      <span className="min-w-0">{children}</span>
    </div>
  );
}
