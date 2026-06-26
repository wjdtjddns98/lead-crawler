"""벤더링된 insane-search 엔진(WAF 프로파일 기반 fetch 체인) — A2.

출처: github.com/fivetaku/gptaku_plugins · insane-search 0.4.1 · MIT License
(저작권 (c) 2026 fivetaku, 동봉 ``LICENSE`` 참조). **버전 핀 복사본**이며 우리 코드는
:mod:`leadcrawler.sources.insane_fetcher` 어댑터를 통해서만 이 엔진을 호출한다.

런타임 의존(선택적 extra ``[bypass]``): curl_cffi(브라우저 임퍼소네이션 격자) + beautifulsoup4
+ pyyaml. 미설치 시 ``fetch`` 호출은 graceful 하게 ok=False 를 반환한다(어댑터가 빈 결과로 흡수).

한계(A5): 엔진의 ``playwright_mcp`` 티어는 Claude 세션 MCP 에 의존 → **임베드 환경에서 미동작**
(graceful 폴백). curl_cffi 격자 + 로컬 Playwright 폴백은 정상 동작한다.
"""

from __future__ import annotations

# 핀된 업스트림 버전(추적·업데이트 기준). 엔진 코드는 수정하지 않는다(벤더 무결성).
VENDORED_VERSION = "0.4.1"

__all__ = ["VENDORED_VERSION"]
