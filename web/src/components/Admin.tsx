import { useCallback, useEffect, useState } from "react";
import {
  changeUserRole,
  createUser,
  exportConfirmed,
  fetchAudit,
  fetchCountries,
  fetchCrawlTarget,
  fetchIndustries,
  fetchQueueFilters,
  fetchSendPreview,
  fetchUsers,
  reclaimUser,
  saveCrawlTarget,
  sendCampaign,
  setUserActive,
  withUnclassified,
} from "../api";
import type {
  AuditEntry,
  CrawlTarget,
  Listed,
  Role,
  SendPreview,
  SendResult,
  UserStats,
} from "../types";
import { Download, TriangleAlert } from "lucide-react";
import { MultiPicker, type PickerOption } from "./MultiPicker";
import { ErrorBox } from "./ErrorBox";
import { TableSkeleton } from "./TableSkeleton";
import { BTN, BTN_CONFIRM, BTN_EXPORT, BTN_REJECT, EMPTY, TD, TH } from "../ui";

// 폼 요소 공용 클래스 — 섹션 컨테이너·필드 라벨·입력·셀.
const SECTION_H2 = "text-base mt-0 mb-3";
const FIELD = "flex flex-col gap-1 text-muted text-[13px]";
const FIELD_INLINE = "flex flex-row items-center gap-1.5 text-muted text-[13px]";
const INPUT = "bg-canvas border border-line text-ink py-[7px] px-2.5 rounded-md";
const INPUT_WIDE = `${INPUT} min-w-[200px]`;
// items-start: 칩(선택 토큰) 증가로 피커가 자라도 위쪽 검색 input·라벨은 고정(아래로만 확장).
const CRAWL_TARGET = "flex flex-wrap items-start gap-3";

// 관리자 페이지 — 계정별 처리 통계·역할/활성 관리·계정 생성 + 최근 검증 감사 로그.
export function Admin() {
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
    <div className="flex flex-col gap-7">
      {error && <ErrorBox>{error}</ErrorBox>}

      <CrawlTargetSection />

      <ExportSection />

      <SendSection />

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
            {users.map((u) => (
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
                      <button className={BTN} onClick={() => void act(() => changeUserRole(u.id, "worker"))}>
                        직원으로
                      </button>
                    ) : (
                      <button className={BTN} onClick={() => void act(() => changeUserRole(u.id, "admin"))}>
                        관리자로
                      </button>
                    )}
                    {u.is_active ? (
                      <button className={BTN_REJECT} onClick={() => void act(() => setUserActive(u.id, false))}>
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
            ))}
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
    </div>
  );
}

// 감사 로그 액션 한글 라벨 — reclaim 은 관리자 점유 회수(PRD-queue-claim-permanent §4.6).
const ACTION_LABEL: Record<string, string> = {
  confirmed: "확정",
  rejected: "거부",
  reclaim: "회수",
};

const LISTED_OPTIONS: { value: Listed; label: string }[] = [
  { value: "unknown", label: "전체" },
  { value: "listed", label: "상장" },
  { value: "unlisted", label: "비상장" },
];

// '지금 크롤 실행' UI 는 제거됨(2026-06-30). 백엔드 기능(startCrawl/cancelCrawl/
// fetchCrawlStatus API·엔드포인트)은 그대로 유지 — 재노출하려면 git 이력에서 복원.

