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

// 사이트 편집값 정규화 — 스킴 없는 입력(example.com)은 https:// 를 보충한다. 그래도
// 무효(http(s) 외 스킴·형식 오류)면 null — safeHref 와 동일하게 XSS 스킴을 차단.
export function normSiteUrl(s: string): string | null {
  return safeHref(s) ?? safeHref(`https://${s}`);
}

// 불리언/널 3-state 를 O/X/— 로 표시.
export function tri(v: boolean | null): string {
  if (v === null) return "—";
  return v ? "O" : "X";
}
