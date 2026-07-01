import { useEffect, useState } from "react";
import { login } from "../api";
import type { Role } from "../types";
import { BTN } from "../ui";
import { ErrorBox } from "./ErrorBox";

const FIELD = "flex flex-col gap-1 text-muted text-[13px]";
const INPUT =
  "bg-canvas border border-line text-ink py-2 px-2.5 rounded-md " +
  // 포커스 표시를 버튼·탭·링크(ui.ts)와 통일 — 다크 배경에서 약한 기본 아웃라인 대신 accent 링.
  "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:border-accent";

// 로그인 화면 — 성공 시 토큰을 저장하고 상위에 사용자명·권한을 통지한다.
export function Login({ onLogin }: { onLogin: (username: string, role: Role) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [lockSeconds, setLockSeconds] = useState(0); // 429 잠금 잔여 초(>0 이면 버튼 잠금).

  // 잠금 중 매초 카운트다운 — 0 이 되면 다시 시도 가능.
  useEffect(() => {
    if (lockSeconds <= 0) return;
    const id = setInterval(() => setLockSeconds((s) => s - 1), 1000);
    return () => clearInterval(id);
  }, [lockSeconds]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { username: who, role } = await login(username.trim(), password);
      onLogin(who, role);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      const retry = (err as { retryAfter?: number }).retryAfter;
      if (retry && retry > 0) setLockSeconds(retry);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <form
        className="w-full max-w-[320px] bg-panel border border-line rounded-[10px] py-7 px-6 flex flex-col gap-3"
        onSubmit={(e) => void submit(e)}
      >
        {/* 제목은 카드 안 — 내부 툴은 자기완결적 카드 하나가 정답(뺄 브랜드 없음). 라벨과 좌측 축 정렬. */}
        <div className="mb-1">
          <h1 className="text-2xl font-semibold m-0">검증 워크벤치</h1>
          <p className="text-muted mt-0.5">직원 로그인</p>
        </div>
        {error && <ErrorBox>{error}</ErrorBox>}
        <label className={FIELD}>
          아이디
          <input
            className={INPUT}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            autoComplete="username"
          />
        </label>
        <label className={FIELD}>
          비밀번호
          <input
            className={INPUT}
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </label>
        <button
          className={`${BTN} mt-1.5 text-center`}
          type="submit"
          disabled={busy || !username || !password || lockSeconds > 0}
        >
          {busy ? "로그인 중…" : lockSeconds > 0 ? `${lockSeconds}초 후 재시도` : "로그인"}
        </button>
      </form>
    </div>
  );
}
