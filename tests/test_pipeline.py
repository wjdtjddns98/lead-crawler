"""dry_run 파이프라인 + 중복제거 테스트."""

from __future__ import annotations

from leadcrawler.models import ValidationStatus
from leadcrawler.pipeline import run_pipeline
from leadcrawler.sources.base import Segment


def test_dry_run_pipeline_produces_leads() -> None:
    leads = run_pipeline([Segment(country="KR", industry="건설")])
    assert leads
    lead = leads[0]
    assert lead.company.is_active is True
    assert lead.email is not None and lead.email.value.startswith("ir@")
    assert lead.form is not None
    assert lead.email_validation.status is ValidationStatus.VALID


def test_seen_keys_dedup() -> None:
    seg = Segment(country="KR", industry="건설")
    seen: set[str] = set()
    first = run_pipeline([seg], seen=seen)
    assert first
    # 같은 세그먼트를 같은 seen 으로 재실행하면 전부 스킵.
    second = run_pipeline([seg], seen=seen)
    assert second == []
