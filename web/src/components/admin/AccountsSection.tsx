import { useCallback, useEffect, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import {
  changeUserRole,
  createUser,
  fetchAudit,
  fetchReviewDaily,
  fetchUsers,
  getUser,
  reclaimUser,
  setUserActive,
} from "../../api";
import type { AuditEntry, ReviewDailyStats, Role, UserStats } from "../../types";
import { errMsg } from "../../format";
import { ErrorBox } from "../ErrorBox";
import { TableSkeleton } from "../TableSkeleton";
import { BTN, BTN_CONFIRM, BTN_FILTER_ACTIVE, BTN_REJECT, EMPTY, INPUT, TD, TH } from "../../ui";
import { SECTION_H2, fmt } from "./shared";
import { ConfirmDialog } from "./ConfirmDialog";

// 감사 로그 액션 한글 라벨 — reclaim 은 관리자 점유 회수(PRD-queue-claim-permanent §4.6).
const ACTION_LABEL: Record<string, string> = {
  confirmed: "확정",
  rejected: "거부",
  reclaim: "회수",
};

// 확인 다이얼로그 대기 중인 액션 타입
type PendingAction =
  | { type: "demote"; user: UserStats }
  | { type: "deactivate"; user: UserStats }
  | { type: "reclaim"; user: UserStats }
  | null;

// 관리자 페이지 — 계정별 처리 통계·역할/활성 관리·계정 생성 + 최근 검증 감사 로그.
export function AccountsSection() {
  const [users, setUsers] = useState<UserStats[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null); // 회수 등 액션 성공 피드백
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState<PendingAction>(null); // 확인 다이얼로그 대기 상태
  const [busy, setBusy] = useState(false); // 다이얼로그 액션 진행 중

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [u, a] = await Promise.all([fetchUsers(), fetchAudit()]);
      setUsers(u);
      setAudit(a);
    } catch (e) {
      setError(errMsg(e));
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
      setError(errMsg(e));
    }
  };

  // 잠금성 액션(강등·비활성)은 회수와 같은 confirm 패턴 — 대상 계정이 즉시 접근을 잃는다.
  // 본인 계정은 버튼 자체를 disable(관리자가 스스로를 잠그는 사고 방지). 백엔드 가드는 별도.
  const me = getUser();

  // 다이얼로그 확정 — pending 타입에 따라 분기, busy 중 재진입 차단은 버튼 disabled 로 커버.
  const handleConfirm = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      if (pending.type === "demote") {
        await act(() => changeUserRole(pending.user.id, "worker"));
      } else if (pending.type === "deactivate") {
        await act(() => setUserActive(pending.user.id, false));
      } else {
        await act(async () => {
          const r = await reclaimUser(pending.user.id);
          setMsg(`${pending.user.username} 계정의 점유 ${r.reclaimed}건을 회수했습니다.`);
        });
      }
    } finally {
      setBusy(false);
      setPending(null);
    }
  };

  const handleCancel = () => {
    if (!busy) setPending(null);
  };

  return (
    <>
      {error && <ErrorBox>{error}</ErrorBox>}

      {/* 확인 다이얼로그 — 강등·비활성·회수 3종 공용. pending.user 스냅샷 기준으로 문구 결정. */}
      <ConfirmDialog
        open={!!pending}
        title={
          pending?.type === "demote"
            ? `${pending.user.username} 계정을 직원으로 변경할까요?`
            : pending?.type === "deactivate"
            ? `${pending.user.username} 계정을 비활성화할까요?`
            : `${pending?.user.username} 계정이 점유 중인 작업 ${pending?.user.claimed}건을 전부 회수할까요?`
        }
        danger={pending?.type !== "demote"}
        confirmLabel={
          pending?.type === "demote"
            ? "직원으로 변경"
            : pending?.type === "deactivate"
            ? "비활성화"
            : "회수"
        }
        busyLabel="처리 중…"
        busy={busy}
        onConfirm={() => void handleConfirm()}
        onCancel={handleCancel}
      >
        <p className="text-sm text-muted m-0">
          {pending?.type === "demote"
            ? "관리자 콘솔 접근이 즉시 차단됩니다."
            : pending?.type === "deactivate"
            ? "해당 계정은 즉시 로그인이 차단됩니다."
            : "회수된 작업은 즉시 다른 직원이 받아갈 수 있습니다."}
        </p>
      </ConfirmDialog>

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
                const inactive = !u.is_active;
                return (
                  <tr key={u.id} className={inactive ? "text-muted" : ""}>
                    <td className={`${TD} font-semibold`}>{u.username}</td>
                    <td className={TD}>
                      {/* 역할 배지 — 관리자만 굵게, 직원은 muted(비활성 행과 겹쳐도 구분됨) */}
                      <span className={u.role === "admin" ? "text-xs font-semibold" : "text-xs text-muted"}>
                        {u.role === "admin" ? "관리자" : "직원"}
                      </span>
                    </td>
                    <td className={TD}>
                      {/* 상태 배지 — 비활성 행에서도 danger 색으로 명시적으로 구분 */}
                      <span
                        className={
                          u.is_active
                            ? "text-xs font-semibold text-ok-fg"
                            : "text-xs font-semibold text-danger-fg"
                        }
                      >
                        {u.is_active ? "활성" : "비활성"}
                      </span>
                    </td>
                    <td className={`${TD} tabular-nums`}>{u.confirmed}</td>
                    <td className={`${TD} tabular-nums`}>{u.rejected}</td>
                    <td className={`${TD} tabular-nums`}>{u.claimed}</td>
                    <td className={`${TD} text-muted whitespace-nowrap`}>{fmt(u.last_action_at)}</td>
                    <td className={TD}>
                      <div className="flex gap-1.5 flex-wrap">
                        {u.role === "admin" ? (
                          <button
                            className={BTN}
                            disabled={self}
                            title={self ? "본인 계정은 강등할 수 없습니다" : undefined}
                            onClick={() => setPending({ type: "demote", user: u })}
                          >
                            직원으로
                          </button>
                        ) : (
                          <button
                            className={BTN}
                            onClick={() => void act(() => changeUserRole(u.id, "admin"))}
                          >
                            관리자로
                          </button>
                        )}
                        {u.is_active ? (
                          <button
                            className={BTN_REJECT}
                            disabled={self}
                            title={self ? "본인 계정은 비활성화할 수 없습니다" : undefined}
                            onClick={() => setPending({ type: "deactivate", user: u })}
                          >
                            비활성
                          </button>
                        ) : (
                          <button
                            className={BTN_CONFIRM}
                            onClick={() => void act(() => setUserActive(u.id, true))}
                          >
                            활성
                          </button>
                        )}
                        <button
                          className={BTN}
                          disabled={u.claimed === 0}
                          onClick={() => setPending({ type: "reclaim", user: u })}
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

      <ReviewDailySection />

      <AuditSection audit={audit} loading={loading} />
    </>
  );
}

