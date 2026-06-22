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


def test_cross_segment_domain_dedup(monkeypatch) -> None:
    # 같은 도메인을 서로 다른 세그먼트가 다른 key(reg:/dom:)로 잡아도 런 전체에서 1회만 추출.
    from leadcrawler.sources.base import DiscoveredCompany

    def _fake_discover(segment, settings):  # noqa: ARG001
        if segment.industry == "건설":
            return [DiscoveredCompany(
                canonical_key="reg:dart:001", name="삼성", domain="samsung.com",
                registry="dart", registry_id="001", source="dart",
            )]
        return [DiscoveredCompany(
            canonical_key="dom:samsung.com", name="삼성전자",
            domain="https://www.samsung.com", source="search",
        )]

    import leadcrawler.pipeline.run as run_mod

    monkeypatch.setattr(run_mod, "discover_segment", _fake_discover)
    leads = run_pipeline([
        Segment(country="KR", industry="건설"),
        Segment(country="KR", industry="제조"),
    ])
    # 두 번째 세그먼트의 dom: 후보는 도메인 동치로 스킵 → 1건만.
    assert len(leads) == 1
    assert leads[0].company.canonical_key == "reg:dart:001"
