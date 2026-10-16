import { useRef, useState, type ClipboardEvent, type ReactNode } from "react";
import { fetchSendPreview, sendCampaign } from "../../api";
import type { SendPreview, SendResult } from "../../types";
import { TriangleAlert, X } from "lucide-react";
import { bodyToHtml, errMsg, fmtBytes, IMG_TOKEN } from "../../format";
import { useQueueFilterOpts } from "../../filterOptions";
import { MultiPicker } from "../MultiPicker";
import { ErrorBox } from "../ErrorBox";
import { BTN, BTN_CONFIRM, INPUT } from "../../ui";
import { SECTION_H2, FIELD, CRAWL_TARGET } from "./shared";
import { ConfirmDialog } from "./ConfirmDialog";

// 라벨-값 행(미리보기·결과·확인 다이얼로그 공용) — gap 만 호출부가 지정.
function Row({ label, gap = "gap-3", children }: { label: string; gap?: string; children: ReactNode }) {
  return (
    <div className={`flex ${gap}`}>
      <span className="text-muted w-20 shrink-0">{label}</span>
      {children}
    </div>
  );
}

// 첨부·본문 이미지 상한 — BE #535 SendRequest 와 동일(넘으면 요청 전체가 422).
const MAX_FILES = 10;
const MAX_IMAGES = 20;
const MAX_TOTAL = 10 * 1024 * 1024;
// 붙여넣기 허용 이미지 → 확장자(BE 허용 목록 png/jpeg/gif/webp — 확장자로 판정, webp 는 BE #537).
const IMG_EXT: Record<string, string> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/gif": "gif",
  "image/webp": "webp",
};

// 붙여넣은 이미지 — n 은 본문 자리표시 `[이미지 n]`, name 은 Content-ID 겸 파일명(ASCII 전용).
type PastedImage = { n: number; name: string; file: File; url: string };

// File → 순수 base64. readAsDataURL 의 `data:…;base64,` 접두는 BE(validate=True)가 422 로 거부.
function toBase64(f: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] ?? "");
    r.onerror = () => reject(r.error);
    r.readAsDataURL(f);
  });
}

const toPayload = async (filename: string, f: File) => ({
  filename,
  content_base64: await toBase64(f),
});

// 상한 초과 안내 — 첫 번째로 걸린 상한 하나만 보여준다(없으면 null = 발송 가능).
function attachError(fileCount: number, imageCount: number, totalBytes: number): string | null {
  if (fileCount > MAX_FILES) return `첨부파일은 ${MAX_FILES}개까지 보낼 수 있습니다.`;
  if (imageCount > MAX_IMAGES) return `본문 이미지는 ${MAX_IMAGES}개까지 보낼 수 있습니다.`;
  if (totalBytes > MAX_TOTAL) {
    return `첨부 합계 ${fmtBytes(totalBytes)} — 본문 이미지 포함 10MB 이하로 줄이세요.`;
  }
  return null;
}

