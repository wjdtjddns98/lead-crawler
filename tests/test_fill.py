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


def test_count_targets_country_scope(tmp_path) -> None:
    """국가 스코프 — 크롤이 국가 명시선택이면 채우기 대상도 그 국가만 센다('대한민국' 별칭 포함)."""
    from leadcrawler.schema import CompanyRow, DiscoveredCompanyRow
    from leadcrawler.storage.db import get_sessionmaker, init_db

    s = Settings(database_url=f"sqlite:///{tmp_path}/fc.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)
    with sm() as session:
        rows = [
            ("dom:kr:a.co.kr", "한국사", "대한민국", "a.co.kr"),
            ("dom:us:b.com", "USCo", "US", "b.com"),
        ]
        for key, name, country, domain in rows:
            session.add(DiscoveredCompanyRow(
                canonical_key=key, name=name, country=country, industry="화학·석유화학",
                source="import", domain=domain,
            ))
        session.commit()  # FK(company→discovered) 순서 보장 — 발견행 먼저 확정.
        for i, (key, name, country, _domain) in enumerate(rows):
            session.add(CompanyRow(
                id=f"co_{i}", canonical_key=key, name=name, country=country,
                industry="화학·석유화학", site_alive=True,
            ))
        session.commit()

    assert count_targets(sm) == 2  # 무스코프=전세계(현행).
    assert count_targets(sm, ["US"]) == 1  # KR('대한민국' 표기) 제외.
    assert count_targets(sm, ["KR"]) == 1  # 'KR' 선택이 '대한민국' 표기도 잡는다(별칭 확장).
