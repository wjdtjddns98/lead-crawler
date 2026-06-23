"""테스트 격리 — dry_run 강제 + 실제 네트워크 차단.

Nutti 컨벤션을 따른다: 테스트는 네트워크 없이 통과해야 하며, 실수로 외부(유료)
호출이 일어나면 조용히 과금되는 대신 명시적으로 실패한다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from leadcrawler.config import get_settings


class NetworkAccessBlockedError(RuntimeError):
    """테스트 중 실제 네트워크 전송이 시도됐을 때 발생(격리 위반 신호)."""


@pytest.fixture(autouse=True)
def _isolate_database(
    request: pytest.FixtureRequest, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """기본 DATABASE_URL 을 테스트마다 격리된 빈 SQLite 로 강제한다.

    이전엔 conftest 가 dry_run·네트워크만 막고 DATABASE_URL 은 막지 않아서, 기본값
    (로컬 PG ``localhost:5432``)을 그대로 쓰는 테스트가 개발자의 실 DB 를 오염시켰다
    (예: 스케줄러 일일잡이 ``review_queue`` 에 더미 리드를 적재). 네트워크 차단과 같은
    "실수로 외부 자원을 건드리면 막는다" 철학을 DB 에도 적용한다.

    실 PostgreSQL 통합테스트(``test_pg_integration``)만 의도적으로 실 DB URL 을 쓰므로
    그 모듈에 한해 격리를 건너뛴다. 전역 ``LEADCRAWLER_PG_TEST`` 플래그가 아니라 **테스트
    노드 단위로 게이트**해, PG 통합테스트를 켠 채 풀 스위트를 돌려도 나머지 테스트는 계속
    SQLite 로 격리된다(전역 플래그로 격리를 끄면 같은 오염 버그가 조용히 재발).
    """
    from leadcrawler.storage.db import dispose_engines

    if request.module.__name__.endswith("test_pg_integration"):
        yield
        return
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    get_settings.cache_clear()  # setenv 와 캐시 무효화를 한 곳에 묶어 순서 의존 제거
    dispose_engines()  # 이전 테스트가 캐시한 엔진 제거(URL 재사용·핸들 누수 방지)
    yield
    dispose_engines()  # tmp SQLite 파일 핸들 해제(Windows 파일 잠금 회피)


@pytest.fixture(autouse=True)
def _force_dry_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """모든 테스트에서 dry_run 을 강제하고 설정 캐시를 비운다."""
    monkeypatch.setenv("LEADCRAWLER_DRY_RUN", "true")
    monkeypatch.delenv("LEADCRAWLER_NOTION_TOKEN", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx·SMTP·DNS 의 실제 전송을 차단한다(테스트는 주입/monkeypatch 로 우회)."""

    def _blocked(*args: object, **kwargs: object) -> None:
        raise NetworkAccessBlockedError("테스트 중 실제 네트워크 호출이 차단되었습니다")

    import smtplib

    import httpx

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked, raising=False)
    # SMTP/DNS 는 httpx 가 아니므로 별도 차단(verify SMTP·MX 경로 안전망).
    monkeypatch.setattr(smtplib, "SMTP", _blocked, raising=False)
    try:
        import dns.resolver

        monkeypatch.setattr(dns.resolver, "resolve", _blocked, raising=False)
    except ImportError:
        pass
