"""web/dist 정적 서빙 — 빌드 있으면 루트에 SPA, 없으면 404, API 라우트는 항상 우선."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from leadcrawler.config import get_settings  # noqa: E402


@pytest.fixture
def _env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LEADCRAWLER_DATABASE_URL", f"sqlite:///{tmp_path}/spa.db")
    get_settings.cache_clear()


def test_serves_spa_when_dist_exists(_env, tmp_path, monkeypatch) -> None:
    from leadcrawler.api import app as app_module

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>SPA-OK</html>", encoding="utf-8")
    monkeypatch.setattr(app_module, "_WEB_DIST", dist)

    client = TestClient(app_module.create_app())
    r = client.get("/")
    assert r.status_code == 200
    assert "SPA-OK" in r.text
    # API 라우트가 마운트에 가려지지 않는다.
    assert client.get("/health").status_code == 200


def test_root_404_without_dist(_env, tmp_path, monkeypatch) -> None:
    from leadcrawler.api import app as app_module

    monkeypatch.setattr(app_module, "_WEB_DIST", tmp_path / "no-dist")

    client = TestClient(app_module.create_app())
    assert client.get("/").status_code == 404
    assert client.get("/health").status_code == 200
