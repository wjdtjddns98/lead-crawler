import { useState } from "react";

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
    <div className="multi-picker">
      <input
        className="picker-search"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder={placeholder}
      />
      {selectedOpts.length > 0 ? (
        <div className="picker-chips">
          {selectedOpts.map((o) => (
            <button
              key={o.value}
              type="button"
              className="chip"
              onClick={() => toggle(o)}
              title="제거"
            >
              {o.label} <span className="chip-x">×</span>
            </button>
          ))}
          <button type="button" className="chip-clear" onClick={() => onChange("")}>
            전체 해제
          </button>
        </div>
      ) : (
        <p className="muted picker-all">{emptyHint}</p>
      )}
      <ul className="picker-list">
        {filtered.map((o) => (
          <li key={o.value}>
            <label className="picker-item">
              <input type="checkbox" checked={isSelected(o)} onChange={() => toggle(o)} />
              <span className="picker-name">{o.label}</span>
              {o.code && <span className="muted picker-code">{o.code}</span>}
            </label>
          </li>
        ))}
        {filtered.length === 0 && <li className="muted picker-empty">검색 결과 없음</li>}
      </ul>
    </div>
  );
}
