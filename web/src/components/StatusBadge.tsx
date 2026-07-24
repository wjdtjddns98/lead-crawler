import type { CandidateInfo, ReviewStatus } from "../types";

const LABEL: Record<ReviewStatus, string> = {
  pending: "대기",
  confirmed: "확정",
  rejected: "거부",
};

// 뱃지 공통 — 테두리 색은 각 상태가 지정(base 에는 폭만 둬 색 충돌 방지).
const BADGE = "inline-block px-2 py-0.5 rounded-[10px] text-xs border whitespace-nowrap";

const STATUS_CLS: Record<ReviewStatus, string> = {
  pending: "text-warn border-warn",
  confirmed: "text-ok-fg border-ok",
  rejected: "text-danger-fg border-danger",
};

// 큐 상태를 색상 뱃지로 표시.
export function StatusBadge({ status }: { status: ReviewStatus }) {
  return <span className={`${BADGE} ${STATUS_CLS[status]}`}>{LABEL[status]}</span>;
}

// 이메일 검증 상태(valid/risky/invalid/unknown/null)를 뱃지로 표시.
const EMAIL_CLS: Record<string, string> = {
  valid: "text-ok-fg border-ok",
  risky: "text-warn border-warn",
  invalid: "text-danger-fg border-danger",
  unknown: "text-muted border-line",
};

// 영문 상태값을 한눈에 인지되는 한글 라벨로 — 알 수 없는 값은 원문 그대로 노출(방어).
// 정상/불가는 형태가 확연히 달라(유효/무효처럼 1글자 차이 아님) 색 없이도 구분되고,
// 확정 판단(쓴다/조심/안 씀/모름)에 바로 대응된다.
const EMAIL_LABEL: Record<string, string> = {
  valid: "정상",
  risky: "주의",
  invalid: "불가",
  unknown: "미확인",
};

export function EmailBadge({ status }: { status: string | null }) {
  if (!status || !status.trim()) return <span className="text-muted">—</span>;
  // 알려진 어휘만 색상 클래스 적용, 그 외는 중립 처리(예상 밖 값 방어).
  const cls = EMAIL_CLS[status] ?? "text-muted border-line";
  const label = EMAIL_LABEL[status] ?? status;
  return <span className={`${BADGE} ${cls}`} title={status}>{label}</span>;
}

// 이메일 후보 라디오 목록 — 큐 행·사이트 탐색 사이드바 공용(같은 표기·같은 뱃지).
// 선택 상태(choice)와 반영(onPick)은 호출부 몫, name 은 행/모달별 라디오 그룹 구분용.
// removed(소문자 매칭)·onToggleRemove 를 주면 후보별 "이 주소 없음"(실존하지 않는 이메일
// 삭제, BE PR#314) 토글을 보인다 — 없으면 삭제 UI 를 숨긴다(하위 화면 하위호환).
export function CandidateRadios({
  candidates,
  name,
  choice,
  disabled,
  onPick,
  removed,
  onToggleRemove,
}: {
  candidates: CandidateInfo[];
  name: string;
  choice: string | undefined;
  disabled: boolean;
  onPick: (value: string) => void;
  removed?: string[];
  onToggleRemove?: (value: string) => void;
}) {
  // 삭제 표시는 대소문자 무시로 대조(BE 매칭과 동일) — 저장값 원형과 표기가 달라도 일치.
  const removedLc = new Set((removed ?? []).map((e) => e.toLowerCase()));
  return (
    <>
      {candidates.map((c) => {
        const isRemoved = removedLc.has(c.value.toLowerCase());
        return (
          <label
            key={c.value}
            className={`flex items-start gap-1.5 cursor-pointer ${isRemoved ? "opacity-60" : ""}`}
            title={c.value}
          >
            <input
              type="radio"
              className="cursor-pointer flex-none mt-0.5"
              name={name}
              checked={choice === c.value}
              // 삭제 표시된 후보는 선택 대상에서 제외(지운 주소를 selected 로 보내면 BE 400).
              disabled={disabled || isRemoved}
              onChange={() => onPick(c.value)}
            />
            <span
              className={`font-mono text-[13px] [overflow-wrap:anywhere] ${isRemoved ? "line-through text-muted" : ""}`}
            >
              {c.value}
            </span>
            <EmailBadge status={c.email_status} />
            {/* "이 주소 없음" — 후보+연락처에서 삭제(확정 시 반영). confirmed 잠금이면 숨김. */}
            {onToggleRemove && !disabled && (
              <button
                type="button"
                className={`ml-auto flex-none text-[11px] leading-none rounded border px-1.5 py-0.5 ${
                  isRemoved
                    ? "border-line text-muted hover:text-ink"
                    : "border-danger/50 text-danger-fg hover:bg-danger/10"
                }`}
                title={
                  isRemoved
                    ? "삭제 취소 — 후보로 되돌립니다"
                    : "이 주소는 실존하지 않음 — 확정 시 후보·연락처에서 삭제합니다"
                }
                onClick={(e) => {
                  // label 안의 버튼 — 라디오 선택으로 새지 않게 기본동작·전파를 막는다.
                  e.preventDefault();
                  e.stopPropagation();
                  onToggleRemove(c.value);
                }}
              >
                {isRemoved ? "되돌리기" : "없음"}
              </button>
            )}
          </label>
        );
      })}
    </>
  );
}
