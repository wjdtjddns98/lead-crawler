import { useEffect, useRef, type MouseEvent } from "react";
import type { ReviewItem } from "../types";
import { BTN, BTN_CONFIRM, BTN_REJECT, tabCls } from "../ui";
import { EmailBadge } from "./StatusBadge";

// http(s) 만 허용(javascript:/data: 차단) — QueueTable 과 동일 정책(신뢰불가 URL 방어).
function safeHref(url: string | null): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

// 불리언/널 3-state 를 O/X/— 로 표시(QueueTable 과 동일 표기).
function tri(v: boolean | null): string {
  if (v === null) return "—";
  return v ? "O" : "X";
}

// 잡텍스트에서 첫 이메일을 뽑는다(끝 구두점 제거). 없으면 null. 팝업에서 줄째 복사해도
// 이메일만 골라 입력란에 채우기 위함.
function extractEmail(text: string): string | null {
  const m = text.match(/[^\s<>()[\]'"]+@[^\s<>()[\]'"]+\.[^\s<>()[\]'"]+/);
  return m ? m[0].replace(/[.,;:]+$/, "") : null;
}

export type SiteTab = "home" | "form";

// 팝업 창 크롬 최소화 — popup=yes 로 탭/툴바 없는 최소 창. location/menubar/status 는
// 브라우저가 가능한 만큼만 따른다(최신 브라우저는 보안상 주소창을 강제 표시할 수 있음).
const POPUP_FEATURES =
  "popup=yes,width=1100,height=820,location=no,toolbar=no,menubar=no,status=no";

// 슬롯 div 의 화면상 위치·크기에 팝업을 맞춰 연다(모달 안 사이트 영역과 동일 좌표·사이즈).
// 같은 창 이름은 재사용되며 features 를 무시하므로 열린 뒤 moveTo/resizeTo 로 다시 맞춘다.
// 뷰포트→스크린 변환의 크롬(주소창/툴바) 높이는 outer-inner 로 근사.
// ponytail: 크롬 높이 근사값 — 멀티모니터·줌·DevTools 에서 수십px 어긋나면 top 에 상수 보정.
function openSitePopup(url: string, slot: HTMLElement | null): Window | null {
  if (!slot) return window.open(url, "site-preview", POPUP_FEATURES);
  const r = slot.getBoundingClientRect();
  const left = Math.round(window.screenX + r.left);
  const top = Math.round(window.screenY + (window.outerHeight - window.innerHeight) + r.top);
  const width = Math.round(r.width);
  const height = Math.round(r.height);
  const feat =
    `popup=yes,location=no,toolbar=no,menubar=no,status=no,` +
    `left=${left},top=${top},width=${width},height=${height}`;
  const win = window.open(url, "site-preview", feat);
  win?.moveTo(left, top);
  win?.resizeTo(width, height);
  return win;
}

interface Props {
  item: ReviewItem;
  tab: SiteTab;
  choice: string | undefined;
  busy: boolean;
  onTab: (tab: SiteTab) => void;
  onPick: (id: string, value: string) => void;
  onConfirm: (id: string, selected?: string) => void;
  onReject: (id: string) => void;
  onClose: () => void;
}

