import { useState } from "react";
import { CrawlTargetSection } from "./admin/CrawlSection";
import { ExportSection } from "./admin/ExportSection";
import { SendSection } from "./admin/SendSection";
import { AccountsSection } from "./admin/AccountsSection";
import { tabCls } from "../ui";

type AdminGroup = "운영" | "관리";

// 관리자 페이지 셸 — 운영(크롤·추출·발송) / 관리(계정) 두 군 서브탭.
// ponytail: 4섹션 항상 렌더 트리에 유지 — CrawlSection 의 mount-effect(진행중 크롤 복원)·
//           3초 폴링이 마운트에 묶여 있어, 언마운트 시 새로고침 복원이 깨진다.
//           비활성 패널은 hidden 속성(display:none)으로만 숨긴다.
export function Admin() {
  const [group, setGroupState] = useState<AdminGroup>(() => {
    const saved = localStorage.getItem("admin.group");
    return saved === "관리" ? "관리" : "운영";
  });

  const setGroup = (g: AdminGroup) => {
    localStorage.setItem("admin.group", g);
    setGroupState(g);
  };

  return (
    <div className="flex flex-col gap-5">
      <nav role="tablist" aria-label="관리자 그룹" className="flex gap-1">
        {(["운영", "관리"] as AdminGroup[]).map((g) => (
          <button
            key={g}
            role="tab"
            aria-selected={group === g}
            aria-controls={`admin-panel-${g}`}
            id={`admin-tab-${g}`}
            className={tabCls(group === g)}
            onClick={() => setGroup(g)}
          >
            {g}
          </button>
        ))}
      </nav>

      {/* 운영 패널 — 항상 마운트, CSS(hidden) 토글만 */}
      <div
        id="admin-panel-운영"
        role="tabpanel"
        aria-labelledby="admin-tab-운영"
        hidden={group !== "운영"}
        className="flex flex-col gap-7"
      >
        <CrawlTargetSection />
        <ExportSection />
        <SendSection />
      </div>

      {/* 관리 패널 — 항상 마운트, CSS(hidden) 토글만 */}
      <div
        id="admin-panel-관리"
        role="tabpanel"
        aria-labelledby="admin-tab-관리"
        hidden={group !== "관리"}
        className="flex flex-col gap-7"
      >
        <AccountsSection />
      </div>
    </div>
  );
}
