import { useCallback, useEffect, useState } from "react";
import {
  cancelCrawl,
  changeUserRole,
  createUser,
  exportConfirmed,
  fetchAudit,
  fetchCountries,
  fetchCrawlStatus,
  fetchCrawlTarget,
  fetchIndustries,
  fetchSendPreview,
  fetchUsers,
  saveCrawlTarget,
  sendCampaign,
  setUserActive,
  startCrawl,
} from "../api";
import type {
  AuditEntry,
  CrawlJob,
  CrawlTarget,
  Listed,
  Role,
  SendPreview,
  SendResult,
  UserStats,
} from "../types";
import { MultiPicker, type PickerOption } from "./MultiPicker";

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

      <CrawlNowSection />

      <CrawlTargetSection />

      <ExportSection />

      <SendSection />

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

const LISTED_OPTIONS: { value: Listed; label: string }[] = [
  { value: "unknown", label: "전체" },
  { value: "listed", label: "상장" },
  { value: "unlisted", label: "비상장" },
];

// 크롤 작업 상태 → 한글 라벨.
const CRAWL_STATUS_LABEL: Record<CrawlJob["status"], string> = {
  idle: "대기",
  running: "진행 중",
  done: "완료",
  failed: "실패",
  cancelled: "취소됨",
};

