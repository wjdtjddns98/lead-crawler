"""테스트 격리 — dry_run 강제 + 실제 네트워크 차단.

Nutti 컨벤션을 따른다: 테스트는 네트워크 없이 통과해야 하며, 실수로 외부(유료)
호출이 일어나면 조용히 과금되는 대신 명시적으로 실패한다.
"""

from __future__ import annotations

import pytest

from leadcrawler.config import get_settings


class NetworkAccessBlockedError(RuntimeError):
    """테스트 중 실제 네트워크 전송이 시도됐을 때 발생(격리 위반 신호)."""


@pytest.fixture(autouse=True)
def _force_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """모든 테스트에서 dry_run 을 강제하고 설정 캐시를 비운다."""
    monkeypatch.setenv("LEADCRAWLER_DRY_RUN", "true")
    monkeypatch.delenv("LEADCRAWLER_NOTION_TOKEN", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx 의 실제 전송을 차단한다."""

    def _blocked(*args: object, **kwargs: object) -> None:
        raise NetworkAccessBlockedError("테스트 중 실제 네트워크 호출이 차단되었습니다")

    import httpx

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked, raising=False)
