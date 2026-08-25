"""도메인 보유 미승격 회수 백필 스크립트(얇은 래퍼) — CLI 배선·커서파일 동작 검증.

승격 로직 자체(대상 SQL·도메인가드·listed 매핑·실패격리)는 ``leadcrawler/pipeline/
promote.py`` 로 이관됐다(세그먼트 작업 큐 설계 §4·§6 PR1, tests/test_promote.py 가 검증).
이 파일은 스크립트가 그 API 를 올바른 인자로 호출하고 CLI 옵션·커서파일 타이밍을 기존과
동일하게 배선하는지만 검증한다.

scripts/ 는 패키지가 아니라 importlib 로 파일에서 직접 로드한다.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_promote_domained.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_promote_domained", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_split_multi_flattens_commas() -> None:
    """반복 지정 + 쉼표 병기 평탄화 — CLI --exclude-industry 관례와 동일(promote.py 재사용)."""
    mod = _load()
    assert mod._split_multi(["정보보안,게임", "은행"]) == ["정보보안", "게임", "은행"]
    assert mod._split_multi(None) is None
    assert mod._split_multi([" , "]) is None


def test_listed_of_maps_cli_flags_to_tri_state() -> None:
    mod = _load()
    assert mod._listed_of(False, False) == "unknown"
    assert mod._listed_of(True, False) == "unlisted"
    assert mod._listed_of(False, True) == "listed"


def test_cursor_file_only_advances_after_batch_persisted(tmp_path, monkeypatch) -> None:
    """--cursor-file 은 promote_batch 반환(=배치 persist 완료) 후에만 기록된다(리뷰 HIGH 회귀 가드).

    중간에 죽으면(promote_batch 예외) 커서 파일이 없어야 재기동이 같은 배치를 다시 훑는다 —
    선기록이면 미처리 행이 커서 뒤로 영구 스킵된다.
    """
    mod = _load()
    cursor = tmp_path / "cursor.txt"

    monkeypatch.setattr(mod, "Settings", lambda: type(
        "S", (), {"dry_run": False}
    )())
    monkeypatch.setattr(
        mod, "get_sessionmaker", lambda settings: (lambda: contextlib.nullcontext(None))
    )
    monkeypatch.setattr(mod, "count_promote_targets", lambda *a, **k: 2)
    monkeypatch.setattr(mod, "_load_domain_guards", lambda session: (set(), set()))
    closed = {"n": 0}
    fake_run = SimpleNamespace(close=lambda: closed.__setitem__("n", closed["n"] + 1))
    monkeypatch.setattr(mod, "PromoteRun", SimpleNamespace(open=lambda settings: fake_run))

    import sys as _sys
    monkeypatch.setattr(_sys, "argv", [
        "backfill_promote_domained.py", "--workers", "1", "--batch", "10",
        "--cursor-file", str(cursor),
    ])

    # ① 배치 중간 사망 시뮬레이션 — promote_batch 가 터지면 커서 파일이 없어야 한다.
    def boom(settings, sm, **kw):
        raise RuntimeError("mid-batch death")

    monkeypatch.setattr(mod, "promote_batch", boom)
    with pytest.raises(RuntimeError):
        mod.main()
    assert not cursor.exists()
    assert closed["n"] == 1  # 예외 경로에서도 런 컴포넌트는 닫힌다.

    # ② 정상 완주 — 1배치 처리 후 대상 0 건으로 종료, 커서 파일에 마지막 키가 남는다.
    calls = {"n": 0}

    def fake_batch(settings, sm, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return 2, "dom:kr:b2.co.kr", 2, 0, 0
        return 0, kw["after"], 0, 0, 0

    monkeypatch.setattr(mod, "promote_batch", fake_batch)
    assert mod.main() == 0
    assert cursor.read_text(encoding="utf-8") == "dom:kr:b2.co.kr"
    assert closed["n"] == 2


def test_main_exits_early_on_dry_run(monkeypatch) -> None:
    """DRY_RUN=true 면 promote_batch 를 전혀 호출하지 않고 즉시 중단한다(기존 동작 유지)."""
    mod = _load()
    monkeypatch.setattr(mod, "Settings", lambda: type("S", (), {"dry_run": True})())

    def _unexpected(*a, **k):
        raise AssertionError("dry_run 인데 호출됨")

    monkeypatch.setattr(mod, "get_sessionmaker", _unexpected)
    import sys as _sys
    monkeypatch.setattr(_sys, "argv", ["backfill_promote_domained.py"])
    assert mod.main() == 1
