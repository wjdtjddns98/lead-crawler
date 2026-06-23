import { useState } from "react";
import { login } from "../api";
import type { Role } from "../types";

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
    <div className="login-wrap">
      <form className="login-card" onSubmit={(e) => void submit(e)}>
        <h1>검증 워크벤치</h1>
        <p className="muted">직원 로그인</p>
        {error && <div className="error">⚠ {error}</div>}
        <label>
          아이디
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            autoComplete="username"
          />
        </label>
        <label>
          비밀번호
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </label>
        <button className="btn" type="submit" disabled={busy || !username || !password}>
          {busy ? "로그인 중…" : "로그인"}
        </button>
      </form>
    </div>
  );
}