// 지금 크롤 실행 — 폼 즉석 입력값으로 즉시 1회전 크롤(타깃 저장과 무관). 진행현황을 3초
// 폴링으로 실시간 표시하고, 진행 중에는 '중지'로 협조적 취소한다. 동시 1건(409).
function CrawlNowSection() {
  const [countryOpts, setCountryOpts] = useState<PickerOption[]>([]);
  const [industryOpts, setIndustryOpts] = useState<PickerOption[]>([]);
  const [countries, setCountries] = useState("");
  const [industries, setIndustries] = useState("");
  const [listed, setListed] = useState<Listed>("unknown");
  const [persist, setPersist] = useState(true);
  const [job, setJob] = useState<CrawlJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const running = job?.status === "running";

  // 초기 로드 — 선택지·타깃 폼 초기값(편의)·현재 진행 중 작업(있으면 즉시 표시).
  useEffect(() => {
    let alive = true;
    Promise.all([fetchCountries(), fetchIndustries(), fetchCrawlTarget(), fetchCrawlStatus()])
      .then(([cs, is, target, status]) => {
        if (!alive) return;
        setCountryOpts(
          cs.map((c) => ({ value: c.iso2, label: c.label, code: c.iso2, aliases: c.aliases })),
        );
        setIndustryOpts(is.map((i) => ({ value: i.value, label: i.label, aliases: i.aliases })));
        setCountries(target.countries);
        setIndustries(target.industries);
        setListed(target.listed);
        setPersist(target.persist);
        if (status.status !== "idle") setJob(status);
      })
      .catch((e) => alive && setErr(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, []);

  // 진행 중이면 3초마다 현황 폴링. 종료 상태가 되면 인터벌 정리.
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => {
      fetchCrawlStatus()
        .then((s) => setJob(s))
        .catch((e) => setErr(e instanceof Error ? e.message : String(e)));
    }, 3000);
    return () => clearInterval(timer);
  }, [running]);

  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      setJob(
        await startCrawl({ countries: countries.trim(), industries: industries.trim(), listed, persist }),
      );
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    if (!window.confirm("진행 중인 크롤을 중지할까요? (처리된 분은 보존됩니다)")) return;
    setBusy(true);
    setErr(null);
    try {
      setJob(await cancelCrawl());
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <h2>지금 크롤 실행</h2>
      {err && <div className="error">⚠ {err}</div>}
      <div className="crawl-target">
        <div className="field">
          <span className="field-label">
            국가 <span className="muted">(선택 안 함=지원 전체국)</span>
          </span>
          <MultiPicker
            options={countryOpts}
            value={countries}
            onChange={setCountries}
            placeholder="국가 검색 (예: 미국, US, 일본)"
            emptyHint="지원 전체국 대상(국가를 선택하면 좁혀집니다)"
          />
        </div>
        <div className="field">
          <span className="field-label">
            업종 <span className="muted">(1개 이상 필수 — 표준 업종만 선택)</span>
          </span>
          <MultiPicker
            options={industryOpts}
            value={industries}
            onChange={setIndustries}
            placeholder="업종 검색 (예: 건설, construction)"
            emptyHint="업종을 1개 이상 선택하세요"
          />
        </div>
        <label>
          상장여부
          <select value={listed} onChange={(e) => setListed(e.target.value as Listed)}>
            {LISTED_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label className="inline">
          <input type="checkbox" checked={persist} onChange={(e) => setPersist(e.target.checked)} />
          DB 적재(검증 큐로)
        </label>
        <div className="send-actions">
          <button
            className="btn confirm"
            type="button"
            disabled={busy || running || !industries.trim()}
            onClick={() => void run()}
          >
            {running ? "실행 중…" : "지금 크롤 실행 ▶"}
          </button>
          {running && (
            <button className="btn reject" type="button" disabled={busy} onClick={() => void stop()}>
              중지 ■
            </button>
          )}
        </div>
      </div>
      {job && job.status !== "idle" && <CrawlProgress job={job} />}
    </section>
  );
}

// 크롤 진행현황 패널 — 상태·세그먼트 진행바·발견/처리/저장 카운터.
function CrawlProgress({ job }: { job: CrawlJob }) {
  const total = job.segments_total || 0;
  const done = job.segments_done || 0;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const stale = job.status === "running" && job.cancel_requested;
  return (
    <div className="crawl-progress">
      <p>
        <strong>상태: {CRAWL_STATUS_LABEL[job.status]}</strong>
        {stale && <span className="muted"> · 중지 요청됨…</span>}
        {job.triggered_by && <span className="muted"> · {job.triggered_by}</span>}
      </p>
      <progress value={done} max={total || 1} style={{ width: "100%" }} />
      <p className="muted">
        세그먼트 {done}/{total} ({pct}%) · 발견 {job.discovered} · 처리 {job.enriched} · 저장(실존){" "}
        {job.saved}
      </p>
      {job.error && <div className="error">⚠ {job.error}</div>}
      {job.finished_at && <p className="muted">종료: {fmt(job.finished_at)}</p>}
    </div>
  );
}

// 내일(다음) 크롤 타깃 설정 — 국가·업종·상장여부·DB적재. 스케줄러가 매일 이 값을 읽는다.
function CrawlTargetSection() {
  const [target, setTarget] = useState<CrawlTarget | null>(null);
  const [countryOpts, setCountryOpts] = useState<PickerOption[]>([]);
  const [industryOpts, setIndustryOpts] = useState<PickerOption[]>([]);
  const [countries, setCountries] = useState("");
  const [industries, setIndustries] = useState("");
  const [listed, setListed] = useState<Listed>("unknown");
  const [persist, setPersist] = useState(true);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const apply = (t: CrawlTarget) => {
    setTarget(t);
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
    setMsg(null);
    try {
      const saved = await saveCrawlTarget({
        countries: countries.trim(),
        industries: industries.trim(),
        listed,
        persist,
      });
      apply(saved);
      setMsg("저장됨 — 다음 크롤부터 반영됩니다.");
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : String(e2));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section>
      <h2>내일 크롤 타깃</h2>
      {err && <div className="error">⚠ {err}</div>}
      <form className="crawl-target" onSubmit={(e) => void save(e)}>
        <div className="field">
          <span className="field-label">
            국가 <span className="muted">(선택 안 함=지원 전체국)</span>
          </span>
          <MultiPicker
            options={countryOpts}
            value={countries}
            onChange={setCountries}
            placeholder="국가 검색 (예: 미국, US, 일본)"
            emptyHint="지원 전체국 대상(국가를 선택하면 좁혀집니다)"
          />
        </div>
        <div className="field">
          <span className="field-label">
            업종 <span className="muted">(1개 이상 필수 — 표준 업종만 선택)</span>
          </span>
          <MultiPicker
            options={industryOpts}
            value={industries}
            onChange={setIndustries}
            placeholder="업종 검색 (예: 건설, construction)"
            emptyHint="업종을 1개 이상 선택하세요(정확한 업종 필터를 위해 표준 목록에서만 선택)"
          />
        </div>
        <label>
          상장여부
          <select value={listed} onChange={(e) => setListed(e.target.value as Listed)}>
            {LISTED_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label className="inline">
          <input
            type="checkbox"
            checked={persist}
            onChange={(e) => setPersist(e.target.checked)}
          />
          DB 적재(검증 큐로)
        </label>
        <button className="btn confirm" type="submit" disabled={saving || !industries.trim()}>
          {saving ? "저장 중…" : "타깃 저장"}
        </button>
      </form>
      {msg && <p className="muted">{msg}</p>}
      {target?.updated_by && (
        <p className="muted">
          최근 설정: {target.updated_by} · {fmt(target.updated_at)}
        </p>
      )}
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
    Promise.all([fetchCountries(), fetchIndustries()])
      .then(([cs, is]) => {
        if (!alive) return;
        setCountryOpts(
          cs.map((c) => ({ value: c.iso2, label: c.label, code: c.iso2, aliases: c.aliases })),
        );
        setIndustryOpts(is.map((i) => ({ value: i.value, label: i.label, aliases: i.aliases })));
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
      <h2>확정큐 이메일 발송</h2>
      {err && <div className="error">⚠ {err}</div>}
      <div className="send-form">
        <label>
          제목
          <input value={subject} onChange={(e) => setSubject(e.target.value)} />
        </label>
        <label>
          발신 표시명 <span className="muted">(From 주소는 서버 발신계정 고정)</span>
          <input
            value={fromName}
            onChange={(e) => setFromName(e.target.value)}
            placeholder="예: Zenith Asset IR"
          />
        </label>
        <label className="full">
          본문
          <textarea value={bodyText} rows={6} onChange={(e) => setBodyText(e.target.value)} />
        </label>
        <div className="crawl-target">
          <div className="field">
            <span className="field-label">
              국가 <span className="muted">(선택 안 함=전체)</span>
            </span>
            <MultiPicker
              options={countryOpts}
              value={countries}
              onChange={setCountries}
              placeholder="국가 검색"
              emptyHint="전체 국가"
            />
          </div>
          <div className="field">
            <span className="field-label">
              업종 <span className="muted">(선택 안 함=전체)</span>
            </span>
            <MultiPicker
              options={industryOpts}
              value={industries}
              onChange={setIndustries}
              placeholder="업종 검색"
              emptyHint="전체 업종"
            />
          </div>
        </div>
        <div className="send-actions">
          <button className="btn" type="button" disabled={busy} onClick={() => void doPreview()}>
            미리보기(수신 N명)
          </button>
          <button
            className="btn confirm"
            type="button"
            disabled={busy || !canSend}
            onClick={() => void doSend()}
          >
            발송
          </button>
        </div>
      </div>
      {preview && (
        <p className="muted">
          수신 {preview.recipients}명 · 발신 {preview.sender || "(.env 미설정)"} ·{" "}
          {preview.enabled
            ? `오늘 잔여 ${preview.remaining_today}건`
            : "⚠ 발송 비활성(dry-run) — .env LEADCRAWLER_EMAIL_SEND_ENABLED=true 필요"}
          {preview.sample.length > 0 && ` · 예: ${preview.sample.slice(0, 3).join(", ")}…`}
        </p>
      )}
      {result && (
        <p className={result.dry_run ? "muted" : ""}>
          {result.dry_run
            ? `dry-run: 수신 ${result.recipients}명 (실발송 안 함 — email_send_enabled=true 필요)`
            : `발송 완료 — 성공 ${result.sent} · 실패 ${result.failed} · 상한초과 ${result.capped} (수신 ${result.recipients})`}
        </p>
      )}
    </section>
  );
}

// 확정분 엑셀 추출 — 국가/업종을 골라 선택 추출(빈 선택=전체). 헤더의 '전체 확정분'
// 버튼과 별개로, 국가·업종별로 좁혀 받는다.
function ExportSection() {
  const [countryOpts, setCountryOpts] = useState<PickerOption[]>([]);
  const [industryOpts, setIndustryOpts] = useState<PickerOption[]>([]);
  const [countries, setCountries] = useState("");
  const [industries, setIndustries] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([fetchCountries(), fetchIndustries()])
      .then(([cs, is]) => {
        if (!alive) return;
        setCountryOpts(
          cs.map((c) => ({ value: c.iso2, label: c.label, code: c.iso2, aliases: c.aliases })),
        );
        setIndustryOpts(is.map((i) => ({ value: i.value, label: i.label, aliases: i.aliases })));
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
      <h2>확정분 엑셀 추출</h2>
      {err && <div className="error">⚠ {err}</div>}
      <div className="crawl-target">
        <div className="field">
          <span className="field-label">
            국가 <span className="muted">(선택 안 함=전체)</span>
          </span>
          <MultiPicker
            options={countryOpts}
            value={countries}
            onChange={setCountries}
            placeholder="국가 검색 (예: 미국, US)"
            emptyHint="전체 국가 대상(선택하면 좁혀집니다)"
          />
        </div>
        <div className="field">
          <span className="field-label">
            업종 <span className="muted">(선택 안 함=전체)</span>
          </span>
          <MultiPicker
            options={industryOpts}
            value={industries}
            onChange={setIndustries}
            placeholder="업종 검색 (예: 건설, construction)"
            emptyHint="전체 업종 대상(선택하면 좁혀집니다)"
          />
        </div>
        <button
          className="btn export"
          type="button"
          disabled={busy}
          onClick={() => void download()}
        >
          {busy ? "추출 중…" : "엑셀 다운로드 ↓"}
        </button>
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