// 확정큐 이메일 전체발송 — 제목·본문·발신표시명 직접 입력, 국가/업종 필터. 미리보기로
// 수신 N명 확인 후 발송. email_send_enabled(.env)가 꺼져 있으면 dry-run(실발송 안 함).
export function SendSection() {
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
  const [files, setFiles] = useState<File[]>([]);
  const [images, setImages] = useState<PastedImage[]>([]);
  const imgSeq = useRef(0); // 지운 번호를 재사용하지 않는다(같은 cid 두 개 → 그림 뒤바뀜).
  const fileInput = useRef<HTMLInputElement>(null);

  // 발송 범위 업종은 큐 행 저장 어휘(구분 택소노미+미분류)와 일치해야 매치된다(#115) —
  // 크롤 타깃용 /admin/industries(18키)가 아니라 /queue/filters 를 출처로 쓴다.
  const { countryOpts, industryOpts } = useQueueFilterOpts(setErr);

  const doPreview = async () => {
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      setPreview(await fetchSendPreview(countries, industries, subject));
    } catch (e) {
      setErr(errMsg(e));
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
      setPreview(await fetchSendPreview(countries, industries, subject));
      setConfirmOpen(true);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  // 본문에 자리표시가 남아 있는 이미지만 보낸다 — 자리표시를 지우면 발송·합계에서 빠진다.
  const used = new Set([...bodyText.matchAll(IMG_TOKEN)].map((m) => Number(m[1])));
  const usedImages = images.filter((im) => used.has(im.n));
  // BE 선검사(base64 길이 × 3/4)와 같은 산식 — 파일당 최대 2바이트 크게 잡힌다.
  const totalBytes = [...files, ...usedImages.map((im) => im.file)].reduce(
    (s, f) => s + 3 * Math.ceil(f.size / 3),
    0,
  );
  const attachErr = attachError(files.length, usedImages.length, totalBytes);

  const pickFiles = (list: FileList | null) => {
    const picked = [...(list ?? [])];
    if (picked.some((f) => f.size === 0)) setErr("빈 파일은 첨부할 수 없습니다.");
    setFiles((prev) => [...prev, ...picked.filter((f) => f.size > 0)]);
  };

  // 이미지 붙여넣기 → 커서 자리에 `[이미지 n]`. 텍스트가 함께 오면(엑셀·워드 복사는 그림
  // 사본을 같이 싣는다) 평소 텍스트 붙여넣기로 둔다.
  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const pasted = [...e.clipboardData.files].filter((f) => f.type.startsWith("image/"));
    if (pasted.length === 0 || e.clipboardData.getData("text/plain").trim()) return;
    e.preventDefault();
    const bad = pasted.find((f) => !IMG_EXT[f.type]);
    if (bad) {
      setErr(`붙여넣을 수 없는 이미지 형식입니다(${bad.type}) — png·jpeg·gif·webp 만 됩니다.`);
      return;
    }
    const added = pasted.map((file) => {
      const n = ++imgSeq.current;
      return { n, name: `paste-${n}.${IMG_EXT[file.type]}`, file, url: URL.createObjectURL(file) };
    });
    setImages((prev) => [...prev, ...added]);
    const ta = e.currentTarget;
    const tokens = added.map((im) => `[이미지 ${im.n}]`).join("\n");
    const pos = ta.selectionStart + tokens.length;
    setBodyText(bodyText.slice(0, ta.selectionStart) + tokens + bodyText.slice(ta.selectionEnd));
    requestAnimationFrame(() => ta.setSelectionRange(pos, pos));
  };

  const removeImage = (im: PastedImage) => {
    URL.revokeObjectURL(im.url);
    setImages((prev) => prev.filter((x) => x !== im));
    setBodyText((t) => t.split(`[이미지 ${im.n}]`).join(""));
  };

  const doSend = async () => {
    setBusy(true);
    setErr(null);
    try {
      const [attachments, inline_images] = await Promise.all([
        Promise.all(files.map((f) => toPayload(f.name, f))),
        Promise.all(usedImages.map((im) => toPayload(im.name, im.file))),
      ]);
      setResult(
        await sendCampaign({
          subject,
          body: bodyText,
          from_display: fromName,
          country: countries,
          industry: industries,
          attachments,
          // 이미지가 없으면 지금처럼 텍스트 메일 — HTML 본문은 붙여넣은 이미지가 있을 때만.
          ...(inline_images.length > 0 && {
            body_html: bodyToHtml(bodyText, (n) => usedImages.find((im) => im.n === n)?.name),
            inline_images,
          }),
        }),
      );
      setPreview(null);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
      setConfirmOpen(false);
    }
  };

  const canSend = subject.trim().length > 0 && bodyText.trim().length > 0 && !attachErr;
  const attachSummary = [
    files.length > 0 && `파일 ${files.length}개`,
    usedImages.length > 0 && `본문 이미지 ${usedImages.length}개`,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <section>
      <h2 className={SECTION_H2}>확정큐 이메일 발송</h2>
      {err && <ErrorBox>{err}</ErrorBox>}

      <div className="flex flex-col gap-5">
        {/* 데스크탑(xl)은 작성 | 범위 2열 — 범위가 작성 내내 옆에 보인다. 작성란이 남는 폭을 채움. */}
        <div className="flex flex-col gap-5 xl:flex-row xl:items-start xl:gap-8">
          {/* 메시지 작성 그룹 */}
          <div className="flex flex-col gap-3 w-full max-w-[780px] xl:flex-1 xl:min-w-0">
            <span className="text-[13px] font-semibold text-ink">메시지 작성</span>
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
              <span>
                본문 <span className="text-muted">(이미지는 Ctrl+V 로 붙여넣기)</span>
              </span>
              <textarea
                className={`${INPUT} text-[13px] resize-y font-sans`}
                value={bodyText}
                rows={6}
                onChange={(e) => setBodyText(e.target.value)}
                onPaste={onPaste}
              />
            </label>
            {usedImages.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {usedImages.map((im) => (
                  <figure key={im.n} className="m-0 flex flex-col items-start gap-1 text-[12px] text-muted">
                    <img
                      src={im.url}
                      alt={`이미지 ${im.n}`}
                      className="h-16 max-w-[160px] object-contain rounded border border-line bg-canvas"
                    />
                    <figcaption className="flex items-center gap-1">
                      [이미지 {im.n}]
                      <button
                        type="button"
                        className="p-1 -m-1 text-muted hover:text-ink"
                        aria-label={`이미지 ${im.n} 제거`}
                        onClick={() => removeImage(im)}
                      >
                        <X size={12} aria-hidden />
                      </button>
                    </figcaption>
                  </figure>
                ))}
              </div>
            )}
            <div className={FIELD}>
              <span>
                첨부파일{" "}
                <span className="text-muted">(최대 {MAX_FILES}개 · 본문 이미지 포함 합계 10MB)</span>
              </span>
              <div className="flex flex-wrap items-center gap-2">
                <button className={BTN} type="button" onClick={() => fileInput.current?.click()}>
                  파일 선택
                </button>
                <input
                  ref={fileInput}
                  type="file"
                  multiple
                  hidden
                  onChange={(e) => {
                    pickFiles(e.target.files);
                    e.target.value = ""; // 같은 파일을 지웠다 다시 고를 수 있게
                  }}
                />
                {files.map((f, i) => (
                  <span
                    key={`${i}-${f.name}`}
                    className="inline-flex items-center gap-1 border border-line rounded-md px-2 py-0.5 text-[12px] text-ink"
                  >
                    <span className="[overflow-wrap:anywhere]">{f.name}</span>
                    <span className="text-muted tabular-nums">{fmtBytes(f.size)}</span>
                    <button
                      type="button"
                      className="p-1 -m-1 text-muted hover:text-ink"
                      aria-label={`${f.name} 첨부 제거`}
                      onClick={() => setFiles((prev) => prev.filter((_, j) => j !== i))}
                    >
                      <X size={12} aria-hidden />
                    </button>
                  </span>
                ))}
              </div>
              <span className="text-danger-fg" aria-live="polite">
                {attachErr}
              </span>
            </div>
          </div>

          {/* 발송 범위 그룹 — 피커가 체크 목록이라 세로로 쌓으면 길어진다: 2열에서도 국가·업종 나란히 */}
          <div className="flex flex-col gap-2 xl:shrink-0">
            <span className="text-[13px] font-semibold text-ink">발송 범위</span>
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
            <Row label="수신">
              <span className="text-ink tabular-nums">{preview.recipients}명</span>
            </Row>
            <Row label="발신">
              <span className="text-ink">
                {preview.sender || <span className="text-muted">.env 미설정</span>}
              </span>
            </Row>
            {preview.enabled && (
              <Row label="오늘 잔여">
                <span className="text-ink tabular-nums">{preview.remaining_today}건</span>
              </Row>
            )}
            {preview.sample.length > 0 && (
              <Row label="예시">
                <span className="text-muted [overflow-wrap:anywhere]">
                  {preview.sample.slice(0, 3).join(", ")}…
                </span>
              </Row>
            )}
          </div>
        </div>
      )}

      {/* 발송 결과 — 성공/실패/불명/스킵/상한초과 카운트를 행으로 분리 */}
      {result && (
        <div className="mt-4 max-w-[780px]">
          {result.dry_run ? (
            <div className="bg-panel border border-line rounded-md px-3 py-2.5 text-[13px] text-muted">
              dry-run — 수신 {result.recipients}명 대상 시뮬레이션 (실발송 안 함)
            </div>
          ) : (
            <div className="bg-panel border border-line rounded-md px-3 py-2.5 text-[13px] flex flex-col gap-1.5">
              <Row label="성공">
                <span className="text-ok-fg tabular-nums">{result.sent}건</span>
              </Row>
              {result.failed > 0 && (
                <Row label="실패">
                  <span className="text-danger-fg tabular-nums">{result.failed}건</span>
                </Row>
              )}
              {result.uncertain > 0 && (
                <Row label="결과 불명">
                  <span className="text-warn tabular-nums">{result.uncertain}건</span>
                </Row>
              )}
              {result.skipped > 0 && (
                <Row label="스킵">
                  <span className="text-muted tabular-nums">{result.skipped}건</span>
                </Row>
              )}
              {result.capped > 0 && (
                <Row label="상한초과">
                  <span className="text-warn tabular-nums">{result.capped}건</span>
                </Row>
              )}
              <Row label="수신">
                <span className="text-ink tabular-nums">{result.recipients}명</span>
              </Row>
              {/* 결과 불명은 유일하게 사람 손이 필요한 결과 — 자동 재발송에서 빠지므로
                  카운트만 두면 후속 조치가 누락된다(BE #422 계약). */}
              {result.uncertain > 0 && (
                <div className="mt-1 flex items-start gap-2 border-l-2 border-warn pl-3 py-1 text-[13px] text-warn">
                  <TriangleAlert size={13} className="mt-0.5 shrink-0" aria-hidden />
                  <span>
                    결과 불명 {result.uncertain}건 — 메일 서버가 응답을 끊어 전달 여부를 알 수
                    없습니다. 이미 전달됐을 수 있어{" "}
                    <strong className="font-semibold">같은 제목 재발송에서 제외</strong>되니, 발신함을
                    직접 확인해 필요하면 수동 재발송하세요.
                  </span>
                </div>
              )}
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
            <Row label="수신" gap="gap-2">
              <span className="text-ink tabular-nums">
                {preview.recipients}명
                {preview.enabled && ` · 오늘 잔여 ${preview.remaining_today}건`}
              </span>
            </Row>
            <Row label="제목" gap="gap-2">
              <span className="text-ink [overflow-wrap:anywhere]">{subject}</span>
            </Row>
            <Row label="발신 표시명" gap="gap-2">
              <span className="text-ink">{fromName.trim() || "—"}</span>
            </Row>
            {attachSummary && (
              <Row label="첨부" gap="gap-2">
                <span className="text-ink tabular-nums">
                  {attachSummary} · 합계 {fmtBytes(totalBytes)}
                </span>
              </Row>
            )}
          </div>
          {!preview.enabled && (
            <div className="flex items-start gap-2 border-l-2 border-warn pl-3 py-1 text-[13px] text-warn">
              <TriangleAlert size={13} className="mt-0.5 shrink-0" aria-hidden />
              <span>발송 비활성(dry-run) — 실제로 보내지 않고 카운트만 반환합니다.</span>
            </div>
          )}
          {preview.recipients === 0 && (
            <p className="m-0 text-[13px] text-muted">
              수신 대상이 없습니다 — 필터를 확인하거나, 이 제목으로 이미 모두 보냈는지
              확인하세요(다른 내용이면 제목을 바꿔 보내세요).
            </p>
          )}
        </ConfirmDialog>
      )}
    </section>
  );
}
