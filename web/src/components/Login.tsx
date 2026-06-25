import { useState } from "react";
import { login } from "../api";
import type { Role } from "../types";
import { BTN, ERROR_BOX } from "../ui";

const FIELD = "flex flex-col gap-1 text-muted text-[13px]";
const INPUT = "bg-canvas border border-line text-ink py-2 px-2.5 rounded-md";

// 로그인 화면 — 성공 시 토큰을 저장하고 상위에 사용자명·권한을 통지한다.
export function Login({ onLogin }: { onLogin: (username: string, role: Role) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { username: who, role } = await login(username.trim(), password);
      onLogin(who, role);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <form
        className="w-[320px] bg-panel border border-line rounded-[10px] py-7 px-6 flex flex-col gap-3"
        onSubmit={(e) => void submit(e)}
      >
        <h1 className="text-xl m-0">검증 워크벤치</h1>
        <p className="text-muted">직원 로그인</p>
        {error && <div className={ERROR_BOX}>⚠ {error}</div>}
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
        <button className={`${BTN} mt-1.5 text-center`} type="submit" disabled={busy || !username || !password}>
          {busy ? "로그인 중…" : "로그인"}
        </button>
      </form>
    </div>
  );
}
