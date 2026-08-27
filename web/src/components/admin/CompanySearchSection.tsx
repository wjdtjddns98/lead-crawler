import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Search } from "lucide-react";
import { errStatus, searchCompanies } from "../../api";
import { errMsg, safeHref } from "../../format";
import type { CompanyEmailInfo, CompanySearchItem, Listed } from "../../types";
import { ErrorBox } from "../ErrorBox";
import { TableSkeleton } from "../TableSkeleton";
import { EmailBadge, StatusBadge } from "../StatusBadge";
import { BTN, EMPTY, INPUT, LINK_FOCUS, TD, TH } from "../../ui";
import { SECTION_H2 } from "./shared";

const PAGE = 50;

// 상장여부 표기 — 큐 테이블(QueueTable LISTED_LABEL)과 같은 어휘.
const LISTED_LABEL: Record<Listed, string> = { listed: "상장", unlisted: "비상장", unknown: "미상" };

// 이메일 role 표기 — BE EmailRole 어휘. hr·press·personal 은 발송 배제 대상(제약 §3)이라
// 검색 결과에는 보이되 muted 로 낮춰 "쓸 수 있는 주소"와 구분한다.
const ROLE_LABEL: Record<string, string> = {
  ir: "IR",
  general: "일반",
  hr: "인사",
  press: "언론",
  personal: "개인",
  unknown: "미상",
};
const USABLE_ROLES = new Set(["ir", "general"]);

// 한 행에 펼칠 이메일 상한 — 구식 데이터는 회사당 수십 개가 올 수 있어 표가 세로로 터진다.
// BE 정렬이 값(value) 오름차순이라 '좋은 주소 먼저'가 아니므로, 잘린 개수를 반드시 알린다.
const MAX_EMAILS_SHOWN = 3;

// 관리자 회사 DB 검색(BE PR#418) — 큐에 없는 회사까지 포함해 DB 전체를 텍스트로 찾는다.
// 용도는 중복 확인·수동 조회(이 회사 이미 있나 / 어디까지 왔나)라 결과는 읽기 전용이다.
export function CompanySearchSection() {
  const [q, setQ] = useState("");
  // 실행된 검색어 — 입력값과 분리한다. BE 가 선두 와일드카드 ILIKE(인덱스 미사용)라
  // 타이핑마다 조회하면 안 되고, 명시적 제출(Enter·버튼)에서만 돈다. tick 은 같은 검색어
  // 재제출(=새로고침)도 조회로 이어지게 하는 nonce.
  const [run, setRun] = useState<{ q: string; tick: number } | null>(null);
  const [offset, setOffset] = useState(0);
  const [items, setItems] = useState<CompanySearchItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 늦게 도착한 옛 응답이 현재 결과를 덮어쓰지 않게 한다(연타·페이지 이동 레이스).
  const reqRef = useRef(0);

  useEffect(() => {
    if (!run) return;
    const myReq = ++reqRef.current;
    setLoading(true);
    setError(null);
    searchCompanies(run.q, PAGE, offset)
      .then((res) => {
        if (myReq !== reqRef.current) return;
        setItems(res.items);
        setTotal(res.total);
      })
      .catch((e) => {
        if (myReq !== reqRef.current) return;
        setItems([]);
        setTotal(0);
        // 미배포 서버(BE #418 승격 전)에선 404 — 원인이 "요청 실패: 404"로만 보이면
        // 검색어 문제로 오해하므로 배포 대기임을 명시한다.
        setError(
          errStatus(e) === 404
            ? "이 서버에는 회사 검색 API 가 아직 배포되지 않았습니다(백엔드 배포 후 사용 가능)."
            : errMsg(e),
        );
      })
      .finally(() => {
        if (myReq === reqRef.current) setLoading(false);
      });
  }, [run, offset]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const needle = q.trim();
    if (!needle) return;
    setOffset(0); // 새 검색은 항상 1페이지부터(이전 검색의 페이지 위치는 의미가 없다).
    setRun({ q: needle, tick: Date.now() });
  };

  const page = Math.floor(offset / PAGE) + 1;
  const pages = Math.max(1, Math.ceil(total / PAGE));

  return (
    <section>
      <h2 className={SECTION_H2}>
        회사 검색 {loading && <span className="text-muted">· 불러오는 중…</span>}
      </h2>
      <p className="text-muted text-[13px] mt-0 mb-3">
        검증 큐와 무관하게 회사 DB 전체를 찾습니다 — 회사명·홈페이지·이메일/문의폼 주소·해외
        원장 영문명 부분일치(대소문자 무시).
      </p>

      <form className="flex gap-2 mb-3.5 flex-wrap items-center" onSubmit={submit}>
        <input
          className={`${INPUT} min-w-[280px]`}
          placeholder="회사명·홈페이지·이메일 (예: 삼성, toyota, ir@)"
          value={q}
          maxLength={200}
          onChange={(e) => setQ(e.target.value)}
          aria-label="회사 검색어"
        />
        <button className={BTN} type="submit" disabled={!q.trim() || loading}>
          <span className="inline-flex items-center gap-1">
            <Search size={14} aria-hidden /> 검색
          </span>
        </button>
        {run && !loading && !error && (
          <span className="text-muted text-[13px] tabular-nums">총 {total}건</span>
        )}
      </form>

      {error && <ErrorBox>{error}</ErrorBox>}

      {loading && items.length === 0 ? (
        <TableSkeleton rows={5} />
      ) : !run ? (
        <p className={EMPTY}>검색어를 입력하면 회사 DB 전체에서 찾습니다.</p>
      ) : !error && items.length === 0 ? (
        <p className={EMPTY}>일치하는 회사가 없습니다 — “{run.q}”</p>
      ) : items.length > 0 ? (
        <>
          {/* table-fixed: 검색어마다 행 내용이 크게 달라져도 컬럼 폭이 출렁이지 않게 고정. */}
          <table className="w-full table-fixed border-collapse bg-panel border border-line rounded-lg overflow-hidden">
            <thead>
              <tr>
                <th className={`${TH} w-[22%]`}>회사</th>
                <th className={`${TH} w-16`}>국가</th>
                <th className={`${TH} w-[12%]`}>업종</th>
                {/* 상장+보드를 한 줄에 담는 폭(w-24 면 'KOSDAQ' 이 줄바꿈된다). */}
                <th className={`${TH} w-28`}>상장</th>
                <th className={TH}>이메일</th>
                <th className={`${TH} w-[15%]`}>사이트·문의폼</th>
                <th className={`${TH} w-[11%]`}>검증 큐</th>
              </tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <CompanyRow key={c.id} c={c} />
              ))}
            </tbody>
          </table>
          {pages > 1 && (
            <div className="flex items-center gap-4 justify-center mt-3 text-muted">
              <button
                className={BTN}
                disabled={offset === 0 || loading}
                onClick={() => setOffset(Math.max(0, offset - PAGE))}
              >
                <span className="inline-flex items-center gap-1">
                  <ChevronLeft size={14} aria-hidden /> 이전
                </span>
              </button>
              <span className="tabular-nums">
                {page} / {pages}
              </span>
              <button
                className={BTN}
                disabled={offset + PAGE >= total || loading}
                onClick={() => setOffset(offset + PAGE)}
              >
                <span className="inline-flex items-center gap-1">
                  다음 <ChevronRight size={14} aria-hidden />
                </span>
              </button>
            </div>
          )}
        </>
      ) : null}
    </section>
  );
}

