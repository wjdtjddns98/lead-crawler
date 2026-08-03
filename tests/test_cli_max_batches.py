"""CLI --loop --max-batches 계약 검증 — N배치 후 정상종료(러너 재기동용), 0=무제한.

두 커맨드(fill-emails·backfill-resolve-domains)에 동일 로직이 복제돼 있어 각각 검증한다.
배치 함수는 가짜로 주입 — DB·네트워크 없이 카운터 로직만 확인.
"""

from __future__ import annotations

from types import SimpleNamespace

import leadcrawler.cli as cli
import leadcrawler.pipeline.fill as fill


def _stub_common(monkeypatch, settings) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    import leadcrawler.storage.db as db

    monkeypatch.setattr(db, "get_sessionmaker", lambda s: object())


def test_fill_emails_max_batches_도달시_종료(monkeypatch):
    _stub_common(monkeypatch, SimpleNamespace(dry_run=False))
    calls = {"n": 0}
    monkeypatch.setattr(fill, "count_targets", lambda sm, countries=None: 100)

    def fake_fill_batch(settings, sm, *, limit, workers, countries=None):
        calls["n"] += 1
        return 1, 0

    monkeypatch.setattr(fill, "fill_batch", fake_fill_batch)
    # 반환하면 성공 — max_batches 가 없으면 이 호출은 무한루프다.
    cli.fill_emails(loop=True, batch=5, workers=1, interval=0.0, min_queue=0, max_batches=3)
    assert calls["n"] == 3


def test_resolve_max_batches_도달시_종료(monkeypatch):
    _stub_common(monkeypatch, SimpleNamespace(dry_run=False, resolve_domains=True))
    calls = {"n": 0}
    monkeypatch.setattr(fill, "count_resolve_targets", lambda sm, countries=None: 100)

    def fake_resolve_batch(settings, sm, *, limit, workers, countries=None):
        calls["n"] += 1
        return 1, 1, 0

    monkeypatch.setattr(fill, "resolve_batch", fake_resolve_batch)
    cli.backfill_resolve_domains(
        loop=True, batch=5, workers=1, interval=0.0, min_queue=0, max_batches=2
    )
    assert calls["n"] == 2
