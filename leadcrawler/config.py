"""애플리케이션 설정 — pydantic-settings 기반 .env 로드.

모든 외부 키는 ``LEADCRAWLER_`` 접두사 환경변수로 주입한다. 키가 없으면 해당
소스는 비활성(no-op)으로 동작하고, ``dry_run`` 이 켜져 있으면 네트워크 호출 자체를
하지 않는다.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수/.env 에서 로드되는 런타임 설정."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LEADCRAWLER_",
        extra="ignore",
        case_sensitive=False,
    )

    # 기본 동작
    dry_run: bool = Field(default=True)

    # 저장소
    database_url: str = Field(
        default="postgresql+psycopg://leadcrawler:leadcrawler@localhost:5432/leadcrawler"
    )

    # WAF 우회 임베드(Track A, opt-in) — SET/Bursa 등 정적 HTTP 가 WAF 로 막힌 거래소 소스가
    # 벤더링된 insane-search 엔진(InsaneFetcher)으로 목록 HTML 을 가져온다. [bypass] extra
    # (curl_cffi 등) 필요, 미설치/실패는 graceful 빈 결과. off(기본)면 기존 no-op 그대로.
    enable_bypass: bool = Field(default=False)

    # 발견 소스
    edgar_user_agent: str = Field(default="")
    dart_api_key: str = Field(default="")
    companies_house_api_key: str = Field(default="")
    opencorporates_api_key: str = Field(default="")

    # 검색엔진 발견
    google_cse_key: str = Field(default="")
    google_cse_cx: str = Field(default="")
    serper_api_key: str = Field(default="")  # Serper.dev SERP API(유료·CSE 신규차단 대체)
    bing_api_key: str = Field(default="")
    # 검색 공급자 선택: auto(serper 키>cse 키) | serper | cse | none.
    search_provider: str = Field(default="auto")

    # 도메인 해석(opt-in) — 발견 소스가 도메인을 못 준 기업(GLEIF 등)을 회사명+국가로
    # 검색해 공식 도메인을 채운다. Google CSE 키 필요(무료 100/일), dry_run no-op.
    # 정밀도 우선(회사명↔도메인 root 일치할 때만 채택). quota 보호용 런당 캡.
    resolve_domains: bool = Field(default=False)
    domain_resolve_max: int = Field(default=50)  # 런당 CSE 해석 호출 상한(quota 보호)

    # 검증 큐 동시 처리(당겨가기) — 6명 동시 검증 시 충돌 방지. 직원이 한 번에 점유하는
    # 배치 크기와, 점유 후 미처리로 방치된 항목이 풀로 복귀하는 TTL(분).
    review_claim_batch: int = Field(default=15, ge=1)
    review_claim_ttl_minutes: int = Field(default=30, ge=1)

    # 웹 직접 크롤 — 한 번에 도는 세그먼트(국가×업종×상장) 상한. 빈 국가=지원 전체국이라
    # 다업종 선택 시 세그먼트가 폭증할 수 있어, 우발적 대량 크롤(예산·시간 낭비)을 막는 캡.
    crawl_max_segments: int = Field(default=500, ge=1)

    # 이메일 보강/검증
    hunter_api_key: str = Field(default="")
    apollo_api_key: str = Field(default="")
    zerobounce_api_key: str = Field(default="")
    neverbounce_api_key: str = Field(default="")
    # 이메일 탐색 API escalation(opt-in·유료) — 정적·헤드리스·OCR 도 0건이면 Hunter/Apollo
    # 등 제3자 이메일 DB에 도메인을 질의. 키 있는 제공자만 활성, 호출당 과금/크레딧.
    enrich_email_api: bool = Field(default=False)
    email_api_max_results: int = Field(default=5)  # 제공자당 채택 후보 상한(과금 보호)
    # SMTP 메일박스 검증(opt-in) — 느리고 ISP 차단·greylisting 위험이 있어 기본 off.
    # 켜면 라이브에서 MX 호스트에 RCPT 프로브로 수신가능 여부를 확인한다(catch-all 인지).
    email_smtp_check: bool = Field(default=False)
    email_smtp_from: str = Field(default="verify@example.com")  # MAIL FROM 신원
    smtp_timeout: float = Field(default=10.0)  # SMTP 연결/응답 타임아웃(초)
    # 딜리버러빌리티 API 검증(opt-in·유료) — MX/SMTP 다음 단계로 ZeroBounce/NeverBounce 등
    # 제3자 DB에 이메일을 질의해 수신가능 여부를 권위있게 보정. 호출당 과금/크레딧이라 기본
    # off, 키 있는 제공자만 활성(ZeroBounce→NeverBounce 순). dry_run 미경유.
    email_deliverability_check: bool = Field(default=False)
    # 후보 전수 심층검증 — True(기본)면 모든 이메일 후보에 SMTP/딜리버러빌리티를 수행(선택 UI
    # 신호 풍부). False 면 선택 이메일(candidates[0])만 심층검증하고 나머지는 형식/MX 까지만 —
    # 후보 수만큼 곱해지던 SMTP 핸드셰이크·유료 호출을 줄인다(처리량·비용 절감, 산출 선택은 동일).
    validate_all_candidates: bool = Field(default=True)

    # AI (Claude Vision)
    anthropic_api_key: str = Field(default="")

    # 중복해소 LLM 판정(C2, opt-in·유료) — 무료·결정적 사다리가 못 가른 쇼트리스트만
    # Claude(Haiku)로 동일기업 여부 판정. 호출당 과금이라 기본 off + 런당 캡, anthropic_api_key
    # 없으면 미동작(StubJudge 폴백). dry_run 은 네트워크 없는 결정적 스텁으로 동작.
    dedup_llm_judge: bool = Field(default=False)
    dedup_llm_model: str = Field(default="claude-haiku-4-5-20251001")  # 저가 모델 기본(비용)
    dedup_llm_max_pairs: int = Field(default=200, ge=0)  # 런당 유료 판정 상한(과금 보호)
    # 수집 파이프라인 inline 중복 승격(C5, opt-in) — 정확 dedup 통과한 신규 리드를 기존
    # 원장과 near_dup 대조, 최상위(auto) 티어면 재추출 없이 흡수(제약①). off 면 기존 동작.
    dedup_inline: bool = Field(default=False)
    # 수집 시점 도메인 없는(name: 티어) 신규 기업을 기존 name: 티어와 렉시컬(이름) 대조해
    # 유사쌍을 dedup_candidate(워크벤치 pending)로 적재(opt-in). **자동 스킵/머지 안 함** —
    # 동명이인 리드손실 방지(제약②)라 사람 확정 위임. off 면 기존 동작(회귀 0).
    dedup_inline_lexical: bool = Field(default=False)

    # 아웃리치 이메일 발송(확정큐 전체발송) — Gmail SMTP 등. 외부행위라 기본 off(dry-run):
    # email_send_enabled=true 라야 실발송, 아니면 수신 미리보기·로그만 남기고 안 보낸다.
    smtp_send_host: str = Field(default="smtp.gmail.com")
    smtp_send_port: int = Field(default=587)
    smtp_send_user: str = Field(default="")  # 발신 계정(= From 주소). 예: you@gmail.com
    smtp_send_password: str = Field(default="")  # 앱 비밀번호(Gmail 2단계 인증)
    email_send_enabled: bool = Field(default=False)  # true 라야 실발송(안전 게이트)
    email_send_daily_cap: int = Field(default=400)  # 일일 발송 상한(계정 차단·스팸 방지)
    email_send_min_interval: float = Field(default=1.0)  # 발송 간 최소 간격(초, 레이트리밋)

    # Notion 자동 리포팅 — 토큰 없으면 no-op(로그만).
    notion_token: str = Field(default="")
    notion_version: str = Field(default="2022-06-28")
    notion_daily_db: str = Field(default="4709a56a55614147a264e68dc7e521b8")
    notion_scrum_db: str = Field(default="850215969daa4b648a8713055356053a")
    notion_status_db: str = Field(default="dd74e2f7c25f425cbf030117031c9f92")

    # 라이브 발견 제어(예산·레이트리밋)
    # 무료/등록처 소스(EDGAR·DART·CompaniesHouse·거래소·GLEIF·Wikidata·OpenCorporates)의
    # 소스·세그먼트당 후보 상한 — 후보당 무료 API 1콜이라 깊게 긁어도 무비용. 이 프로그램의
    # 핵심 가치는 "계속 데이터를 추출"하는 것이라 보수적 50 에서 크게 올린다(등록처 유니버스까지
    # 깊게). 과도한 호출은 target_count 조기종료 + cost_ledger 예산 가드 + 취소로 막는다.
    # 유료 검색(Serper)은 이 캡을 쓰지 않는다 → discovery_search_max_per_segment 로 분리.
    discovery_max_per_source: int = Field(default=500)
    # 유료 검색(SearchSource/Serper)의 세그먼트당 결과 상한 — 무료 캡과 독립(무료를 수천까지
    # 올려도 유료가 끌려가지 않게). Serper 는 page_size=100·1페이지/세그먼트라 ≤100 이면 1콜로
    # 끝난다 → 이 값을 100 밑으로 낮춰도 과금(1콜)은 그대로고 무료 결과만 버리므로 비용 절감엔
    # 무의미. 유료 실절감은 search_skip_if_free_ge·글로벌 seen 주입이 담당한다. 기본 100 은
    # 기존 동작(min(discovery_max_per_source, 100))을 보존한다.
    discovery_search_max_per_segment: int = Field(default=100, ge=1)
    # 유료 검색(Serper/CSE) 비용 가드 — 글로벌 seen(DB시드+런 누적)을 검색에 주입해 중복에
    # 돈을 쓰지 않게 한다. ① 한 페이지의 실후보 대비 신규 비율이 이 값 미만이면 다음 페이지를
    # 더 사지 않고 페이징 중단(CSE 다페이지 절감). 0.0 이면 항상 끝까지 페이징(기존 동작).
    # 주의: 신규 도메인이 뒤페이지에 몰린 쿼리는 과소수확 가능. 주공급자 Serper 는 1페이지/세그
    # (조기중단 무관)라 실질 무해, CSE(다페이지·폐기경로)에서만 영향 — 보수적 0.2 기본.
    search_min_new_ratio: float = Field(default=0.2)
    # ② 무료 등록처가 한 세그먼트에서 신규 N건 이상 발견하면 그 세그먼트의 유료 검색 호출을
    # 통째로 건너뛴다(Serper 1콜/세그먼트 바닥까지 절감). 0=비활성(유료검색 항상 실행).
    # 주의: 올리면 비용↓이지만 등록처에 안 잡히는 장기꼬리(비상장 SME) 발견이 줄 수 있다.
    search_skip_if_free_ge: int = Field(default=0)
    http_request_delay: float = Field(default=0.12)  # 요청 간 최소 간격(초)
    http_timeout: float = Field(default=15.0)
    # 무키 집계원(GLEIF/Wikidata) 공통 UA. Wikidata WDQS 는 WMF 로봇 정책상 연락처
    # (URL/이메일) 없는 UA 를 403 거부 — 식별 가능한 연락처 URL 필수(2026-06-19 실연동 확인).
    discovery_user_agent: str = Field(
        default="LeadCrawler/1.0 (+https://github.com/wjdtjddns98/lead-crawler)"
    )

    # 실존성 헤드리스 확인(Track B, opt-in) — HTTP 200 이어도 파킹/JS-blank 사이트를 거른다.
    # 켜면 site_alive 후보를 헤드리스 렌더로 재확인(SupportsRender 주입 또는 Playwright 지연
    # 빌드, 미설치면 graceful 통과). 느려서 기본 off. dry_run 은 미경유(결정적).
    verify_headless: bool = Field(default=False)

    # 보강(enrich) 제어
    enrich_max_pages: int = Field(default=6)  # 기업당 정적 크롤 페이지 상한(홈+후보)
    # 기업 단위 병렬 추출 동시성(enrich+verify+validate 는 I/O 바운드). 1=순차(기존 동작).
    # 서로 다른 기업=다른 호스트라 동시 처리해도 호스트당 과부하 없음(워커별 독립 페처).
    enrich_workers: int = Field(default=4, ge=1, le=16)
    # 헤드리스 escalation(opt-in) — 정적으로 이메일 못 찾은 기업만 JS 렌더로 재시도.
    # Playwright(선택적 extra) 미설치면 자동 폴백(정적 결과 유지). 느려서 기본 off.
    enrich_headless: bool = Field(default=False)
    headless_timeout: float = Field(default=20.0)  # 페이지 렌더 타임아웃(초)
    headless_max_pages: int = Field(default=3)  # 헤드리스 렌더 페이지 상한(정적과 분리·예산)
    # OCR escalation(opt-in) — 정적·헤드리스로도 이메일 못 찾으면 이미지 OCR(무료·로컬).
    # Tesseract(선택적 extra ocr) 미설치면 자동 폴백. 기본 off.
    enrich_ocr: bool = Field(default=False)
    ocr_max_images: int = Field(default=5)  # 기업당 OCR 이미지 상한(비용 보호)
    # Vision escalation(opt-in·유료) — OCR 도 실패하면 Claude Vision(anthropic_api_key 필요).
    # 호출당 과금이라 기본 off + 엄격한 이미지 캡. anthropic 키 없으면 미동작.
    enrich_vision: bool = Field(default=False)
    vision_model: str = Field(default="claude-haiku-4-5-20251001")  # 저가 모델 기본(비용)
    # 기업당 최대 Vision 호출(과금) 횟수 — 이메일 0건 이미지도 호출을 소모한다.
    vision_max_images: int = Field(default=2)

    # 운영비 한도(월, 원)
    monthly_budget_krw: int = Field(default=500_000)
    # 예산 안전장치 — 월 누계가 monthly_budget_krw 이상이면 추가 유료 호출
    # (EmailAPI·Vision·딜리버러빌리티)을 차단한다. cost_ledger 가 활성(라이브)일 때만 작동.
    cost_budget_enforce: bool = Field(default=True)
    # 호출당 단가 보정(원) — DEFAULT_PRICING_KRW 를 provider 별로 덮어쓴다. 실청구 대사 후
    # 운영자가 실단가로 보정(예: LEADCRAWLER_COST_PRICING_KRW='{"hunter": 80}'). 미지정=기본 추정치.
    cost_pricing_krw: dict[str, int] = Field(default_factory=dict)

    # 검증 웹앱 로그인 세션 유효시간(시간). 만료 시 재로그인 필요.
    web_session_ttl_hours: int = Field(default=12)

    # 24/7 스케줄러(opt-in) — 매일 크롤 1회전 + Notion 자동 리포팅(일일보고·스크럼·현황).
    # APScheduler(선택적 extra ``schedule``) 미설치면 ``serve`` 가 안내 후 종료. 기본 off.
    scheduler_enabled: bool = Field(default=False)
    report_hour: int = Field(default=0)  # 일일 리포트 실행 시각(UTC 시)
    report_minute: int = Field(default=0)  # 일일 리포트 실행 시각(UTC 분)
    report_industries: str = Field(default="건설")  # 일일 잡 기본 업종(쉼표구분)
    report_countries: str = Field(default="KR")  # 일일 잡 기본 국가(쉼표구분, 빈값=전체국)
    report_milestone: str = Field(default="M3")  # 일일 보고 마일스톤 라벨
    report_persist: bool = Field(default=False)  # 일일 잡 결과 DB 영속화 여부


@lru_cache
def get_settings() -> Settings:
    """프로세스 단위로 캐시된 설정 인스턴스를 반환한다."""
    return Settings()
