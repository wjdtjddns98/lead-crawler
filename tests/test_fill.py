"""이메일 채우기 consumer(pipeline.fill) — 빈 대상 조기반환 + 카운트(네트워크 없음)."""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.pipeline.fill import count_targets, fill_batch


class _Result:
    def all(self):
        return []

    def scalar(self):
        return 0


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return _Result()


def _sm():
    return _Session()


def test_fill_batch_empty_targets_noop() -> None:
    # 대상 0건이면 컴포넌트도 안 만들고 (0,0) 조기반환 — 네트워크·enrich 없음.
    assert fill_batch(Settings(dry_run=False), _sm, limit=50, workers=4) == (0, 0)


def test_count_targets_reads_scalar() -> None:
    assert count_targets(_sm) == 0