// 한 페이지에 보여줄 이력 건수
const AUDIT_PAGE = 20;

// 연도 필드에 4자리 상한을 걸어야 브라우저가 5번째 숫자부터 다음 칸(월)으로 넘긴다.
// (max 없이 비워두면 연도 세그먼트가 6자리까지 계속 먹는다 — 네이티브 date input 스펙 동작.)
const DATE_MAX = "9999-12-31";

// 날짜 필터 프리셋 — [라벨, 오늘로부터 며칠 전]. null=전체(필터 해제).
const DATE_PRESETS: [string, number | null][] = [
  ["오늘", 0],
  ["최근 7일", 6],
  ["최근 30일", 29],
  ["전체", null],
];

// 직원별 일일 처리량(확정/거부) — GET /admin/stats/review-daily(#279). date=""면 BE 기본(오늘 KST).
function ReviewDailySection() {
  const [date, setDate] = useState("");
  const [stats, setStats] = useState<ReviewDailyStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchReviewDaily(date || undefined)
      .then((s) => {
        if (!cancelled) setStats(s);
      })
      .catch((e) => {
        if (!cancelled) setError(errMsg(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [date]);

  return (
    <section>
      <h2 className={SECTION_H2}>직원별 일일 처리량</h2>
      <div className="flex gap-2 mb-3.5 items-center">
        <input
          type="date"
          className={INPUT}
          value={date}
          max={DATE_MAX}
          onChange={(e) => setDate(e.target.value)}
          aria-label="집계 일자"
        />
        <button type="button" className={date === "" ? BTN_FILTER_ACTIVE : BTN} onClick={() => setDate("")}>
          오늘
        </button>
        {stats && <span className="text-muted text-[13px]">{stats.date} 기준</span>}
      </div>
      {error && <ErrorBox>{error}</ErrorBox>}
      {loading && !stats ? (
        <TableSkeleton rows={3} />
      ) : !stats || stats.items.length === 0 ? (
        <p className={EMPTY}>집계할 처리 이력이 없습니다.</p>
      ) : (
        <table className="w-full border-collapse bg-panel border border-line rounded-lg overflow-hidden">
          <thead>
            <tr>
              <th className={TH}>담당자</th>
              <th className={TH}>확정</th>
              <th className={TH}>거부</th>
            </tr>
          </thead>
          <tbody>
            {stats.items.map((it) => (
              <tr key={it.username}>
                <td className={`${TD} font-semibold`}>{it.username}</td>
                <td className={`${TD} tabular-nums`}>{it.confirmed}</td>
                <td className={`${TD} tabular-nums`}>{it.rejected}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

// 최근 검증 이력 — 담당자·액션·업체명 필터 + 페이지네이션.
// fetchAudit 이 BE 상한(500건)까지 받아오므로 필터/페이지는 전부 클라이언트에서 계산한다.
function AuditSection({ audit, loading }: { audit: AuditEntry[]; loading: boolean }) {
  const [actor, setActor] = useState(""); // ""=전체
  const [action, setAction] = useState(""); // ""=전체
  const [q, setQ] = useState(""); // 업체명 부분일치 검색
  const [dateFrom, setDateFrom] = useState(""); // ""=제한 없음, YYYY-MM-DD
  const [dateTo, setDateTo] = useState("");
  const [page, setPage] = useState(0);

  const actors = [...new Set(audit.map((a) => a.actor_username).filter(Boolean))];
  const needle = q.trim().toLowerCase();
  // fmt()와 동일하게 로컬 타임존 날짜로 비교 — ISO(UTC) 슬라이스는 자정 근처에서 표시일과 하루 어긋난다.
  const localDate = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString("sv-SE") : "");
  const daysAgoLocal = (n: number) => {
    const d = new Date();
    d.setDate(d.getDate() - n);
    return d.toLocaleDateString("sv-SE");
  };
  const setPreset = (days: number | null) =>
    applyFilter(() => {
      setDateFrom(days === null ? "" : daysAgoLocal(days));
      setDateTo(days === null ? "" : daysAgoLocal(0));
    });
  const isPreset = (days: number | null) =>
    days === null ? !dateFrom && !dateTo : dateFrom === daysAgoLocal(days) && dateTo === daysAgoLocal(0);
  const filtered = audit.filter((a) => {
    const d = localDate(a.at);
    return (
      (!actor || a.actor_username === actor) &&
      (!action || a.action === action) &&
      (!needle || (a.company_name || "").toLowerCase().includes(needle)) &&
      (!dateFrom || (d && d >= dateFrom)) &&
      (!dateTo || (d && d <= dateTo))
    );
  });
  const pages = Math.max(1, Math.ceil(filtered.length / AUDIT_PAGE));
  const cur = Math.min(page, pages - 1); // 필터로 결과가 줄어 페이지가 범위를 벗어나면 마지막 페이지로 보정
  const rows = filtered.slice(cur * AUDIT_PAGE, (cur + 1) * AUDIT_PAGE);

  // 필터 변경 시 1페이지로 — 이전 페이지 위치는 다른 조건에선 의미가 없다.
  const applyFilter = (fn: () => void) => {
    fn();
    setPage(0);
  };

  return (
    <section>
      <h2 className={SECTION_H2}>최근 검증 이력</h2>
      {loading && audit.length === 0 ? (
        <TableSkeleton rows={5} />
      ) : audit.length === 0 ? (
        <p className={EMPTY}>기록된 처리 이력이 없습니다.</p>
      ) : (
        <>
          <div className="flex gap-2 mb-3.5 flex-wrap items-center">
            <select
              className={INPUT}
              value={actor}
              onChange={(e) => applyFilter(() => setActor(e.target.value))}
              aria-label="담당자 필터"
            >
              <option value="">담당자 전체</option>
              {actors.map((u) => (
                <option key={u} value={u}>
                  {u}
                </option>
              ))}
            </select>
            <select
              className={INPUT}
              value={action}
              onChange={(e) => applyFilter(() => setAction(e.target.value))}
              aria-label="액션 필터"
            >
              <option value="">액션 전체</option>
              {Object.entries(ACTION_LABEL).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
            <input
              className={INPUT}
              placeholder="업체명 검색"
              value={q}
              onChange={(e) => applyFilter(() => setQ(e.target.value))}
              aria-label="업체명 검색"
            />
            <input
              type="date"
              className={INPUT}
              value={dateFrom}
              max={dateTo || DATE_MAX}
              onChange={(e) => applyFilter(() => setDateFrom(e.target.value))}
              aria-label="시작일"
            />
            <span className="text-muted text-[13px]">~</span>
            <input
              type="date"
              className={INPUT}
              value={dateTo}
              min={dateFrom || undefined}
              max={DATE_MAX}
              onChange={(e) => applyFilter(() => setDateTo(e.target.value))}
              aria-label="종료일"
            />
            {DATE_PRESETS.map(([label, days]) => (
              <button
                key={label}
                type="button"
                className={isPreset(days) ? BTN_FILTER_ACTIVE : BTN}
                onClick={() => setPreset(days)}
              >
                {label}
              </button>
            ))}
            <span className="text-muted text-[13px] tabular-nums">총 {filtered.length}건</span>
          </div>
          {/* table-fixed: 컬럼 폭을 헤더에서 고정 — 필터로 행 내용이 바뀌어도 폭이 출렁이지 않는다.
              시각은 nowrap 이라 px 고정, 업체·이메일은 남는 폭을 나눠 갖고 넘치면 truncate. */}
          {filtered.length === 0 ? (
            <p className={EMPTY}>조건에 맞는 이력이 없습니다.</p>
          ) : (
            <table className="w-full table-fixed border-collapse bg-panel border border-line rounded-lg overflow-hidden">
              <thead>
                <tr>
                  <th className={`${TH} w-48`}>시각</th>
                  <th className={`${TH} w-[12%]`}>담당자</th>
                  <th className={`${TH} w-[9%]`}>액션</th>
                  <th className={TH}>업체</th>
                  <th className={TH}>선택 이메일</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((a) => (
                  <tr key={a.id}>
                    <td className={`${TD} text-muted whitespace-nowrap tabular-nums`}>{fmt(a.at)}</td>
                    <td className={TD}>{a.actor_username || "—"}</td>
                    <td className={TD}>{ACTION_LABEL[a.action] ?? a.action}</td>
                    <td className={`${TD} font-semibold truncate`} title={a.company_name || undefined}>
                      {a.company_name || "—"}
                    </td>
                    <td className={`${TD} truncate`} title={a.selected ?? undefined}>
                      {a.selected ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {pages > 1 && (
            <div className="flex items-center gap-4 justify-center mt-3 text-muted">
              <button className={BTN} disabled={cur === 0} onClick={() => setPage(cur - 1)}>
                <span className="inline-flex items-center gap-1">
                  <ChevronLeft size={14} aria-hidden /> 이전
                </span>
              </button>
              <span className="tabular-nums">
                {cur + 1} / {pages}
              </span>
              <button className={BTN} disabled={cur + 1 >= pages} onClick={() => setPage(cur + 1)}>
                <span className="inline-flex items-center gap-1">
                  다음 <ChevronRight size={14} aria-hidden />
                </span>
              </button>
            </div>
          )}
        </>
      )}
    </section>
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
