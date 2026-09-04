import { useEffect, useState } from "react";
import { fetchCountries, fetchCrawlTarget, fetchIndustries, saveCrawlTarget } from "../../api";
import type { CrawlTarget, Listed } from "../../types";
import { toast } from "sonner";
import { errMsg } from "../../format";
import { toCountryOpts } from "../../filterOptions";
import { MultiPicker, type PickerOption } from "../MultiPicker";
import { ErrorBox } from "../ErrorBox";
import { BTN_CONFIRM } from "../../ui";
import {
  SECTION_H2,
  FIELD,
  FIELD_INLINE,
  INPUT_WIDE,
  CRAWL_TARGET,
  LISTED_TARGET_OPTIONS,
  fmt,
} from "./shared";

// 전 업종 CSV ↔ 빈값(=전체) 접기 — BE 는 빈 업종을 422 로 거부하므로(과도 발견 방지)
// 빈 선택은 전송 시 전 업종 CSV 로 확장하고, 표시할 땐 전 업종 일치 시 빈값으로 되돌려
// 발송/추출 섹션과 같은 '선택 안 함 = 전체' UI 를 유지한다.
function collapseAllIndustries(csv: string, opts: PickerOption[]): string {
  const picked = new Set(
    csv
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  );
  // 상위집합도 '전체'로 접는다 — 현 옵션을 모두 덮으면 의미상 전체이고, 어휘가 줄어든
  // 뒤 남은 구 라벨 잔여분도 다음 재저장에서 자연 정리된다(정확일치 요구 시 회복 불가).
  const isAll = opts.length > 0 && opts.every((o) => picked.has(o.value));
  return isAll ? "" : csv;
}

// 일일 크롤 타깃 — 스케줄러가 매일 읽어 세그먼트를 만드는 값(GET/PUT /admin/crawl-target).
// 웹 즉시크롤(실행·진행폴링·중지)은 2026-09-01 제거됐다(#448, BE #450) — 지금 바로 발견/추출을
// 돌리는 진입점은 아래 '세그먼트 작업요청'(큐·재개·커서 포함 상위호환)이다. 여기 남은 건
// 저장 하나뿐이라, 저장 전 상태를 사용자가 확인할 수 있게 **현재 저장값을 그대로 채운다**.
export function CrawlTargetSection() {
  const [countryOpts, setCountryOpts] = useState<PickerOption[]>([]);
  const [industryOpts, setIndustryOpts] = useState<PickerOption[]>([]);
  const [countries, setCountries] = useState("");
  const [industries, setIndustries] = useState("");
  const [listed, setListed] = useState<Listed>("unknown");
  const [persist, setPersist] = useState(true);
  const [savedAt, setSavedAt] = useState<{ by: string | null; at: string | null } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); // 저장 왕복 중(이중 클릭 방지)

  const apply = (t: CrawlTarget, opts: PickerOption[]) => {
    setCountries(t.countries);
    setIndustries(collapseAllIndustries(t.industries, opts));
    setListed(t.listed);
    setPersist(t.persist);
    setSavedAt({ by: t.updated_by, at: t.updated_at });
  };

  useEffect(() => {
    let alive = true;
    Promise.all([fetchCrawlTarget(), fetchCountries(), fetchIndustries()])
      .then(([t, countryList, industryList]) => {
        if (!alive) return;
        setCountryOpts(toCountryOpts(countryList));
        setIndustryOpts(industryList);
        // 즉시크롤이 있던 시절엔 국가·업종을 일부러 비워 띄웠다(마지막 타깃으로 실수 재크롤
        // 하는 사고 — #186, 2026-07-06). 실행 버튼이 사라진 지금은 반대로 비워 두는 쪽이
        // 위험하다 — 빈 선택은 전 업종으로 확장돼 저장되므로, 사용자가 폼을 열어 저장만
        // 눌러도 기존 타깃이 '전체'로 덮이기 때문. 저장값을 그대로 보여준다.
        apply(t, industryList);
      })
      .catch((e) => alive && setErr(errMsg(e)));
    return () => {
      alive = false;
    };
  }, []);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      // 빈 선택(=전체)은 전 업종 CSV 로 확장해 전송(BE 는 빈 업종 422).
      const inds = industries.trim() || industryOpts.map((o) => o.value).join(",");
      const saved = await saveCrawlTarget({
        countries: countries.trim(),
        industries: inds,
        listed,
        persist,
      });
      apply(saved, industryOpts);
      // 저장 피드백 — 휘발성 정보라 인라인 문구 대신 토스트(자동 소멸).
      toast.success("크롤 타깃 저장");
    } catch (e2) {
      setErr(errMsg(e2));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <h2 className={SECTION_H2}>일일 크롤 타깃</h2>
      <p className="text-muted text-[13px] mt-0 mb-3">
        매일 자동 크롤이 사용할 범위입니다. 지금 바로 발견·추출을 돌리려면 아래 ‘세그먼트
        작업요청’을 사용하세요.
      </p>
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
            업종 <span className="text-muted">(선택 안 함 = 전체)</span>
          </span>
          <MultiPicker
            options={industryOpts}
            value={industries}
            onChange={setIndustries}
            placeholder="업종 검색 (예: 건설, construction)"
            emptyHint="전체 업종"
          />
        </div>
        <label className={FIELD}>
          상장여부
          <select
            className={INPUT_WIDE}
            value={listed}
            onChange={(e) => setListed(e.target.value as Listed)}
          >
            {LISTED_TARGET_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        {/* DB적재 체크박스는 '저장 시 DB에 넣을지' — 저장 동작의 옵션이라 상장여부(필터)가
            아니라 저장 버튼과 한 그룹으로 묶는다. */}
        <div className="flex flex-col gap-2">
          <label className={FIELD_INLINE}>
            <input
              type="checkbox"
              checked={persist}
              onChange={(e) => setPersist(e.target.checked)}
            />
            DB 적재(검증 큐로)
          </label>
          <div className="flex gap-2 mt-0.5">
            <button
              className={BTN_CONFIRM}
              type="submit"
              // 빈 선택은 전 업종 확장에 옵션 목록이 필요 — 미로드 상태만 잠깐 막는다.
              disabled={busy || (!industries.trim() && industryOpts.length === 0)}
            >
              {busy ? "저장 중…" : "타깃 저장"}
            </button>
          </div>
        </div>
      </form>
      {savedAt?.at && (
        <p className="text-muted text-xs mt-2 mb-0">
          마지막 저장: {fmt(savedAt.at)}
          {savedAt.by && ` · ${savedAt.by}`}
        </p>
      )}
    </section>
  );
}