function CompanyRow({ c }: { c: CompanySearchItem }) {
  const href = safeHref(c.homepage);
  const formHref = safeHref(c.form);
  const shown = c.emails.slice(0, MAX_EMAILS_SHOWN);
  const hidden = c.emails.length - shown.length;
  return (
    <tr className={c.is_active ? "" : "opacity-60"}>
      <td className={TD}>
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="font-semibold" title={c.name}>
            {c.name}
          </span>
          {/* 비활성(폐업·소멸 처리분) — 검색은 큐 상태와 무관하게 전부 보여주므로, 지금
              연락하면 안 되는 회사임을 행에서 바로 알려야 한다(제약 ②). */}
          {!c.is_active && (
            <span className="text-xs text-danger-fg border border-danger rounded-[10px] px-1.5 py-0.5">
              비활성
            </span>
          )}
        </div>
        {/* 중복 확인의 실제 근거는 이름이 아니라 canonical_key(재추출 금지 판정 키, 제약 ①). */}
        <div className="text-muted text-[11px] font-mono truncate" title={c.canonical_key}>
          {c.canonical_key}
        </div>
      </td>
      <td className={TD}>{c.country || "—"}</td>
      <td className={`${TD} truncate`} title={c.industry || undefined}>
        {c.industry || "—"}
      </td>
      <td className={`${TD} ${c.listed === "unknown" ? "text-muted" : ""}`}>
        {LISTED_LABEL[c.listed] ?? c.listed}
        {/* 시장 보드는 큐 테이블과 같이 상장여부에 병기(별도 컬럼 없이 정보 밀도 절제). */}
        {c.market && <span className="text-muted text-xs ml-1">{c.market}</span>}
      </td>
      <td className={TD}>
        {c.emails.length === 0 ? (
          <span className="text-muted">—</span>
        ) : (
          <div className="flex flex-col gap-0.5">
            {shown.map((e) => (
              <EmailLine key={`${e.value}-${e.role}`} email={e} />
            ))}
            {hidden > 0 && <span className="text-muted text-[11px]">외 {hidden}개</span>}
          </div>
        )}
      </td>
      <td className={`${TD} text-[13px]`}>
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noreferrer"
            className={`${LINK_FOCUS} block truncate ${
              c.site_alive ? "text-accent" : "text-muted line-through"
            }`}
            title={c.site_alive ? c.homepage ?? undefined : "사이트 미응답"}
          >
            {c.homepage}
          </a>
        ) : (
          <span className="text-muted">—</span>
        )}
        {formHref && (
          <div>
            <a
              href={formHref}
              target="_blank"
              rel="noreferrer"
              className={`${LINK_FOCUS} text-accent text-xs`}
              title={c.form ?? undefined}
            >
              문의폼
            </a>
          </div>
        )}
      </td>
      <td className={TD}>
        {c.review_status ? (
          <>
            <StatusBadge status={c.review_status} />
            {c.review_assignee && (
              <div className="text-muted text-[11px] truncate" title={c.review_assignee}>
                {c.review_assignee}
              </div>
            )}
          </>
        ) : (
          // 큐 미적재 = 아직 검증 대상으로 올라오지 않은 회사(이메일 없음·백필 대기 등).
          <span className="text-muted text-xs">미적재</span>
        )}
      </td>
    </tr>
  );
}

function EmailLine({ email }: { email: CompanyEmailInfo }) {
  const usable = USABLE_ROLES.has(email.role);
  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="font-mono text-[13px] [overflow-wrap:anywhere]">{email.value}</span>
      <span
        className={`text-[11px] ${usable ? "text-muted" : "text-muted/70"}`}
        title={usable ? undefined : "발송 대상에서 배제되는 역할(HR·언론·개인)"}
      >
        {ROLE_LABEL[email.role] ?? email.role}
      </span>
      <EmailBadge status={email.status} />
    </div>
  );
}
