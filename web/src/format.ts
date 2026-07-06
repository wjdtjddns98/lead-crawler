// 여러 컴포넌트에서 반복되던 순수 표시/방어 헬퍼 모음.

// unknown 오류 → 사용자 표시 메시지(catch 블록 공용).
export function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// 크롤된 신뢰불가 URL 의 스킴을 검증한다 — http(s) 만 허용(javascript:/data: 등 XSS 차단).
export function safeHref(url: string | null): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

// 불리언/널 3-state 를 O/X/— 로 표시.
export function tri(v: boolean | null): string {
  if (v === null) return "—";
  return v ? "O" : "X";
}
