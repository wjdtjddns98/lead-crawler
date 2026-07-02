"""`leadcrawler web` SSL 패스스루 — cert/key 쌍 검증 + uvicorn.run 전달."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from leadcrawler.cli import app

uvicorn = pytest.importorskip("uvicorn")

runner = CliRunner()


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: captured.update(kw))
    return captured


def test_web_default_no_ssl(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_run(monkeypatch)
    result = runner.invoke(app, ["web"])
    assert result.exit_code == 0
    assert captured["ssl_certfile"] is None
    assert captured["ssl_keyfile"] is None


def test_web_ssl_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_run(monkeypatch)
    result = runner.invoke(
        app,
        ["web", "--ssl-certfile", "certs/cert.pem", "--ssl-keyfile", "certs/key.pem"],
    )
    assert result.exit_code == 0
    assert captured["ssl_certfile"] == "certs/cert.pem"
    assert captured["ssl_keyfile"] == "certs/key.pem"


def test_web_ssl_requires_both(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_run(monkeypatch)
    result = runner.invoke(app, ["web", "--ssl-certfile", "certs/cert.pem"])
    assert result.exit_code != 0
    assert not captured  # uvicorn.run 미호출
