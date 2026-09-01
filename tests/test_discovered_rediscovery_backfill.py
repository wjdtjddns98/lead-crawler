"""save_discovered — 같은 등록처 키 재발견 시 비어 있던 보조정보(domain·name_eng·phone·address)를
null 전용으로 채우고, 원장이 일문이면 영문 표시명으로 교체한다(Codex 리뷰 HIGH, #449 후속)."""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.sources.base import DiscoveredCompany
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.repository import save_discovered


def _dc(**kw: object) -> DiscoveredCompany:
    base = dict(
        canonical_key="reg:fsa_jp:6010001008845", name="株式会社みずほ銀行", country="JP",
        industry="은행", listed="unknown", registry="fsa_jp", registry_id="6010001008845",
        source="fsa_jp", segment="JP/은행/unknown",
    )
    base.update(kw)
    return DiscoveredCompany(**base)


def test_rediscovery_backfills_empty_domain_and_english_name(tmp_path) -> None:
    settings = Settings(dry_run=True, database_url=f"sqlite:///{tmp_path / 't.db'}")
    init_db(settings)
    with session_scope(settings) as s:
        save_discovered(s, _dc())  # 1차: 토큰 부재 → 도메인·영문 없음.
    with session_scope(settings) as s:
        row = save_discovered(s, _dc(
            name="Mizuho Bank, Ltd.", name_eng="株式会社みずほ銀行",
            domain="mizuhobank.co.jp", phone="03-3214-1111", address="東京都千代田区",
        ))
        assert row.domain == "mizuhobank.co.jp" and row.phone == "03-3214-1111"
        assert row.name == "Mizuho Bank, Ltd." and row.name_eng == "株式会社みずほ銀行"
    with session_scope(settings) as s:
        # 기존 값은 절대 덮지 않는다(제약 ① — 재크롤 흔들림 방지).
        row = save_discovered(s, _dc(name="Other Name", domain="other.example", phone="02-0"))
        assert row.domain == "mizuhobank.co.jp" and row.phone == "03-3214-1111"
        assert row.name == "Mizuho Bank, Ltd."
