#!/usr/bin/env node
// 브랜치 전략 하네스(2026-07-03 전환, CLAUDE.md §협업 워크플로):
//   dev=통합 트렁크(작업 PR 타깃) · prod=배포(dev→prod 승격 PR 전용) · main=동결(잠금).
// Claude Code PreToolUse 훅 — Bash/PowerShell 도구의 git push 가 main/prod 를 직접 겨누면
// 실행 전에 차단한다(exit 2). 서버측 보호(잠금·PR 필수)가 최종 방어선이고, 이 훅은
// 로컬에서 즉시 피드백을 주는 1차 가드다. stdin=Claude Code 훅 JSON.
let raw = "";
process.stdin.on("data", (c) => (raw += c));
process.stdin.on("end", () => {
  let cmd = "";
  try {
    cmd = JSON.parse(raw)?.tool_input?.command ?? "";
  } catch {
    process.exit(0); // 파싱 불가면 차단하지 않는다(오탐으로 작업을 막는 게 더 나쁨).
  }
  // 컴파운드 커맨드(&&·;·| 체인)에서 push 가 아닌 구간의 'prod' 토큰(예: gh pr create
  // --base prod)을 오탐하지 않도록, git push 가 든 세그먼트 안의 토큰만 검사한다.
  const segments = cmd.split(/&&|\|\||;|\||\n/);
  for (const seg of segments) {
    const m = seg.match(/\bgit\b.*\bpush\b(.*)/);
    if (!m) continue;
    // push 뒤 refspec 목적지(dst) 검사 — 'main' / 'HEAD:main' / 'x:prod' 전부 차단.
    // 플래그 토큰(-u, --force-with-lease, --delete 등)은 건너뛴다.
    const tokens = m[1].split(/\s+/).filter((t) => t && !t.startsWith("-"));
    for (const t of tokens) {
      const dst = t.includes(":") ? t.split(":").pop() : t;
      if (dst === "main" || dst === "prod") {
        console.error(
          `브랜치 전략 위반 차단: '${dst}' 직접 push 금지. ` +
            "main=동결(읽기 전용)·prod=dev→prod 승격 PR 전용 — 작업은 dev 타깃 PR 로 (CLAUDE.md §협업 워크플로)."
        );
        process.exit(2);
      }
    }
  }
  process.exit(0);
});
