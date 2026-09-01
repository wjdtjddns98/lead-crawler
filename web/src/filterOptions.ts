// 큐 필터 옵션 공용 — /queue/filters 1회 로드 훅 + 옵션 매핑 + 상장여부 어휘.
// 전체 큐(App)·내 작업(MyWork)·발송(SendSection)·추출(ExportSection)이 같은 로드/매핑을
// 반복하던 것을 한 곳으로 모았다.
import { useEffect, useState } from "react";
import { fetchQueueFilters, withUnclassified } from "./api";
import { errMsg } from "./format";
import type { PickerOption } from "./components/MultiPicker";
import type { CountryOption, Listed } from "./types";

// 상장여부 필터 — 빈값=전체 + Listed 3값(전체 큐·내 작업 공용 어휘).
export const LISTED_FILTER_OPTIONS: { value: "" | Listed; label: string }[] = [
  { value: "", label: "전체" },
  { value: "listed", label: "상장" },
  { value: "unlisted", label: "비상장" },
  { value: "unknown", label: "미상" },
];

// 국가 옵션 → 픽커 옵션(iso2=저장 토큰, code 로 흐리게 병기).
// 업종(IndustryOption)은 PickerOption 과 구조가 같아 매핑 없이 그대로 쓴다.
export function toCountryOpts(countries: CountryOption[]): PickerOption[] {
  return countries.map((c) => ({ value: c.iso2, label: c.label, code: c.iso2, aliases: c.aliases }));
}

// 지역 어휘(문자열 배열) → 픽커 옵션 — 저장값=표시값(이미 한글 시/도명이라 별칭 불필요).
export function toRegionOpts(regions: string[]): PickerOption[] {
  return regions.map((r) => ({ value: r, label: r }));
}

// 지역 필터 노출 조건 — 국가 CSV 에 KR 이 포함됐는지(지역은 KR 세그먼트 전용, #139/#243).
export function krInScope(countryCsv: string): boolean {
  return countryCsv.split(",").some((c) => c.trim().toUpperCase() === "KR");
}

interface QueueFilterOpts {
  countryOpts: PickerOption[];
  industryOpts: PickerOption[];
  markets: string[]; // 시장 보드 어휘 — BE 계약 확장 전엔 빈 배열(호출부가 폴백 처리).
  regionOpts: PickerOption[]; // 지역 어휘(KR 시/도, 실측값만) — 데이터 없으면 빈 배열.
}

// 검증 직원용 필터 옵션(국가+업종+시장)을 마운트 시 1회 로드한다.
// 업종엔 '미분류'(분류 실패 폴백값)를 보장 — BE 가 이미 내려주면 그대로(#115 중복 방지).
// 실패 시 onError 로 통지(생략하면 무시) — 옵션 없이도 화면 동작엔 지장 없다(픽커만 빈 채).
export function useQueueFilterOpts(onError?: (msg: string) => void): QueueFilterOpts {
  const [opts, setOpts] = useState<QueueFilterOpts>({
    countryOpts: [],
    industryOpts: [],
    markets: [],
    regionOpts: [],
  });
  useEffect(() => {
    let alive = true;
    fetchQueueFilters()
      .then((f) => {
        if (!alive) return;
        setOpts({
          countryOpts: toCountryOpts(f.countries),
          industryOpts: withUnclassified(f.industries),
          markets: f.markets ?? [],
          regionOpts: toRegionOpts(f.regions ?? []),
        });
      })
      .catch((e) => {
        if (alive) onError?.(errMsg(e));
      });
    return () => {
      alive = false;
    };
    // 1회 로드 — onError 는 마운트 시점 것을 쓴다(재구독 불필요).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return opts;
}
