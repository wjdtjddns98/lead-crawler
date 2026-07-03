import { useCallback, useEffect, useState } from "react";
import {
  changeUserRole,
  createUser,
  fetchAudit,
  fetchUsers,
  getUser,
  reclaimUser,
  setUserActive,
} from "../../api";
import type { AuditEntry, Role, UserStats } from "../../types";
import { ErrorBox } from "../ErrorBox";
import { TableSkeleton } from "../TableSkeleton";
import { BTN, BTN_CONFIRM, BTN_REJECT, EMPTY, TD, TH } from "../../ui";
import { SECTION_H2, INPUT, fmt } from "./shared";

// 감사 로그 액션 한글 라벨 — reclaim 은 관리자 점유 회수(PRD-queue-claim-permanent §4.6).
const ACTION_LABEL: Record<string, string> = {
  confirmed: "확정",
  rejected: "거부",
  reclaim: "회수",
};

// 관리자 페이지 — 계정별 처리 통계·역할/활성 관리·계정 생성 + 최근 검증 감사 로그.
export function AccountsSection() {
  const [users, setUsers] = useState<UserStats[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null); // 회수 등 액션 성공 피드백
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
    setMsg(null); // 이전 액션의 성공 메시지 잔존 방지 — fn 이 성공 시 새 메시지를 채운다.
    try {
      await fn();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  // 잠금성 액션(강등·비활성)은 회수와 같은 confirm 패턴 — 대상 계정이 즉시 접근을 잃는다.
  // 본인 계정은 버튼 자체를 disable(관리자가 스스로를 잠그는 사고 방지). 백엔드 가드는 별도.
  const me = getUser();

  const demote = async (u: UserStats) => {
    if (
      !window.confirm(
        `${u.username} 계정을 직원으로 변경할까요?\n관리자 콘솔 접근이 즉시 차단됩니다.`,
      )
    )
      return;
    await act(() => changeUserRole(u.id, "worker"));
  };

  const deactivate = async (u: UserStats) => {
    if (
      !window.confirm(`${u.username} 계정을 비활성화할까요?\n해당 계정은 즉시 로그인이 차단됩니다.`)
    )
      return;
    await act(() => setUserActive(u.id, false));
  };

  // 점유 회수 — 영구 배정이라 방치 점유(퇴사·장기부재)는 이 버튼이 유일한 해제 경로.
  // 되돌릴 수 없는 건 아니지만 다른 직원 작업분에 영향이 커 확인 다이얼로그를 거친다.
  const reclaim = async (u: UserStats) => {
    if (
      !window.confirm(
        `${u.username} 계정이 점유 중인 작업 ${u.claimed}건을 전부 회수할까요?\n회수된 작업은 즉시 다른 직원이 받아갈 수 있습니다.`,
      )
    )
      return;
    await act(async () => {
      const r = await reclaimUser(u.id);
      setMsg(`${u.username} 계정의 점유 ${r.reclaimed}건을 회수했습니다.`);
    });
  };

  return (
    <>
      {error && <ErrorBox>{error}</ErrorBox>}
      <section>
        <h2 className={SECTION_H2}>
          계정 {loading && <span className="text-muted">· 불러오는 중…</span>}
        </h2>
        <CreateUserForm onCreate={(u, p, r) => act(() => createUser(u, p, r))} />
        {msg && (
          <p className="text-ok-fg text-[13px] my-2" role="status">
            {msg}
          </p>
        )}
        {loading && users.length === 0 ? (
          <TableSkeleton rows={4} />
        ) : (
        <table className="w-full border-collapse bg-panel border border-line rounded-lg overflow-hidden">
          <thead>
            <tr>
              <th className={TH}>아이디</th>
              <th className={TH}>권한</th>
              <th className={TH}>상태</th>
              <th className={TH}>확정</th>
              <th className={TH}>거부</th>
              <th className={TH}>점유</th>
              <th className={TH}>마지막 처리</th>
              <th className={TH}>액션</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const self = u.username === me;
              return (
              <tr key={u.id} className={u.is_active ? "" : "opacity-60"}>
                <td className={`${TD} font-semibold`}>{u.username}</td>
                <td className={TD}>{u.role === "admin" ? "관리자" : "직원"}</td>
                <td className={TD}>{u.is_active ? "활성" : "비활성"}</td>
                <td className={TD}>{u.confirmed}</td>
                <td className={TD}>{u.rejected}</td>
                <td className={`${TD} tabular-nums`}>{u.claimed}</td>
                <td className={`${TD} text-muted`}>{fmt(u.last_action_at)}</td>
                <td className={TD}>
                  <div className="flex gap-1.5 flex-wrap">
                    {u.role === "admin" ? (
                      <button
                        className={BTN}
                        disabled={self}
                        title={self ? "본인 계정은 강등할 수 없습니다" : undefined}
                        onClick={() => void demote(u)}
                      >
                        직원으로
                      </button>
                    ) : (
                      <button className={BTN} onClick={() => void act(() => changeUserRole(u.id, "admin"))}>
                        관리자로
                      </button>
                    )}
                    {u.is_active ? (
                      <button
                        className={BTN_REJECT}
                        disabled={self}
                        title={self ? "본인 계정은 비활성화할 수 없습니다" : undefined}
                        onClick={() => void deactivate(u)}
                      >
                        비활성
                      </button>
                    ) : (
                      <button className={BTN_CONFIRM} onClick={() => void act(() => setUserActive(u.id, true))}>
                        활성
                      </button>
                    )}
                    <button
                      className={BTN}
                      disabled={u.claimed === 0}
                      onClick={() => void reclaim(u)}
                      title="이 계정이 점유 중인 미처리 작업을 전부 풀로 되돌립니다"
                    >
                      회수
                    </button>
                  </div>
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
        )}
      </section>

      <section>
        <h2 className={SECTION_H2}>최근 검증 이력</h2>
        {loading && audit.length === 0 ? (
          <TableSkeleton rows={5} />
        ) : audit.length === 0 ? (
          <p className={EMPTY}>기록된 처리 이력이 없습니다.</p>
        ) : (
          <table className="w-full border-collapse bg-panel border border-line rounded-lg overflow-hidden">
            <thead>
              <tr>
                <th className={TH}>시각</th>
                <th className={TH}>담당자</th>
                <th className={TH}>액션</th>
                <th className={TH}>업체</th>
                <th className={TH}>선택 이메일</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((a) => (
                <tr key={a.id}>
                  <td className={`${TD} text-muted`}>{fmt(a.at)}</td>
                  <td className={TD}>{a.actor_username || "—"}</td>
                  <td className={TD}>{ACTION_LABEL[a.action] ?? a.action}</td>
                  <td className={`${TD} font-semibold`}>{a.company_name || "—"}</td>
                  <td className={TD}>{a.selected ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
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
    <form className="flex gap-2 mb-3.5 flex-wrap" onSubmit={submit}>
      <input
        className={INPUT}
        placeholder="아이디"
        value={username}
        onChange={(e) => setUsername(e.target.value)}
        autoComplete="off"
      />
      <input
        className={INPUT}
        type="password"
        placeholder="비밀번호(8자 이상)"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        autoComplete="new-password"
      />
      <select className={INPUT} value={role} onChange={(e) => setRole(e.target.value as Role)}>
        <option value="worker">직원</option>
        <option value="admin">관리자</option>
      </select>
      <button className={BTN_CONFIRM} type="submit" disabled={!username || password.length < 8}>
        계정 생성
      </button>
    </form>
  );
}
