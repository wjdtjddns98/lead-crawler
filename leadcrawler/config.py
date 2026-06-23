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

    # 발견 소스
    edgar_user_agent: str = Field(default="")
    dart_api_key: str = Field(default="")
    companies_house_api_key: str = Field(default="")
    opencorporates_api_key: str = Field(default="")

    # 검색엔진 발견
    google_cse_key: str = Field(default="")
    google_cse_cx: str = Field(default="")
    bing_api_key: str = Field(default="")

    # 도메인 해석(opt-in) — 발견 소스가 도메인을 못 준 기업(GLEIF 등)을 회사명+국가로
    # 검색해 공식 도메인을 채운다. Google CSE 키 필요(무료 100/일), dry_run no-op.
    # 정밀도 우선(회사명↔도메인 root 일치할 때만 채택). quota 보호용 런당 캡.
    resolve_domains: bool = Field(default=False)
    domain_resolve_max: int = Field(default=50)  # 런당 CSE 해석 호출 상한(quota 보호)

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

    # AI (Claude Vision)
    anthropic_api_key: str = Field(default="")

    # Notion 자동 리포팅 — 토큰 없으면 no-op(로그만).
    notion_token: str = Field(default="")
    notion_version: str = Field(default="2022-06-28")
    notion_daily_db: str = Field(default="4709a56a55614147a264e68dc7e521b8")
    notion_scrum_db: str = Field(default="850215969daa4b648a8713055356053a")
    notion_status_db: str = Field(default="dd74e2f7c25f425cbf030117031c9f92")

    # 라이브 발견 제어(예산·레이트리밋)
    discovery_max_per_source: int = Field(default=50)  # 소스·세그먼트당 후보 상한
    http_request_delay: float = Field(default=0.12)  # 요청 간 최소 간격(초)
    http_timeout: float = Field(default=15.0)
    # 무키 집계원(GLEIF/Wikidata) 공통 UA. Wikidata WDQS 는 WMF 로봇 정책상 연락처
    # (URL/이메일) 없는 UA 를 403 거부 — 식별 가능한 연락처 URL 필수(2026-06-19 실연동 확인).
    discovery_user_agent: str = Field(
        default="LeadCrawler/1.0 (+https://github.com/wjdtjddns98/lead-crawler)"
    )

    # 보강(enrich) 제어
    enrich_max_pages: int = Field(default=6)  # 기업당 정적 크롤 페이지 상한(홈+후보)
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
