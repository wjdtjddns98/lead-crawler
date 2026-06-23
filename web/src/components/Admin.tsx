import { useCallback, useEffect, useState } from "react";
import {
  changeUserRole,
  createUser,
  fetchAudit,
  fetchUsers,
  setUserActive,
} from "../api";
import type { AuditEntry, Role, UserStats } from "../types";

// 관리자 페이지 — 계정별 처리 통계·역할/활성 관리·계정 생성 + 최근 검증 감사 로그.
export function Admin() {
  const [users, setUsers] = useState<UserStats[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [u, a] = await Promise.all([fetchUsers(), fetchAudit()]);
      setUsers(u);
      setAudit(a);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="admin">
      {error && <div className="error">⚠ {error}</div>}

      <section>
        <h2>계정 {loading && <span className="muted">· 불러오는 중…</span>}</h2>
        <CreateUserForm onCreate={(u, p, r) => act(() => createUser(u, p, r))} />
        <table className="queue">
          <thead>
            <tr>
              <th>아이디</th>
              <th>권한</th>
              <th>상태</th>
              <th>확정</th>
              <th>거부</th>
              <th>마지막 처리</th>
              <th>액션</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className={u.is_active ? "" : "done"}>
                <td className="name">{u.username}</td>
                <td>{u.role === "admin" ? "관리자" : "직원"}</td>
                <td>{u.is_active ? "활성" : "비활성"}</td>
                <td>{u.confirmed}</td>
                <td>{u.rejected}</td>
                <td className="muted">{fmt(u.last_action_at)}</td>
                <td className="actions">
                  {u.role === "admin" ? (
                    <button className="btn" onClick={() => void act(() => changeUserRole(u.id, "worker"))}>
                      직원으로
                    </button>
                  ) : (
                    <button className="btn" onClick={() => void act(() => changeUserRole(u.id, "admin"))}>
                      관리자로
                    </button>
                  )}
                  {u.is_active ? (
                    <button className="btn reject" onClick={() => void act(() => setUserActive(u.id, false))}>
                      비활성
                    </button>
                  ) : (
                    <button className="btn confirm" onClick={() => void act(() => setUserActive(u.id, true))}>
                      활성
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section>
        <h2>최근 검증 이력</h2>
        {audit.length === 0 ? (
          <p className="empty">기록된 처리 이력이 없습니다.</p>
        ) : (
          <table className="queue">
            <thead>
              <tr>
                <th>시각</th>
                <th>담당자</th>
                <th>액션</th>
                <th>업체</th>
                <th>선택 이메일</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((a) => (
                <tr key={a.id}>
                  <td className="muted">{fmt(a.at)}</td>
                  <td>{a.actor_username || "—"}</td>
                  <td>{a.action === "confirmed" ? "확정" : "거부"}</td>
                  <td className="name">{a.company_name || "—"}</td>
                  <td>{a.selected ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function CreateUserForm({
  onCreate,
}: {
  onCreate: (username: string, password: string, role: Role) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("worker");

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    onCreate(username.trim(), password, role);
    setUsername("");
    setPassword("");
    setRole("worker");
  };

  return (
    <form className="create-user" onSubmit={submit}>
      <input
        placeholder="아이디"
        value={username}
        onChange={(e) => setUsername(e.target.value)}
        autoComplete="off"
      />
      <input
        type="password"
        placeholder="비밀번호(8자 이상)"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        autoComplete="new-password"
      />
      <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
        <option value="worker">직원</option>
        <option value="admin">관리자</option>
      </select>
      <button className="btn confirm" type="submit" disabled={!username || password.length < 8}>
        계정 생성
      </button>
    </form>
  );
}

// ISO8601 → 로컬 표시(분 단위). 없으면 대시.
function fmt(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}
