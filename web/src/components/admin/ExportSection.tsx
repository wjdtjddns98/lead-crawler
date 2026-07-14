import { useState } from "react";
import { exportConfirmed } from "../../api";
import { Download } from "lucide-react";
import { errMsg } from "../../format";
import { useQueueFilterOpts } from "../../filterOptions";
import { MultiPicker } from "../MultiPicker";
import { ErrorBox } from "../ErrorBox";
import { BTN_EXPORT } from "../../ui";
import { SECTION_H2, FIELD, CRAWL_TARGET } from "./shared";

// 확정분 엑셀 추출 — 국가/업종을 골라 선택 추출(빈 선택=전체). 전체 추출도 여기서
// (선택 없이 다운로드). 헤더의 '전체 확정분' 버튼은 중복이라 제거됨(2026-07-02).
// admin 콘솔 전용이 아니다 — MyWork(worker)도 재사용(#273, BE 가 role 로 본인 확정분만 내려줌).
export function ExportSection({ title = "확정분 엑셀 추출" }: { title?: string }) {
  const [countries, setCountries] = useState("");
  const [industries, setIndustries] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // 추출 범위 업종도 큐 행 저장 어휘(구분 택소노미+미분류)와 일치해야 매치된다(#115) —
  // 크롤 타깃용 /admin/industries(18키)가 아니라 /queue/filters 를 출처로 쓴다.
  const { countryOpts, industryOpts } = useQueueFilterOpts(setErr);

  const download = async () => {
    setBusy(true);
    setErr(null);
    try {
      await exportConfirmed(countries, industries);
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <h2 className={SECTION_H2}>{title}</h2>
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
        {/* pt-6 = 라벨 줄 높이(text-[13px]×1.5≈20px) + gap-1(4px) — input 상단에 버튼 정렬 */}
        <div className="pt-6">
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
