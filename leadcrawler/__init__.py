"""lead-crawler: 전 산업·전 기업 IR 연락처 추출 크롤러 + 검증 웹앱.

핵심 계약: ``LEADCRAWLER_DRY_RUN=true``(기본)면 외부 키 없이 전 파이프라인이
결정적 시뮬레이션으로 동작한다. 모든 외부 연동은 dry_run 분기에서 네트워크 없이
더미를 반환한다 — Nutti 프로젝트의 dry_run 우선 컨벤션을 그대로 따른다.
"""

__version__ = "0.1.0"
