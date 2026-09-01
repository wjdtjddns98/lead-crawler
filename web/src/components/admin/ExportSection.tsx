import { useState } from "react";
import { exportLeads } from "../../api";
import { Download } from "lucide-react";
import { errMsg } from "../../format";
import { useQueueFilterOpts } from "../../filterOptions";
import { MultiPicker } from "../MultiPicker";
import { ErrorBox } from "../ErrorBox";
import { BTN_EXPORT, INPUT } from "../../ui";
import { SECTION_H2, FIELD, CRAWL_TARGET, segCls } from "./shared";
import type { ExportStatus } from "../../types";

// 연도 4자리 상한 — AccountsSection 의 DATE_MAX 와 동일 사유(네이티브 date input 세그먼트 넘김).
const DATE_MAX = "9999-12-31";

const STATUS_OPTS: { value: ExportStatus; label: string }[] = [
  { value: "confirmed", label: "확정분" },
  { value: "rejected", label: "거부분" },
];

// 확정분·거부분 엑셀 추출 — 국가/업종/처리일(KST)로 선택 추출(전부 빈 선택=전체). 전체 추출도
// 여기서(선택 없이 다운로드). 헤더의 '전체 확정분' 버튼은 중복이라 제거됨(2026-07-02).
// admin 콘솔 전용이 아니다 — MyWork(worker)도 재사용(#273, BE 가 role 로 본인 처리분만 내려줌).
// 날짜 필터는 review_queue.reviewed_at 기준(#308) — 도입 전 구데이터(NULL)는 지정 시 제외.
// mode 를 주면 그 상태로 고정하고 선택 UI 를 숨긴다 — MyWork 는 확정/거부 탭 자체가 모드라
// 같은 선택을 두 번 시키지 않는다. 미지정(admin)일 때만 확정/거부 토글을 노출(#462).
export function ExportSection({ title, mode }: { title?: string; mode?: ExportStatus }) {
  const [picked, setPicked] = useState<ExportStatus>("confirmed");
  const [countries, setCountries] = useState("");
  const [industries, setIndustries] = useState("");
  const [dateFrom, setDateFrom] = useState(""); // ""=제한 없음, YYYY-MM-DD(KST, 포함)
  const [dateTo, setDateTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // 추출 범위 업종도 큐 행 저장 어휘(구분 택소노미+미분류)와 일치해야 매치된다(#115) —
  // 크롤 타깃용 /admin/industries(18키)가 아니라 /queue/filters 를 출처로 쓴다.
  const { countryOpts, industryOpts } = useQueueFilterOpts(setErr);

  const status = mode ?? picked;
  const statusLabel = status === "confirmed" ? "확정" : "거부";

  const download = async () => {
    setBusy(true);
    setErr(null);
    try {
      await exportLeads(status, countries, industries, dateFrom, dateTo);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <h2 className={SECTION_H2}>{title ?? "엑셀 추출"}</h2>
      {err && <ErrorBox>{err}</ErrorBox>}
      <div className={CRAWL_TARGET}>
        {mode === undefined && (
          <div className={FIELD}>
            <span>추출 대상</span>
            {/* 필터가 아니라 '무엇을 뽑는가' 라서 맨 앞 — 국가·업종·기간은 그 아래 좁히는 조건.
                세그먼트 버튼 묶음 마크업은 백필·세그먼트 섹션의 모드 선택과 동일 형태. */}
            <span className="inline-flex gap-1" role="group" aria-label="추출 대상">
              {STATUS_OPTS.map((o) => (
                <button
                  key={o.value}
                  type="button"
                  className={segCls(picked === o.value)}
                  aria-pressed={picked === o.value}
                  onClick={() => setPicked(o.value)}
                >
                  {o.label}
                </button>
              ))}
            </span>
          </div>
        )}
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
        <div className={FIELD}>
          <span>
            {statusLabel} 처리일 <span className="text-muted">(선택 안 함 = 전체)</span>
          </span>
          <div className="flex items-center gap-1.5">
            <input
              type="date"
              className={INPUT}
              value={dateFrom}
              max={dateTo || DATE_MAX}
              onChange={(e) => setDateFrom(e.target.value)}
              aria-label={`${statusLabel} 처리일 시작`}
            />
            <span className="text-muted text-[13px]">~</span>
            <input
              type="date"
              className={INPUT}
              value={dateTo}
              min={dateFrom || undefined}
              max={DATE_MAX}
              onChange={(e) => setDateTo(e.target.value)}
              aria-label={`${statusLabel} 처리일 종료`}
            />
          </div>
        </div>
        {/* pt-6 = 라벨 줄 높이(text-[13px]×1.5≈20px) + gap-1(4px) — input 상단에 버튼 정렬 */}
        <div className="pt-6">
          <button className={BTN_EXPORT} type="button" disabled={busy} onClick={() => void download()}>
            {busy ? (
              "추출 중…"
            ) : (
              /* 대상(확정/거부)을 버튼에도 박아둔다 — 토글이 행 반대편이라 라벨이 '엑셀
                 다운로드'뿐이면 토글을 바꾼 걸 잊고 반대 파일을 받기 쉽다. */
              <span className="inline-flex items-center gap-1">
                {statusLabel}분 다운로드 <Download size={14} aria-hidden />
              </span>
            )}
          </button>
        </div>
      </div>
    </section>
  );
}
