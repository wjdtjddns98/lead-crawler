import { CrawlTargetSection } from "./admin/CrawlSection";
import { ExportSection } from "./admin/ExportSection";
import { SendSection } from "./admin/SendSection";
import { AccountsSection } from "./admin/AccountsSection";

// 관리자 페이지 셸 — 레이아웃(gap-7)과 섹션 조립만. 각 섹션이 자체 상태를 소유한다.
export function Admin() {
  return (
    <div className="flex flex-col gap-7">
      <CrawlTargetSection />
      <ExportSection />
      <SendSection />
      <AccountsSection />
    </div>
  );
}
