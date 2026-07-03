import { useEffect, useState } from "react";
import { fetchQueueFilters, fetchSendPreview, sendCampaign, withUnclassified } from "../../api";
import type { SendPreview, SendResult } from "../../types";
import { TriangleAlert } from "lucide-react";
import { MultiPicker, type PickerOption } from "../MultiPicker";
import { ErrorBox } from "../ErrorBox";
import { BTN, BTN_CONFIRM } from "../../ui";
import { SECTION_H2, FIELD, CRAWL_TARGET, INPUT } from "./shared";
import { ConfirmDialog } from "./ConfirmDialog";

// 확정큐 이메일 전체발송 — 제목·본문·발신표시명 직접 입력, 국가/업종 필터. 미리보기로
// 수신 N명 확인 후 발송. email_send_enabled(.env)가 꺼져 있으면 dry-run(실발송 안 함).
export function SendSection() {
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
  // 발송 확인 다이얼로그 — 앱에서 가장 위험한 액션이라 OS confirm 대신 수신 N·제목·
  // dry-run 여부를 보여주는 앱 스타일 다이얼로그를 거친다(SiteExplorer 확정 오버레이 톤).
  const [confirmOpen, setConfirmOpen] = useState(false);

  // Esc 리스너는 ConfirmDialog 내부로 이동(useEffect 제거).

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

  // 발송 클릭 → 항상 최신 미리보기를 받아 확인 다이얼로그를 연다. 기존 window.confirm 은
  // 미리보기 미실행 시 "0건에 발송할까요?"라고 뜨면서 실제론 전량 발송되는 문제가 있었다.
  const askSend = async () => {
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      setPreview(await fetchSendPreview(countries, industries));
      setConfirmOpen(true);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const doSend = async () => {
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
      setConfirmOpen(false);
    }
  };

  const canSend = subject.trim().length > 0 && bodyText.trim().length > 0;

  return (
    <section>
      <h2 className={SECTION_H2}>확정큐 이메일 발송</h2>
      {err && <ErrorBox>{err}</ErrorBox>}

      <div className="flex flex-col gap-5 max-w-[780px]">
        {/* 메시지 작성 그룹 */}
        <div className="flex flex-col gap-3">
          <span className="text-[11px] text-muted uppercase tracking-[0.06em]">메시지 작성</span>
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
        </div>

        {/* 발송 범위 그룹 */}
        <div className="flex flex-col gap-2">
          <span className="text-[11px] text-muted uppercase tracking-[0.06em]">발송 범위</span>
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
        </div>

        {/* 액션 버튼 */}
        <div className="flex gap-2">
          <button className={BTN} type="button" disabled={busy} onClick={() => void doPreview()}>
            미리보기(수신 N명)
          </button>
          <button
            className={BTN_CONFIRM}
            type="button"
            disabled={busy || !canSend}
            onClick={() => void askSend()}
          >
            발송
          </button>
        </div>
      </div>

      {/* 미리보기 결과 — 라벨-값 행으로 스캔 가능하게, dry-run 은 배너로 분리 */}
      {preview && (
        <div className="mt-4 max-w-[780px] flex flex-col gap-2">
          {!preview.enabled && (
            <div className="flex items-start gap-2 border-l-2 border-warn pl-3 py-1 text-[13px] text-warn">
              <TriangleAlert size={14} className="mt-0.5 shrink-0" aria-hidden />
              <span>
                발송 비활성(dry-run) — 실제로 보내지 않습니다.{" "}
                <span className="font-mono text-[12px]">LEADCRAWLER_EMAIL_SEND_ENABLED=true</span>{" "}
                필요
              </span>
            </div>
          )}
          <div className="bg-panel border border-line rounded-md px-3 py-2.5 text-[13px] flex flex-col gap-1.5">
            <div className="flex gap-3">
              <span className="text-muted w-20 shrink-0">수신</span>
              <span className="text-ink tabular-nums">{preview.recipients}명</span>
            </div>
            <div className="flex gap-3">
              <span className="text-muted w-20 shrink-0">발신</span>
              <span className="text-ink">
                {preview.sender || <span className="text-muted">.env 미설정</span>}
              </span>
            </div>
            {preview.enabled && (
              <div className="flex gap-3">
                <span className="text-muted w-20 shrink-0">오늘 잔여</span>
                <span className="text-ink tabular-nums">{preview.remaining_today}건</span>
              </div>
            )}
            {preview.sample.length > 0 && (
              <div className="flex gap-3">
                <span className="text-muted w-20 shrink-0">예시</span>
                <span className="text-muted [overflow-wrap:anywhere]">
                  {preview.sample.slice(0, 3).join(", ")}…
                </span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* 발송 결과 — 성공/실패/상한초과 카운트를 행으로 분리 */}
      {result && (
        <div className="mt-4 max-w-[780px]">
          {result.dry_run ? (
            <div className="bg-panel border border-line rounded-md px-3 py-2.5 text-[13px] text-muted">
              dry-run — 수신 {result.recipients}명 대상 시뮬레이션 (실발송 안 함)
            </div>
          ) : (
            <div className="bg-panel border border-line rounded-md px-3 py-2.5 text-[13px] flex flex-col gap-1.5">
              <div className="flex gap-3">
                <span className="text-muted w-20 shrink-0">성공</span>
                <span className="text-ok-fg tabular-nums">{result.sent}건</span>
              </div>
              {result.failed > 0 && (
                <div className="flex gap-3">
                  <span className="text-muted w-20 shrink-0">실패</span>
                  <span className="text-danger-fg tabular-nums">{result.failed}건</span>
                </div>
              )}
              {result.capped > 0 && (
                <div className="flex gap-3">
                  <span className="text-muted w-20 shrink-0">상한초과</span>
                  <span className="text-warn tabular-nums">{result.capped}건</span>
                </div>
              )}
              <div className="flex gap-3">
                <span className="text-muted w-20 shrink-0">수신</span>
                <span className="text-ink tabular-nums">{result.recipients}명</span>
              </div>
            </div>
          )}
        </div>
      )}

      {preview && (
        <ConfirmDialog
          open={confirmOpen}
          title={`확정큐 ${preview.recipients}건에 발송할까요?`}
          confirmLabel={preview.enabled ? "발송" : "dry-run 실행"}
          busy={busy}
          busyLabel="발송 중…"
          confirmDisabled={preview.recipients === 0}
          onConfirm={() => void doSend()}
          onCancel={() => setConfirmOpen(false)}
        >
          <div className="flex flex-col gap-1.5 text-[13px]">
            <div className="flex gap-2">
              <span className="text-muted w-20 shrink-0">수신</span>
              <span className="text-ink tabular-nums">
                {preview.recipients}명
                {preview.enabled && ` · 오늘 잔여 ${preview.remaining_today}건`}
              </span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted w-20 shrink-0">제목</span>
              <span className="text-ink [overflow-wrap:anywhere]">{subject}</span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted w-20 shrink-0">발신 표시명</span>
              <span className="text-ink">{fromName.trim() || "—"}</span>
            </div>
          </div>
          {!preview.enabled && (
            <div className="flex items-start gap-2 border-l-2 border-warn pl-3 py-1 text-[13px] text-warn">
              <TriangleAlert size={13} className="mt-0.5 shrink-0" aria-hidden />
              <span>발송 비활성(dry-run) — 실제로 보내지 않고 카운트만 반환합니다.</span>
            </div>
          )}
          {preview.recipients === 0 && (
            <p className="m-0 text-[13px] text-muted">
              수신 대상이 없습니다 — 필터를 확인하세요.
            </p>
          )}
        </ConfirmDialog>
      )}
    </section>
  );
}
