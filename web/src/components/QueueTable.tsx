import { memo, useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { ArrowDown, ArrowUp, ChevronsUpDown, ExternalLink, FileText, Pencil } from "lucide-react";
import type { Listed, ReviewItem } from "../types";
import { BTN, BTN_CONFIRM, BTN_REJECT, EMPTY, LINK_FOCUS, TD, TH } from "../ui";
import { normSiteUrl, safeHref, tri } from "../format";
import { CandidateRadios, EmailBadge, StatusBadge } from "./StatusBadge";
import { SiteExplorer, type SiteTab } from "./SiteExplorer";

// 상태별 행 좌측 색 띠(첫 칸에 inset 그림자로 표현) — 한눈에 스캔.
const STRIPE: Record<ReviewItem["status"], string> = {
  pending: "shadow-[inset_3px_0_0_var(--color-warn)]",
  confirmed: "shadow-[inset_3px_0_0_var(--color-ok)]",
  rejected: "shadow-[inset_3px_0_0_var(--color-danger)]",
};

// 컬럼 폭(table-auto) — 고정 % 대신 핵심 컬럼만 최소폭을 주고 나머지는 내용 길이에
// 맞춰 늘어난다. TD 기본이 [overflow-wrap:anywhere] 라 폭이 부족하면 셀 안에서 접힌다
// — 내용이 긴 가변 컬럼(국가·업종·메일·사이트·상태)은 nowrap 을 걸지 않아 표가 항상
// 컨테이너 폭에 맞고 가로 스크롤이 생기지 않는다(뱃지·MX/SMTP 등 토큰 단위는 각자
// nowrap 이라 단위 사이에서만 줄이 바뀐다). 상장여부만 짧은 고정 어휘라 한 줄로 잠근다.
// 업체명은 min-w 만 주고 셀 안에서 줄바꿈(전문은 title). 액션은 flex(줄바꿈 없음)+min-w 로 한 줄 고정.
const COL_W = [
  "min-w-[120px]", // 업체명(셀 내부 줄바꿈)
  "", // 국가
  "", // 업종
  "whitespace-nowrap", // 상장여부
  "min-w-[240px]", // 이메일 후보(편집 입력 포함 — 넓게 유지)
  "", // 메일(뱃지·MX·SMTP — 단위 사이에서만 줄바꿈)
  "", // 사이트
  "whitespace-nowrap", // 문의폼 유무
  "", // 상태(뱃지는 자체 nowrap — 담당자·시각이 아래로 접힘)
  "min-w-[120px]", // 액션(버튼 2개 가로 고정 — flex 줄바꿈 없음)
];
const HEADERS = [
  "업체명",
  "국가",
  "업종",
  "상장여부",
  "이메일 후보",
  "메일",
  "사이트",
  "문의폼",
  "상태",
  "액션",
];

// 상장여부 표기·정렬 순서 — 필터 셀렉트 어휘(상장/비상장/미상)와 동일, 상장 먼저.
const LISTED_LABEL: Record<Listed, string> = { listed: "상장", unlisted: "비상장", unknown: "미상" };
const LISTED_RANK: Record<Listed, number> = { listed: 0, unlisted: 1, unknown: 2 };

// 상태 정렬 순서(pending 먼저 = 처리해야 할 것 위로). 알파벳순보다 업무 흐름에 맞다.
const STATUS_RANK: Record<ReviewItem["status"], number> = {
  pending: 0,
  confirmed: 1,
  rejected: 2,
};

// 컬럼별 정렬 키. 없는 인덱스(이메일 후보·메일·사이트·액션)는 정렬 불가
// — 메일/사이트는 뱃지·링크라 자연스러운 대소 순서가 없어 제외.
const SORT_KEY: Record<number, (it: ReviewItem) => string | number> = {
  0: (it) => it.name,
  1: (it) => it.country,
  2: (it) => it.industry,
  3: (it) => LISTED_RANK[it.listed],
  7: (it) => (it.form ? 0 : 1), // 문의폼 있음이 먼저
  8: (it) => STATUS_RANK[it.status],
};

type Sort = { col: number; dir: "asc" | "desc" };

interface Props {
  items: ReviewItem[];
  busyIds: Set<string>;
  doneCount: number; // 이번 세션 처리 건수(진행률 바 분자)
  remaining: number; // 남은 작업 건수(호출부 기준 — 내 작업=내 잔여분, 전체큐=필터 반영 total). 분모 = doneCount + remaining
  // 성공(처리 완료) 시 true 를 resolve — 팝업에서 '성공해야 다음 행 전진' 판단에 쓴다.
  // homepage = 사람이 수정한 사이트 URL(유효 http(s)·원본과 다를 때만, 없으면 undefined).
  // hasForm = 사람이 교정한 문의폼 유무(감지값과 다를 때만, 없으면 undefined).
  onConfirm: (id: string, selected?: string, homepage?: string, hasForm?: boolean) => Promise<boolean>;
  onReject: (id: string) => Promise<boolean>;
  emptyText?: string; // 빈 목록 안내 — 화면 맥락별 문구(생략 시 기본).
}

// URL 에서 표시용 호스트(도메인)를 뽑는다(www. 제거). 실패 시 원문.
function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

// 사이트 링크 클릭 — 수정키(Ctrl/Cmd/Shift)·가운데 클릭은 브라우저 기본(새 탭)을 살리고,
// 평범한 좌클릭만 가로채 페이지 내 미리보기 창을 연다.
function openOrTab(e: MouseEvent, open: () => void) {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
  e.preventDefault();
  open();
}

// ISO8601 → 월-일 시:분(처리 시각 축약 표기). 파싱 실패면 빈 문자열.
function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

interface RowProps {
  item: ReviewItem;
  busy: boolean;
  choice: string | undefined;
  // 사용자가 편집 중인 사이트 URL — undefined 면 편집기 닫힘(기본 한 줄 유지).
  site: string | undefined;
  formChecked: boolean; // 문의폼 유무 체크박스 표시값(override 없으면 감지값).
  onPick: (id: string, value: string) => void;
  onEditSite: (id: string, value: string) => void;
  onCancelSite: (id: string) => void; // 편집 취소 — 입력을 닫고 원본 링크로 복귀.
  onToggleForm: (id: string, value: boolean) => void;
  onConfirm: (id: string, selected?: string) => void;
  onReject: (id: string) => void;
  onOpen: (id: string, tab: SiteTab) => void;
}

// 행은 memo — item·busy·choice 가 같으면 리렌더를 건너뛴다(한 행 처리 시 나머지 행이
// 불필요하게 다시 그려지지 않음). 핸들러 동일성은 비교에서 제외한다: 부모가 인라인
// 핸들러를 매 렌더 새로 만들어도 논리는 동일하고, 행은 item 이 바뀔 때 최신 핸들러로
// 갱신되므로 안전하다(필터 변경 시 item 객체가 새로 와 자연히 재렌더).
const QueueRow = memo(
  function QueueRow({
    item,
    busy,
    choice,
    site,
    formChecked,
    onPick,
    onEditSite,
    onCancelSite,
    onToggleForm,
    onConfirm,
    onReject,
    onOpen,
  }: RowProps) {
    const done = item.status !== "pending";
    // confirmed 만 편집 잠금 — rejected 는 재작업(이메일 수정 → 재확정 번복) 가능 상태로
    // 열어둔다(BE 도 전이 제한 없이 감사기록과 함께 허용).
    const locked = item.status === "confirmed";
    // 링크는 편집 중 값을 따른다(무효 입력이면 링크가 사라져 잘못된 URL 임이 바로 보인다).
    const href = site !== undefined ? normSiteUrl(site.trim()) : safeHref(item.homepage);
    const formHref = safeHref(item.form);
    return (
      <tr className={`hover:bg-white/[0.03] ${done ? "opacity-60" : ""}`}>
        {/* 업체명은 truncate(nowrap) 대신 줄바꿈 — nowrap 이면 긴 사명이 컬럼 최소폭을
            고정시켜 좁은 화면에서 표가 줄지 못하고 가로 스크롤을 만든다. */}
        <td className={`${TD} ${COL_W[0]} font-semibold ${STRIPE[item.status]}`} title={item.name}>
          {item.name}
        </td>
        <td className={`${TD} ${COL_W[1]}`}>{item.country}</td>
        <td className={`${TD} ${COL_W[2]}`}>{item.industry}</td>
        <td className={`${TD} ${COL_W[3]} ${item.listed === "unknown" ? "text-muted" : ""}`}>
          {LISTED_LABEL[item.listed]}
          {/* 시장 보드(#136) — 별도 컬럼 대신 상장여부에 병기(정보 밀도 절제). */}
          {item.market && <span className="text-muted text-xs ml-1">{item.market}</span>}
        </td>
        <td className={`${TD} ${COL_W[4]} font-mono text-[13px]`}>
          {item.candidates.length > 1 && (
            <div className="flex flex-col gap-1">
              <CandidateRadios
                candidates={item.candidates}
                name={`sel-${item.id}`}
                choice={choice}
                disabled={locked}
                onPick={(v) => onPick(item.id, v)}
              />
            </div>
          )}
          {/* 확정 전 이메일 직접 수정/입력 — 후보 선택값을 채우되 사람이 덮어쓸 수 있다.
              폼만 있던(후보 0) 행엔 새 이메일 추가도 가능. */}
          <input
            className="w-full mt-1 bg-canvas border border-line text-ink font-mono text-xs py-1 px-1.5 rounded focus:outline-none focus:border-accent disabled:opacity-50"
            type="email"
            value={choice ?? ""}
            disabled={locked}
            placeholder="이메일 직접 입력/수정"
            onChange={(e) => onPick(item.id, e.target.value)}
            title="확정 전 이메일을 수정하거나 직접 입력할 수 있습니다"
          />
        </td>
        <td className={`${TD} ${COL_W[5]} text-xs`}>
          <EmailBadge status={item.email_status} />
          <span className="text-muted ml-2 whitespace-nowrap">MX {tri(item.email_mx)}</span>
          <span className="text-muted ml-2 whitespace-nowrap">SMTP {tri(item.email_smtp)}</span>
        </td>
        <td className={`${TD} ${COL_W[6]}`}>
          {href ? (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              className={`${LINK_FOCUS} ${item.site_alive ? "text-accent" : "text-muted line-through"}`}
              title={item.site_alive ? "클릭: 미리보기 창 (Ctrl+클릭: 새 탭)" : "사이트 미응답"}
              onClick={(e) => openOrTab(e, () => onOpen(item.id, "home"))}
            >
              <span className="inline-flex items-center gap-1">
                <ExternalLink size={13} className="flex-none" aria-hidden /> {hostOf(href)}
              </span>
            </a>
          ) : (
            <span className="text-muted">—</span>
          )}
          {/* 사이트 URL 편집 — 이메일과 달리 링크가 주 기능이고 편집은 예외적 교정이라 상시
              입력 대신 연필 토글로 연다(기본 한 줄 유지). 사이트 없던 행엔 새 URL 추가도 가능.
              편집값은 확정 시 함께 반영. confirmed 잠금은 연필 자체를 숨긴다. */}
          {!locked && (
            <button
              type="button"
              className={`${LINK_FOCUS} text-muted hover:text-ink align-middle ml-1.5`}
              title={site === undefined ? "사이트 URL 수정/입력" : "편집 취소"}
              onClick={() =>
                site === undefined
                  ? onEditSite(item.id, item.homepage ?? "")
                  : onCancelSite(item.id)
              }
            >
              <Pencil size={12} aria-hidden />
              <span className="sr-only">사이트 URL 편집</span>
            </button>
          )}
          {formHref && (
            <div>
              <a
                href={formHref}
                target="_blank"
                rel="noreferrer"
                className={`${LINK_FOCUS} text-accent`}
                title="클릭: 미리보기 창 (Ctrl+클릭: 새 탭)"
                onClick={(e) => openOrTab(e, () => onOpen(item.id, "form"))}
              >
                <span className="inline-flex items-center gap-1">
                  <FileText size={13} className="flex-none" aria-hidden /> 문의폼
                </span>
              </a>
            </div>
          )}
          {site !== undefined && !locked && (
            <input
              autoFocus
              className="w-full mt-1 bg-canvas border border-line text-ink font-mono text-xs py-1 px-1.5 rounded focus:outline-none focus:border-accent"
              type="url"
              value={site}
              placeholder="사이트 URL 직접 입력/수정"
              onChange={(e) => onEditSite(item.id, e.target.value)}
              onKeyDown={(e) => e.key === "Escape" && onCancelSite(item.id)}
              title="확정 시 함께 반영됩니다 (Esc: 취소)"
            />
          )}
        </td>
        <td className={`${TD} ${COL_W[7]}`}>
          <div className="flex justify-center">
            <input
              type="checkbox"
              checked={formChecked}
              disabled={locked}
              title="문의폼 유무를 직접 교정합니다(확정 시 함께 반영)"
              onChange={(e) => onToggleForm(item.id, e.target.checked)}
            />
          </div>
        </td>
        <td className={`${TD} ${COL_W[8]}`}>
          <StatusBadge status={item.status} />
          {item.assignee && (
            <span className="text-muted text-xs" title={item.reviewed_at ?? undefined}>
              {" · "}
              {item.assignee}
              {item.reviewed_at && (
                <small className="text-muted text-[11px] ml-0.5"> {fmtTime(item.reviewed_at)}</small>
              )}
            </span>
          )}
        </td>
        <td className={`${TD} ${COL_W[9]}`}>
          {/* 버튼 라벨은 TD 의 overflow-wrap:anywhere 상속으로 '확/정' 처럼 세로로
              접힐 수 있어 nowrap 으로 잠근다(액션 셀 한정). */}
          <div className="flex gap-1.5 whitespace-nowrap">
            <button
              className={BTN_CONFIRM}
              disabled={busy || item.status === "confirmed"}
              onClick={() => onConfirm(item.id, choice?.trim() ? choice.trim() : undefined)}
            >
              확정
            </button>
            <button
              className={BTN_REJECT}
              disabled={busy || item.status === "rejected"}
              onClick={() => onReject(item.id)}
            >
              거부
            </button>
          </div>
        </td>
      </tr>
    );
  },
  (prev, next) =>
    prev.item === next.item &&
    prev.busy === next.busy &&
    prev.choice === next.choice &&
    prev.site === next.site &&
    prev.formChecked === next.formChecked,
);

// 검증 큐 표 — 회사/이메일 후보(다중 선택)/메일 검증 신호/상태/액션.
export function QueueTable({
  items,
  busyIds,
  doneCount,
  remaining,
  onConfirm,
  onReject,
  emptyText,
}: Props) {
  // 행별 선택(라디오) — 서버 selected 를 기본값으로, 사용자가 바꾸면 덮어쓴다.
  const [picked, setPicked] = useState<Record<string, string>>({});
  const onPick = useCallback((id: string, value: string) => {
    setPicked((p) => ({ ...p, [id]: value }));
  }, []);

  // 행별 사이트 URL 편집값 — 키 존재 = 편집기 열림(연필 토글). 취소하면 키를 지워 닫는다.
  const [sites, setSites] = useState<Record<string, string>>({});
  const onEditSite = useCallback((id: string, value: string) => {
    setSites((p) => ({ ...p, [id]: value }));
  }, []);
  const onCancelSite = useCallback((id: string) => {
    setSites((p) => {
      const next = { ...p };
      delete next[id];
      return next;
    });
  }, []);
  // 확정에 실을 사이트 수정값 — 편집했고, 정규화 후 유효하며, 원본과 다를 때만(그 외 undefined
  // = 변경 없음). 정규화가 스킴만 보충한 경우(원본과 동일)도 변경 없음으로 취급된다.
  const editedSite = useCallback(
    (it: ReviewItem): string | undefined => {
      const raw = sites[it.id];
      if (raw === undefined) return undefined;
      const norm = normSiteUrl(raw.trim());
      return norm && norm !== it.homepage ? norm : undefined;
    },
    [sites],
  );

  // 행별 문의폼 유무 교정 — 감지값(!!form)과 다를 때만 override 를 갖는다.
  const [formOverride, setFormOverride] = useState<Record<string, boolean>>({});
  const onToggleForm = useCallback((id: string, value: boolean) => {
    setFormOverride((p) => ({ ...p, [id]: value }));
  }, []);
  const formChecked = useCallback(
    (it: ReviewItem): boolean => formOverride[it.id] ?? !!it.form,
    [formOverride],
  );
  // 확정에 실을 문의폼 유무 수정값 — 감지값과 다를 때만(그 외 undefined = 변경 없음).
  const editedForm = useCallback(
    (it: ReviewItem): boolean | undefined => {
      const v = formOverride[it.id];
      return v === undefined || v === !!it.form ? undefined : v;
    },
    [formOverride],
  );

  // 컬럼 정렬 — 같은 컬럼 재클릭 시 asc↔desc 토글, 3번째 클릭이면 해제(원본 순서 복귀).
  const [sort, setSort] = useState<Sort | null>(null);
  const onSort = useCallback((col: number) => {
    setSort((s) => {
      if (s?.col !== col) return { col, dir: "asc" };
      if (s.dir === "asc") return { col, dir: "desc" };
      return null;
    });
  }, []);
  const sorted = useMemo(() => {
    if (!sort) return items;
    const key = SORT_KEY[sort.col];
    const sign = sort.dir === "asc" ? 1 : -1;
    return [...items].sort((a, b) => {
      const va = key(a);
      const vb = key(b);
      const c =
        typeof va === "number" && typeof vb === "number"
          ? va - vb
          : String(va).localeCompare(String(vb), "ko");
      return c * sign;
    });
  }, [items, sort]);

  // 행 확정은 2단계 — 클릭 시 바로 확정하지 않고 확인 모달을 거친다(실수 확정 방지,
  // SiteExplorer 의 Enter/클릭 확정 오버레이와 동일 단계 수). 거부는 pending 이면 1클릭
  // (#175 — 가볍고 되돌릴 수 있음)이지만, confirmed 행의 거부는 확정 번복(엑셀 추출 대상
  // 이탈)이라 같은 모달을 거친다. null 이면 모달 닫힘.
  const [modal, setModal] = useState<{ id: string; action: "confirm" | "reject" } | null>(null);

  // 확인 모달 동안 Esc 닫기(window 레벨 — Tab 으로 포커스가 배경에 새도 동작)와 배경
  // 스크롤 잠금(SiteExplorer 와 동일). 닫히면 원복.
  useEffect(() => {
    if (!modal) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setModal(null);
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [modal]);

  // 사이트 미리보기 창 — 열린 행 id 와 초기 탭(홈/문의폼). 닫히면 null.
  const [open, setOpen] = useState<{ id: string; tab: SiteTab } | null>(null);
  const onOpen = useCallback((id: string, tab: SiteTab) => setOpen({ id, tab }), []);

  // 항상 최신 items 를 가리키는 ref — 전진은 처리 await(목록 리필) 후에 일어나므로
  // 콜백 클로저의 옛 items 가 아닌 갱신된 목록에서 다음 행을 찾아야 한다.
  // 표시 순서(sorted)를 가리킨다 — 팝업 '다음 행 전진'이 화면에 보이는 순서를 따르도록.
  const itemsRef = useRef(sorted);
  itemsRef.current = sorted;

  // 팝업에서 완료(확정/거부) 시 성공했을 때만 다음 행으로 전진. 처리 전 목록에서 '다음
  // pending(홈/폼 링크 보유) 행' id 를 미리 잡아두고(처리 후엔 방금 행이 빠져 위치 기준이
  // 무너짐), await 후 그 행이 아직 pending 이면 연다. 없으면 닫는다. 실패면 현재 항목 유지.
  const advanceAfter = useCallback(
    async (id: string, run: () => Promise<boolean>) => {
      const before = itemsRef.current;
      const idx = before.findIndex((it) => it.id === id);
      const nextId =
        before
          .slice(idx + 1)
          .find((it) => it.status === "pending" && (safeHref(it.homepage) || safeHref(it.form)))
          ?.id ?? null;
      if (!(await run())) return; // 실패(400/409 등) — 전진하지 않고 현재 항목·에러 유지
      const after = itemsRef.current;
      const target = nextId
        ? after.find((it) => it.id === nextId && it.status === "pending")
        : undefined;
      setOpen(target ? { id: target.id, tab: "home" } : null);
    },
    [],
  );
  const popupConfirm = useCallback(
    (id: string, selected?: string) =>
      advanceAfter(id, () => {
        // 표에서 편집해 둔 사이트 URL·문의폼 유무가 있으면 팝업 확정에도 함께 실린다(편집 유실 방지).
        const it = itemsRef.current.find((x) => x.id === id);
        return onConfirm(id, selected, it ? editedSite(it) : undefined, it ? editedForm(it) : undefined);
      }),
    [advanceAfter, onConfirm, editedSite, editedForm],
  );
  const popupReject = useCallback(
    (id: string) => advanceAfter(id, () => onReject(id)),
    [advanceAfter, onReject],
  );

  if (items.length === 0) {
    return <p className={EMPTY}>{emptyText ?? "표시할 검증 항목이 없습니다."}</p>;
  }
  // 처리·필터 변경으로 항목이 목록에서 빠지면 창도 자연히 닫힌다(find 결과 없음).
  const openItem = open ? items.find((it) => it.id === open.id) : undefined;
  const modalItem = modal ? items.find((it) => it.id === modal.id) : undefined;
  // 모달에 보여주고 실제 확정에 쓸 이메일 — 행 버튼과 동일한 우선순위(사용자 선택→서버→첫 후보).
  const modalChoice = modalItem
    ? (picked[modalItem.id] ?? modalItem.selected ?? modalItem.candidates[0]?.value)?.trim()
    : undefined;
  // 확정에 실릴 사이트 수정값(없으면 undefined). raw 는 무효 입력 경고 판단용.
  const modalSite = modalItem ? editedSite(modalItem) : undefined;
  const modalSiteRaw = modalItem ? sites[modalItem.id]?.trim() : undefined;
  // 확정에 실릴 문의폼 유무 수정값(없으면 undefined).
  const modalForm = modalItem ? editedForm(modalItem) : undefined;
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse bg-panel border border-line rounded-lg overflow-hidden">
        <thead>
          <tr>
            {HEADERS.map((h, i) => {
              const sortable = i in SORT_KEY;
              const active = sort?.col === i;
              return (
                <th
                  key={h}
                  className={`${TH} ${COL_W[i]}`}
                  aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
                >
                  {sortable ? (
                    <button
                      type="button"
                      className={`${LINK_FOCUS} inline-flex items-center gap-1 uppercase tracking-[0.04em] ${active ? "text-ink" : "hover:text-ink"}`}
                      onClick={() => onSort(i)}
                      title="클릭하여 정렬 (오름차순 → 내림차순 → 해제)"
                    >
                      {h}
                      {active ? (
                        sort.dir === "asc" ? (
                          <ArrowUp size={12} className="flex-none" aria-hidden />
                        ) : (
                          <ArrowDown size={12} className="flex-none" aria-hidden />
                        )
                      ) : (
                        <ChevronsUpDown size={12} className="flex-none opacity-40" aria-hidden />
                      )}
                    </button>
                  ) : (
                    h
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((it) => (
            <QueueRow
              key={it.id}
              item={it}
              busy={busyIds.has(it.id)}
              choice={picked[it.id] ?? it.selected ?? it.candidates[0]?.value}
              site={sites[it.id]}
              formChecked={formChecked(it)}
              onPick={onPick}
              onEditSite={onEditSite}
              onCancelSite={onCancelSite}
              onToggleForm={onToggleForm}
              onConfirm={(id) => setModal({ id, action: "confirm" }) /* 즉시 확정 대신 확인 모달 */}
              // confirmed 행 거부 = 확정 번복 — 확인 모달 경유. pending 거부는 기존대로 1클릭.
              onReject={(id) =>
                it.status === "confirmed" ? setModal({ id, action: "reject" }) : void onReject(id)
              }
              onOpen={onOpen}
            />
          ))}
        </tbody>
      </table>
      {/* 행 확정/번복 거부 확인 모달 — SiteExplorer 확정 오버레이와 동일 톤. Esc·바깥
          클릭=취소, 실행 버튼에 autoFocus 라 Enter 로도 실행된다. */}
      {modal && modalItem && (
        <div
          className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4"
          onClick={() => setModal(null)}
        >
          <div
            role="dialog"
            aria-modal="true"
            className="bg-panel border border-line rounded-lg p-5 max-w-sm w-full text-center flex flex-col gap-4"
            onClick={(e) => e.stopPropagation()}
          >
            <p className="text-ink text-sm leading-relaxed">
              {modal.action === "confirm" ? (
                <>
                  <span className="font-semibold">{modalItem.name}</span> 을(를) 확정하시겠습니까?
                  {modalItem.status === "rejected" && (
                    <span className="block mt-1 text-muted text-xs">
                      거부된 항목을 확정으로 번복합니다.
                    </span>
                  )}
                  {modalChoice && (
                    <span className="block mt-1 font-mono text-xs text-muted [overflow-wrap:anywhere]">
                      {modalChoice}
                    </span>
                  )}
                  {modalSite && (
                    <span className="block mt-1 font-mono text-xs text-muted [overflow-wrap:anywhere]">
                      사이트 → {modalSite}
                    </span>
                  )}
                  {modalForm !== undefined && (
                    <span className="block mt-1 text-xs text-muted">
                      문의폼 → {modalForm ? "있음" : "없음"}
                    </span>
                  )}
                  {/* 편집했지만 반영될 변경이 없는 경우(무효 URL·정규화 후 원본과 동일)
                      — 조용히 버리지 않고 미반영을 알린다. */}
                  {modalSiteRaw !== undefined &&
                    !modalSite &&
                    modalSiteRaw !== (modalItem.homepage ?? "") && (
                      <span className="block mt-1 text-xs text-danger-fg">
                        사이트 수정값이 유효한 변경이 아니어서 반영되지 않습니다
                      </span>
                    )}
                </>
              ) : (
                <>
                  <span className="font-semibold">{modalItem.name}</span> 의 확정을 거부로
                  번복하시겠습니까?
                  <span className="block mt-1 text-muted text-xs">
                    엑셀 추출 대상에서 제외됩니다.
                  </span>
                </>
              )}
            </p>
            <div className="flex gap-2">
              <button
                autoFocus
                className={`${modal.action === "confirm" ? BTN_CONFIRM : BTN_REJECT} flex-1`}
                // 행 버튼과 동일 기준의 상태 가드 — 모달 열린 사이 백그라운드 갱신으로 상태가
                // 바뀐 항목의 중복 처리를 막는다(확정 모달은 이미 confirmed, 번복 거부 모달은
                // confirmed 가 아니게 된 경우). rejected→confirmed 재확정 번복은 허용(BE 동일).
                disabled={
                  busyIds.has(modalItem.id) ||
                  (modal.action === "confirm"
                    ? modalItem.status === "confirmed"
                    : modalItem.status !== "confirmed")
                }
                onClick={() => {
                  setModal(null);
                  if (modal.action === "confirm")
                    void onConfirm(modalItem.id, modalChoice || undefined, modalSite, modalForm);
                  else void onReject(modalItem.id);
                }}
              >
                {modal.action === "confirm" ? "확정" : "거부"}
              </button>
              <button className={`${BTN} flex-1`} onClick={() => setModal(null)}>
                취소
              </button>
            </div>
          </div>
        </div>
      )}
      {open && openItem && (
        <SiteExplorer
          item={openItem}
          doneCount={doneCount}
          remaining={remaining}
          tab={open.tab}
          choice={picked[openItem.id] ?? openItem.selected ?? openItem.candidates[0]?.value}
          site={sites[openItem.id]}
          busy={busyIds.has(openItem.id)}
          onTab={(tab) => setOpen({ id: openItem.id, tab })}
          onPick={onPick}
          onEditSite={onEditSite}
          onConfirm={popupConfirm}
          onReject={popupReject}
          onClose={() => setOpen(null)}
        />
      )}
    </div>
  );
}
