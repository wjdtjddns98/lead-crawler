"""lead-crawler: 전 산업·전 기업 IR 연락처 추출 크롤러 + 검증 웹앱.

핵심 계약: ``LEADCRAWLER_DRY_RUN=true``(기본)면 외부 키 없이 전 파이프라인이
결정적 시뮬레이션으로 동작한다. 모든 외부 연동은 dry_run 분기에서 네트워크 없이
더미를 반환한다 — Nutti 프로젝트의 dry_run 우선 컨벤션을 그대로 따른다.
"""

# 버전 단일 출처 = pyproject.toml(작업규칙 §8) — 하드코딩 드리프트 방지(0.1.0 박제 사고).
# 이 앱은 항상 저장소 체크아웃에서 구동된다(git pull 운영·editable 설치·CI 동일 구조).
try:
    import tomllib as _tomllib
    from pathlib import Path as _Path

    __version__: str = _tomllib.loads(
        (_Path(__file__).resolve().parent.parent / "pyproject.toml").read_text("utf-8")
    )["project"]["version"]
except Exception:  # 저장소 밖(휠 설치 등) — 현 운영엔 없는 경로지만 import 는 죽이지 않는다.
    __version__ = "0.0.0+unknown"
