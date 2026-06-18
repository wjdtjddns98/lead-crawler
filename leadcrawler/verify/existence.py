"""실존성 검증 — 제약 ②(현 시점 실존 기업만).

신호: 등록처 active/최근 공시(실 경로) + 도메인 DNS + 홈페이지 HTTP 200.
dry_run 에서는 네트워크 없이 도메인 유무로 결정적 판정한다.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..config import Settings, get_settings


class ExistenceResult(BaseModel):
    """실존성 판정 결과."""

    is_active: bool
    site_alive: bool
    confidence: float


class ExistenceVerifier:
    """도메인/등록처 신호로 기업 실존 여부를 판정한다."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def verify(self, domain: str | None) -> ExistenceResult:
        """도메인 생존 + 등록처 신호로 실존성을 산정한다."""
        if self.settings.dry_run:
            alive = bool(domain)
            return ExistenceResult(
                is_active=alive, site_alive=alive, confidence=0.9 if alive else 0.0
            )
        # 실 경로: DNS + 홈페이지 200 + 등록처 active (후속 구현).
        alive = self._site_alive(domain) if domain else False
        return ExistenceResult(
            is_active=alive, site_alive=alive, confidence=0.7 if alive else 0.0
        )

    def _site_alive(self, domain: str) -> bool:
        import httpx

        for scheme in ("https", "http"):
            try:
                resp = httpx.head(f"{scheme}://{domain}", timeout=10.0, follow_redirects=True)
                if resp.status_code < 400:
                    return True
            except Exception:
                continue
        return False