// 사이트 탐색 플로팅 윈도우 — 우측 사이드바에서 곧바로 이메일을 선택/수정하고 확정·거부까지
// 끝낸다('복사→닫기→테이블 붙여넣기' 왕복 제거). 사이트 본문은 iframe 임베드를 거부하는 곳이
// 많아(X-Frame-Options/CSP) 별도 팝업 창으로 띄운다(크롬 최소화).
export function SiteExplorer({
  item,
  tab,
  choice,
  busy,
  onTab,
  onPick,
  onConfirm,
  onReject,
  onClose,
}: Props) {
  // Esc 로 닫기 + 모달 동안 배경 스크롤 잠금.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  const done = item.status !== "pending";
  const homeHref = safeHref(item.homepage);
  const formHref = safeHref(item.form);
  // 요청 탭에 URL 이 없으면 가능한 다른 쪽으로 폴백 — 탭 하이라이트·iframe 을 한 변수로
  // 묶어 '문의폼 요청했는데 폼 URL 이 없어 홈을 보여주며 탭은 무표시' 같은 불일치를 막는다.
  const activeTab: SiteTab = tab === "form" && formHref ? "form" : homeHref ? "home" : "form";
  const activeHref = activeTab === "form" ? formHref : homeHref;

  // 열림·탭 전환으로 activeHref 가 바뀌면 팝업을 열거나 같은 창을 새 URL 로 이동시킨다.
  // 모달이 닫히면(언마운트) 팝업도 같이 닫는다.
  const popupRef = useRef<Window | null>(null);
  const slotRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (activeHref) popupRef.current = openSitePopup(activeHref, slotRef.current);
  }, [activeHref]);
  useEffect(() => () => popupRef.current?.close(), []);

  // 사이드바 빈 영역 클릭 → 클립보드를 읽어 이메일만 자동 채움. 클릭은 사용자 제스처라
  // clipboard.readText() 가 허용된다(focus 이벤트는 제스처로 안 쳐줘 막혔던 부분). 입력·버튼·
  // 후보 라디오 클릭은 본래 동작을 살리려 건너뛴다. 이메일 형태가 아니면 건드리지 않는다.
  const emailRef = useRef<HTMLInputElement | null>(null);
  async function pasteFromClipboard(e: MouseEvent) {
    if (done) return;
    if ((e.target as HTMLElement).closest("input,button,a,label,select,textarea")) return;
    try {
      const email = extractEmail(await navigator.clipboard.readText());
      if (email) {
        onPick(item.id, email);
        emailRef.current?.focus();
      }
    } catch {
      // clipboard-read 권한 거부/미지원 — 무시(사용자가 입력란 클릭 후 Ctrl+V 로 폴백).
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`${item.name} 사이트 미리보기`}
        className="relative bg-panel border border-line rounded-lg w-[92vw] h-[86vh] max-w-[1400px] flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 헤더 — 업체명 · 탭(홈/문의폼) · 새 탭 · 닫기 */}
        <div className="flex items-center gap-2 px-3 py-2 border-b border-line">
          <span className="font-semibold text-ink truncate" title={item.name}>
            {item.name}
          </span>
          <div className="flex gap-1 ml-2">
            {homeHref && (
              <button className={tabCls(activeTab === "home")} onClick={() => onTab("home")}>
                홈페이지
              </button>
            )}
            {formHref && (
              <button className={tabCls(activeTab === "form")} onClick={() => onTab("form")}>
                문의폼
              </button>
            )}
          </div>
          <div className="ml-auto flex items-center gap-2">
            {activeHref && (
              <a className={BTN} href={activeHref} target="_blank" rel="noreferrer">
                새 탭 ↗
              </a>
            )}
            <button className={BTN} onClick={onClose} title="닫기 (Esc)">
              ✕
            </button>
          </div>
        </div>

        {/* 본문 — 좌: 사이트 iframe / 우: 이메일 편집 사이드바 */}
        <div className="flex flex-1 min-h-0">
          <div
            ref={slotRef}
            className="flex-1 min-w-0 bg-canvas flex flex-col items-center justify-center gap-3 p-6 text-center"
          >
            {activeHref ? (
              <>
                <p className="text-muted text-sm leading-relaxed">
                  사이트는 별도 팝업 창에서 열립니다. 팝업이 차단됐거나 닫혔다면 아래 버튼으로
                  다시 여세요.
                </p>
                <button
                  className={BTN}
                  onClick={() => (popupRef.current = openSitePopup(activeHref, slotRef.current))}
                >
                  사이트 팝업 열기 ↗
                </button>
              </>
            ) : (
              <span className="text-muted">표시할 사이트가 없습니다.</span>
            )}
          </div>

          <aside
            className="w-[340px] flex-none border-l border-line p-3 overflow-y-auto flex flex-col gap-3"
            onClick={pasteFromClipboard}
          >
            <p className="text-muted text-xs leading-relaxed">
              사이트에서 이메일 복사(Ctrl+C) 후 이 영역 빈 곳을 클릭하면 아래 입력란에 자동
              붙여넣어집니다. 직접 입력/수정도 가능합니다.
            </p>

            {/* 이메일 후보(라디오) — 여러 개면 골라 입력란을 채운다. */}
            {item.candidates.length > 0 && (
              <div className="flex flex-col gap-1">
                <span className="text-muted text-xs">이메일 후보</span>
                {item.candidates.map((c) => (
                  <label
                    key={c.value}
                    className="flex items-start gap-1.5 cursor-pointer"
                    title={c.value}
                  >
                    <input
                      type="radio"
                      className="cursor-pointer flex-none mt-0.5"
                      name={`exp-sel-${item.id}`}
                      checked={choice === c.value}
                      disabled={done}
                      onChange={() => onPick(item.id, c.value)}
                    />
                    <span className="font-mono text-[13px] [overflow-wrap:anywhere]">{c.value}</span>
                    <EmailBadge status={c.email_status} />
                  </label>
                ))}
              </div>
            )}

            {/* 이메일 직접 입력/수정 — 탐색 중 찾은 주소를 곧바로 반영. */}
            <label className="flex flex-col gap-1">
              <span className="text-muted text-xs">이메일(직접 입력/수정)</span>
              <input
                ref={emailRef}
                className="w-full bg-canvas border border-line text-ink font-mono text-sm py-1.5 px-2 rounded focus:outline-none focus:border-accent disabled:opacity-50"
                type="email"
                value={choice ?? ""}
                disabled={done}
                placeholder="exploring@company.com"
                onChange={(e) => onPick(item.id, e.target.value)}
              />
            </label>

            {/* 메일 검증 신호 — 입력값이 아닌 항목 자체의 기존 검증 결과. */}
            <div className="text-xs flex items-center gap-2 flex-wrap">
              <EmailBadge status={item.email_status} />
              <span className="text-muted whitespace-nowrap">MX {tri(item.email_mx)}</span>
              <span className="text-muted whitespace-nowrap">SMTP {tri(item.email_smtp)}</span>
            </div>

            {/* 확정 / 거부 — 처리 후 윈도우를 닫는다(테이블이 결과/오류를 반영). */}
            <div className="flex gap-2 mt-auto">
              <button
                className={`${BTN_CONFIRM} flex-1`}
                disabled={busy || item.status === "confirmed"}
                onClick={() => {
                  onConfirm(item.id, choice?.trim() ? choice.trim() : undefined);
                  onClose();
                }}
              >
                확정
              </button>
              <button
                className={`${BTN_REJECT} flex-1`}
                disabled={busy || item.status === "rejected"}
                onClick={() => {
                  onReject(item.id);
                  onClose();
                }}
              >
                거부
              </button>
            </div>
          </aside>
        </div>
      </div>
    </div>
  );
}
