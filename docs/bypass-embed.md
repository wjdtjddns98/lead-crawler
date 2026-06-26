# WAF 우회 임베드 (Track A) — 한계와 운영

> 벤더링된 insane-search 엔진(MIT, fivetaku)으로 정적 HTTP 가 WAF 로 막힌 거래소 소스
> (SET 태국·Bursa 말레이시아)의 상장목록을 가져오는 임베드. `docs/PLAN.md` Track A 구현.

## 구성
- **벤더**: `leadcrawler/sources/_bypass/engine/` — insane-search 0.4.1 버전 핀 복사본(무수정).
  MIT 고지 `_bypass/LICENSE`. 우리 코드는 어댑터로만 호출(벤더 무결성, 린트 제외).
- **어댑터**: `leadcrawler/sources/insane_fetcher.py` — `InsaneFetcher(SupportsFetch)`. 엔진
  `fetch(url)→FetchResult{ok,content}` 를 감싸 ok 면 HTML, 실패/미설치/오류는 graceful 빈 결과.
- **배선**: `enable_bypass=true` 시 `ExchangeSource._client()` 가 WAF 차단 소스(SET/Bursa)에
  `InsaneFetcher` 를 주입. off(기본)면 기존 no-op 그대로.
- **의존**: extra `[bypass]`(curl_cffi·beautifulsoup4·pyyaml). 미설치면 어댑터가 graceful no-op.

## 활성화
```bash
pip install -e ".[bypass]"           # curl_cffi 격자 등 설치
export LEADCRAWLER_ENABLE_BYPASS=true # SET/Bursa 우회 시도
```

## 한계 (중요)
1. **playwright_mcp 티어 미동작**: 엔진의 최강 티어는 Claude 세션 MCP(`playwright_mcp`)에
   의존한다 → **임베드(우리 프로세스)에서는 동작하지 않는다**. 어댑터는 `enable_playwright=False`
   (curl_cffi 격자만) 기본이고, 엔진은 미가용 티어를 graceful 하게 건너뛴다. 로컬 Playwright
   가 설치돼 있으면 폴백은 정상.
2. **SET/Bursa 셀렉터·엔드포인트는 베스트에포트**: `list_url` 과 `_LISTING_ROW` 정규식은
   라이브 마크업 확인 전 best-effort 다(SgxSource 와 동일 철학). 실제 구조가 다르면 파서가
   못 잡아 **빈 결과**가 되며(graceful), 회사 손실·오류는 없다. 라이브 1회 검증 후 셀렉터를
   실제 구조로 보정해야 실수집이 된다.
3. **curl_cffi 격자**: WAF 프로파일(`_bypass/engine/waf_profiles.yaml`)이 대상 사이트의 보호
   방식과 맞아야 통과한다. 안 맞으면 ok=False → 빈 결과.

## ToS / 합법성
WAF 우회는 대상 사이트 이용약관 판단이 **사용자 몫**이다. 본 용도는 **상장사 공개 데이터**의
정당한 수집이라 리스크가 낮다고 보나, 운영 전 각 거래소 약관을 확인하라.

## 미수행 (사용자 액션 필요)
- **A1 제작자 동의·attribution 메시지**: MIT 라 벤더링 자체는 가능하나, 예의상 제작자에게
  임베드 사실을 알리는 GitHub Issue(github.com/fivetaku/gptaku_plugins)는 **외부 발신**이라
  자동 수행하지 않았다 — 사용자가 직접 올리길 권장.

## 재개/검증 (PoC)
```bash
# 엔진 단독 PoC(우회 동작 확인)
PYTHONPATH=leadcrawler/sources/_bypass \
  .venv/Scripts/python -m engine <SET_URL> --json --trace
```
