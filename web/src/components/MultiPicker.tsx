import { useState } from "react";
import { X } from "lucide-react";

// 검색 + 체크리스트 멀티셀렉트(국가·업종 공용). 선택값은 쉼표구분 토큰 문자열로
// 직렬화한다(백엔드 계약 유지). 선택 0개면 빈 문자열.
export interface PickerOption {
  value: string; // 저장 토큰(국가 ISO2 / 업종 한글명)
  label: string; // 표시명
  code?: string; // 부가 코드(국가 ISO2 등 — 있으면 흐리게 표시)
  aliases?: string[]; // 검색용 별칭(영문 등)
}

export function MultiPicker({
  options,
  value,
  onChange,
  placeholder,
  emptyHint,
}: {
  options: PickerOption[];
  value: string;
  onChange: (csv: string) => void;
  placeholder: string;
  emptyHint: string;
}) {
  const [search, setSearch] = useState("");

  const selectedLower = new Set(
    value
      .split(",")
      .map((t) => t.trim().toLowerCase())
      .filter(Boolean),
  );
  const isSelected = (o: PickerOption) => selectedLower.has(o.value.toLowerCase());

  const toggle = (target: PickerOption) => {
    // options 순서(우선순위)를 보존해 재직렬화 — 토글 대상만 뒤집고 나머지는 유지.
    const next = options
      .filter((o) => (o.value === target.value ? !isSelected(target) : isSelected(o)))
      .map((o) => o.value);
    onChange(next.join(","));
  };

  const q = search.trim().toLowerCase();
  const filtered = q
    ? options.filter(
        (o) =>
          o.label.toLowerCase().includes(q) ||
          o.value.toLowerCase().includes(q) ||
          (o.code?.toLowerCase().includes(q) ?? false) ||
          // 별칭 매칭 — 'UK'→영국, 'construction'→건설 등.
          (o.aliases ?? []).some((a) => a.toLowerCase().includes(q)),
      )
    : options;
  const selectedOpts = options.filter(isSelected);

  return (
    <div className="flex flex-col gap-1.5 w-[340px]">
      <input
        className="bg-canvas border border-line text-ink py-[7px] px-2.5 rounded-md w-full"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder={placeholder}
      />
      {selectedOpts.length > 0 ? (
        // 너비 고정(w-[280px]) + 칩은 아래로 줄바꿈 — 가로로 안 늘어 옆 UI 안 밀림. 칩 많으면
        // 피커만 세로로 길어진다(내부 스크롤 없음). 전체 해제는 칩 끝에 따라와 항상 보인다.
        <div className="flex flex-wrap gap-1.5 items-center min-h-[24px]">
          {selectedOpts.map((o) => (
            <button
              key={o.value}
              type="button"
              className="bg-canvas border border-line text-ink rounded-full py-[3px] px-2.5 text-xs cursor-pointer hover:border-muted"
              onClick={() => toggle(o)}
              title="제거"
            >
              <span className="inline-flex items-center gap-1">
                {o.label} <X size={12} className="text-muted flex-none" aria-hidden />
              </span>
            </button>
          ))}
          <button
            type="button"
            className="bg-transparent border-0 text-muted text-xs cursor-pointer underline"
            onClick={() => onChange("")}
          >
            전체 해제
          </button>
        </div>
      ) : (
        <p className="text-muted m-0 text-xs flex items-center min-h-[24px]">{emptyHint}</p>
      )}
      <ul className="list-none m-0 p-1 h-[200px] overflow-y-auto border border-line rounded-md bg-canvas">
        {filtered.map((o) => (
          <li key={o.value}>
            <label className="flex flex-row items-center gap-2 py-[5px] px-1.5 rounded cursor-pointer text-ink text-[13px] hover:bg-line">
              <input
                type="checkbox"
                className="min-w-0 m-0 flex-none"
                checked={isSelected(o)}
                onChange={() => toggle(o)}
              />
              <span className="flex-1">{o.label}</span>
              {o.code && <span className="text-muted text-[11px] tracking-[0.04em]">{o.code}</span>}
            </label>
          </li>
        ))}
        {filtered.length === 0 && (
          <li className="text-muted py-2 px-1.5 text-[13px]">검색 결과 없음</li>
        )}
      </ul>
    </div>
  );
}
