// 로딩 스켈레톤 — 데이터 도착 전 표 자리(높이·모양)를 미리 잡아 레이아웃 점프를 막는다.
// Tailwind 내장 animate-pulse 사용(의존성 0). 장식이므로 aria-hidden — 로딩 안내 텍스트가 별도로 있음.
export function TableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div
      className="bg-panel border border-line rounded-lg overflow-hidden animate-pulse"
      aria-hidden
    >
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="flex items-center gap-4 px-2.5 py-3 border-b border-line last:border-b-0"
        >
          <div className="h-3 rounded bg-line flex-1" />
          <div className="h-3 rounded bg-line w-16" />
          <div className="h-3 rounded bg-line w-28" />
          <div className="h-3 rounded bg-line w-20" />
        </div>
      ))}
    </div>
  );
}