// 내일(다음) 크롤 타깃 설정 — 국가·업종·상장여부·DB적재. 스케줄러가 매일 이 값을 읽는다.
function CrawlTargetSection() {
  const [countryOpts, setCountryOpts] = useState<PickerOption[]>([]);
  const [industryOpts, setIndustryOpts] = useState<PickerOption[]>([]);
  const [countries, setCountries] = useState("");
  const [industries, setIndustries] = useState("");
  const [listed, setListed] = useState<Listed>("unknown");
  const [persist, setPersist] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const apply = (t: CrawlTarget) => {
    setCountries(t.countries);
    setIndustries(t.industries);
    setListed(t.listed);
    setPersist(t.persist);
  };

  useEffect(() => {
    let alive = true;
    Promise.all([fetchCrawlTarget(), fetchCountries(), fetchIndustries()])
      .then(([t, countryList, industryList]) => {
        if (!alive) return;
        apply(t);
        setCountryOpts(
          countryList.map((c) => ({
            value: c.iso2,
            label: c.label,
            code: c.iso2,
            aliases: c.aliases,
          })),
        );
        setIndustryOpts(
          industryList.map((i) => ({ value: i.value, label: i.label, aliases: i.aliases })),
        );
      })
      .catch((e) => alive && setErr(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, []);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setErr(null);
    try {
      const saved = await saveCrawlTarget({
        countries: countries.trim(),
        industries: industries.trim(),
        listed,
        persist,
      });
      apply(saved);
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : String(e2));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section>
      <h2 className={SECTION_H2}>내일 크롤 타깃</h2>
      {err && <ErrorBox>{err}</ErrorBox>}
      <form className={CRAWL_TARGET} onSubmit={(e) => void save(e)}>
        <div className={FIELD}>
          <span>
            국가 <span className="text-muted">(선택 안 함 = 전체)</span>
          </span>
          <MultiPicker
            options={countryOpts}
            value={countries}
            onChange={setCountries}
            placeholder="국가 검색 (예: 미국, US, 일본)"
            emptyHint="전체 국가"
          />
        </div>
        <div className={FIELD}>
          <span>
            업종 <span className="text-muted">(1개 이상 필수)</span>
          </span>
          <MultiPicker
            options={industryOpts}
            value={industries}
            onChange={setIndustries}
            placeholder="업종 검색 (예: 건설, construction)"
            emptyHint="업종을 1개 이상 선택하세요"
          />
        </div>
        <label className={FIELD}>
          상장여부
          <select
            className={INPUT_WIDE}
            value={listed}
            onChange={(e) => setListed(e.target.value as Listed)}
          >
            {LISTED_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        {/* DB적재 체크박스는 '저장 시 DB에 넣을지' — 저장 동작의 옵션이라 상장여부(필터)가
            아니라 크롤 실행 버튼과 한 그룹으로 묶는다. */}
        <div className="flex flex-col gap-2">
          <label className={FIELD_INLINE}>
            <input
              type="checkbox"
              checked={persist}
              onChange={(e) => setPersist(e.target.checked)}
            />
            DB 적재(검증 큐로)
          </label>
          <button className={BTN_CONFIRM} type="submit" disabled={saving || !industries.trim()}>
            {saving ? "저장 중…" : "크롤 실행"}
          </button>
        </div>
      </form>
    </section>
  );
}

// 확정큐 이메일 전체발송 — 제목·본문·발신표시명 직접 입력, 국가/업종 필터. 미리보기로
// 수신 N명 확인 후 발송. email_send_enabled(.env)가 꺼져 있으면 dry-run(실발송 안 함).
function SendSection() {
  const [countryOpts, setCountryOpts] = useState<PickerOption[]>([]);
  const [industryOpts, setIndustryOpts] = useState<PickerOption[]>([]);
  const [countries, setCountries] = useState("");
  const [industries, setIndustries] = useState("");
  const [subject, setSubject] = useState("");
  const [bodyText, setBodyText] = useState("");
  const [fromName, setFromName] = useState("");
  const [preview, setPreview] = useState<SendPreview | null>(null);
  const [result, setResult] = useState<SendResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchQueueFilters()
      .then((f) => {
        if (!alive) return;
        setCountryOpts(
          f.countries.map((c) => ({ value: c.iso2, label: c.label, code: c.iso2, aliases: c.aliases })),
        );
        // 발송 범위 업종은 큐 행 저장 어휘(구분 택소노미+미분류)와 일치해야 매치된다(#115) —
        // 크롤 타깃용 /admin/industries(18키)가 아니라 /queue/filters 를 출처로 쓴다.
        setIndustryOpts(
          withUnclassified(f.industries).map((i) => ({ value: i.value, label: i.label, aliases: i.aliases })),
        );
      })
      .catch((e) => alive && setErr(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, []);

  const doPreview = async () => {
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      setPreview(await fetchSendPreview(countries, industries));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const doSend = async () => {
    const n = preview?.recipients ?? 0;
    const dryWarn =
      preview && !preview.enabled
        ? "\n\n※ 발송 비활성(dry-run): 실제로 보내지 않고 카운트만 반환합니다."
        : "";
    if (!window.confirm(`확정큐 ${n}건에 발송할까요?${dryWarn}`)) return;
    setBusy(true);
    setErr(null);
    try {
      setResult(
        await sendCampaign({
          subject,
          body: bodyText,
          from_display: fromName,
          country: countries,
          industry: industries,
        }),
      );
      setPreview(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const canSend = subject.trim().length > 0 && bodyText.trim().length > 0;
  return (
    <section>
      <h2 className={SECTION_H2}>확정큐 이메일 발송</h2>
      {err && <ErrorBox>{err}</ErrorBox>}
      <div className="flex flex-col gap-3 max-w-[780px]">
        <label className={FIELD}>
          제목
          <input className={INPUT} value={subject} onChange={(e) => setSubject(e.target.value)} />
        </label>
        <label className={FIELD}>
          발신 표시명 <span className="text-muted">(From 주소는 서버 발신계정 고정)</span>
          <input
            className={INPUT}
            value={fromName}
            onChange={(e) => setFromName(e.target.value)}
            placeholder="예: Zenith Asset IR"
          />
        </label>
        <label className={FIELD}>
          본문
          <textarea
            className={`${INPUT} text-[13px] resize-y font-sans`}
            value={bodyText}
            rows={6}
            onChange={(e) => setBodyText(e.target.value)}
          />
        </label>
        <div className={CRAWL_TARGET}>
          <div className={FIELD}>
            <span>
              국가 <span className="text-muted">(선택 안 함 = 전체)</span>
            </span>
            <MultiPicker
              options={countryOpts}
              value={countries}
              onChange={setCountries}
              placeholder="국가 검색 (예: 미국, US, 일본)"
              emptyHint="전체 국가"
            />
          </div>
          <div className={FIELD}>
            <span>
              업종 <span className="text-muted">(선택 안 함 = 전체)</span>
            </span>
            <MultiPicker
              options={industryOpts}
              value={industries}
              onChange={setIndustries}
              placeholder="업종 검색 (예: 반도체, 미분류)"
              emptyHint="전체 업종"
            />
          </div>
        </div>
        <div className="flex gap-2">
          <button className={BTN} type="button" disabled={busy} onClick={() => void doPreview()}>
            미리보기(수신 N명)
          </button>
          <button
            className={BTN_CONFIRM}
            type="button"
            disabled={busy || !canSend}
            onClick={() => void doSend()}
          >
            발송
          </button>
        </div>
      </div>
      {preview && (
        <p className="text-muted">
          수신 {preview.recipients}명 · 발신 {preview.sender || "(.env 미설정)"} ·{" "}
          {preview.enabled ? (
            `오늘 잔여 ${preview.remaining_today}건`
          ) : (
            <>
              <TriangleAlert size={13} className="inline align-text-bottom" aria-hidden /> 발송
              비활성(dry-run) — .env LEADCRAWLER_EMAIL_SEND_ENABLED=true 필요
            </>
          )}
          {preview.sample.length > 0 && ` · 예: ${preview.sample.slice(0, 3).join(", ")}…`}
        </p>
      )}
      {result && (
        <p className={result.dry_run ? "text-muted" : ""}>
          {result.dry_run
            ? `dry-run: 수신 ${result.recipients}명 (실발송 안 함 — email_send_enabled=true 필요)`
            : `발송 완료 — 성공 ${result.sent} · 실패 ${result.failed} · 상한초과 ${result.capped} (수신 ${result.recipients})`}
        </p>
      )}
    </section>
  );
}

// 확정분 엑셀 추출 — 국가/업종을 골라 선택 추출(빈 선택=전체). 전체 추출도 여기서
// (선택 없이 다운로드). 헤더의 '전체 확정분' 버튼은 중복이라 제거됨(2026-07-02).
function ExportSection() {
  const [countryOpts, setCountryOpts] = useState<PickerOption[]>([]);
  const [industryOpts, setIndustryOpts] = useState<PickerOption[]>([]);
  const [countries, setCountries] = useState("");
  const [industries, setIndustries] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchQueueFilters()
      .then((f) => {
        if (!alive) return;
        setCountryOpts(
          f.countries.map((c) => ({ value: c.iso2, label: c.label, code: c.iso2, aliases: c.aliases })),
        );
        // 추출 범위 업종도 큐 행 저장 어휘(구분 택소노미+미분류)와 일치해야 매치된다(#115) —
        // 크롤 타깃용 /admin/industries(18키)가 아니라 /queue/filters 를 출처로 쓴다.
        setIndustryOpts(
          withUnclassified(f.industries).map((i) => ({ value: i.value, label: i.label, aliases: i.aliases })),
        );
      })
      .catch((e) => alive && setErr(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, []);

  const download = async () => {
    setBusy(true);
    setErr(null);
    try {
      await exportConfirmed(countries, industries);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <h2 className={SECTION_H2}>확정분 엑셀 추출</h2>
      {err && <ErrorBox>{err}</ErrorBox>}
      <div className={CRAWL_TARGET}>
        <div className={FIELD}>
          <span>
            국가 <span className="text-muted">(선택 안 함 = 전체)</span>
          </span>
          <MultiPicker
            options={countryOpts}
            value={countries}
            onChange={setCountries}
            placeholder="국가 검색 (예: 미국, US, 일본)"
            emptyHint="전체 국가"
          />
        </div>
        <div className={FIELD}>
          <span>
            업종 <span className="text-muted">(선택 안 함 = 전체)</span>
          </span>
          <MultiPicker
            options={industryOpts}
            value={industries}
            onChange={setIndustries}
            placeholder="업종 검색 (예: 반도체, 미분류)"
            emptyHint="전체 업종"
          />
        </div>
        {/* 버튼을 검색 input 줄에 맞춤 — 피커 라벨과 같은 높이의 투명 스페이서로 라벨 줄을
            비워 버튼 top 이 input top 과 같아진다(items-start 라 라벨에 붙어 뜨던 것 보정). */}
        <div className="flex flex-col gap-1">
          <span className="text-[13px] invisible select-none" aria-hidden>
            맞춤
          </span>
          <button className={BTN_EXPORT} type="button" disabled={busy} onClick={() => void download()}>
            {busy ? (
              "추출 중…"
            ) : (
              <span className="inline-flex items-center gap-1">
                엑셀 다운로드 <Download size={14} aria-hidden />
              </span>
            )}
          </button>
        </div>
      </div>
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

// ISO8601 → 로컬 표시(분 단위). 없으면 대시.
function fmt(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}
