"""검증 웹앱 FastAPI 앱 (스켈레톤).

직원용 검증 워크벤치의 백엔드. M1 에서 검증 큐 조회/확정/export 라우터를 채운다.
``fastapi`` 미설치 시 import 되지 않으므로, 기본 테스트는 이 모듈을 건너뛴다.
"""

from __future__ import annotations

from fastapi import FastAPI

from .. import __version__


def create_app() -> FastAPI:
    """FastAPI 앱 인스턴스를 생성한다."""
    app = FastAPI(title="lead-crawler 검증 웹앱", version=__version__)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
