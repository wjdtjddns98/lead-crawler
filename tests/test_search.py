"""검색 발견 소스 — 결과 제목→회사명 정제(_clean_name) 테스트."""

from __future__ import annotations

import pytest

from leadcrawler.sources.search import _clean_name


@pytest.mark.parametrize(
    "title,domain,expected",
    [
        # IR/네비 문구 조각을 버리고 실제 회사명 조각을 남긴다.
        ("Investor Relations :: Meritage Homes", "meritagehomes.com", "Meritage Homes"),
        ("MasTec: Investor Relations", "mastec.com", "MasTec"),
        ("Investor Relations | Komatsu", "komatsu.jp", "Komatsu"),
        ("Home | Quanta Services, Inc.", "quantaservices.com", "Quanta Services, Inc."),
        ("Investor Relations - CNH Industrial", "cnh.com", "CNH Industrial"),
        # 구분자 없는 깔끔한 제목은 그대로.
        ("Builders FirstSource, Inc.", "bldr.com", "Builders FirstSource, Inc."),
    ],
)
def test_clean_name_extracts_company(title: str, domain: str, expected: str) -> None:
    assert _clean_name(title, domain) == expected


def test_clean_name_all_noise_falls_back_to_domain_root() -> None:
    # 제목이 통째로 IR/네비 문구면 도메인 root 를 제목화한다(쓸 만한 조각 없음).
    assert _clean_name("Investor Relations", "meritagehomes.com") == "Meritagehomes"
    assert _clean_name("Home | Investor Relations", "acme.co") == "Acme"


def test_clean_name_empty_title_uses_domain() -> None:
    assert _clean_name("", "acme.com") == "Acme"
