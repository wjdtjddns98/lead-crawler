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

    # 이메일 보강/검증
    hunter_api_key: str = Field(default="")
    apollo_api_key: str = Field(default="")
    zerobounce_api_key: str = Field(default="")
    neverbounce_api_key: str = Field(default="")

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
    # 무키 집계원(GLEIF/Wikidata) 공통 UA — WMF 정책상 식별 가능한 UA 필요.
    discovery_user_agent: str = Field(default="LeadCrawler/1.0 (+lead-crawler; research use)")

    # 보강(enrich) 제어
    enrich_max_pages: int = Field(default=6)  # 기업당 정적 크롤 페이지 상한(홈+후보)

    # 운영비 한도(월, 원)
    monthly_budget_krw: int = Field(default=500_000)


@lru_cache
def get_settings() -> Settings:
    """프로세스 단위로 캐시된 설정 인스턴스를 반환한다."""
    return Settings()
