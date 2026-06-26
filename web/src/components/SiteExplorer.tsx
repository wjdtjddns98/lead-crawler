import { useEffect } from "react";
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

export type SiteTab = "home" | "form";

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

// 사이트 탐색 플로팅 윈도우 — 좌측 iframe 으로 사이트를 보며 우측 사이드바에서 곧바로
// 이메일을 선택/수정하고 확정·거부까지 끝낸다('복사→닫기→테이블 붙여넣기' 왕복 제거).
// 일부 사이트는 X-Frame-Options/CSP 로 임베드를 거부하므로 '새 탭 ↗' 을 항상 함께 둔다.
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
          <div className="flex-1 min-w-0 bg-canvas">
            {activeHref ? (
              // sandbox 에 allow-same-origin 을 의도적으로 빼 'allow-scripts + allow-same-origin'
              // escape-pair 를 차단한다. 어차피 교차출처(우리 앱과 다른 도메인)라 same-origin 을
              // 줘도 우리 앱 origin 엔 접근 못 하지만, 빼면 프레임 콘텐츠의 sandbox 무력화 경로
              // 자체가 사라진다. 사이트가 자기 origin 접근에 의존하면 '새 탭 ↗' 으로 폴백.
              <iframe
                key={activeHref}
                src={activeHref}
                title={`${item.name} 사이트`}
                className="w-full h-full border-0"
                sandbox="allow-scripts allow-forms allow-popups"
                referrerPolicy="no-referrer"
              />
            ) : (
              <div className="flex items-center justify-center h-full text-muted">
                표시할 사이트가 없습니다.
              </div>
            )}
          </div>

          <aside className="w-[340px] flex-none border-l border-line p-3 overflow-y-auto flex flex-col gap-3">
            <p className="text-muted text-xs leading-relaxed">
              사이트가 비어 보이면 임베드 차단입니다 — 상단 '새 탭 ↗' 으로 여세요. 찾은
              이메일을 아래에 입력/수정한 뒤 확정하세요.
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
