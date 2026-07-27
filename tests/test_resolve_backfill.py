"""도메인 백필 소비자(pipeline.fill.resolve_batch) — 정체된 무도메인 발견행 되짚기.

SQLite 인프로세스 + 모든 네트워크 컴포넌트(DomainResolver/Enricher/ExistenceVerifier/
EmailValidator/classifier)를 결정적 더블로 교체(monkeypatch) — 네트워크 0.
"""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.enrich.industry_classify import IndustryVerdict
from leadcrawler.models import Contact, ContactType, EmailValidation, ValidationStatus
from leadcrawler.pipeline import fill as fill_mod
from leadcrawler.schema import CompanyRow, DiscoveredCompanyRow
from leadcrawler.sources.base import DiscoveredCompany
from leadcrawler.storage.db import get_sessionmaker, init_db
from leadcrawler.verify.existence import ExistenceResult


class _Result:
    def all(self):
        return []

    def scalar(self):
        return 0


class _EmptySession:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return _Result()


def _empty_sm():
    return _EmptySession()


def test_resolve_batch_empty_targets_noop() -> None:
    assert fill_mod.resolve_batch(Settings(dry_run=False), _empty_sm, limit=50, workers=4) == (
        0,
        0,
        0,
    )


def test_count_resolve_targets_reads_scalar() -> None:
    assert fill_mod.count_resolve_targets(_empty_sm) == 0


class _FakeResolver:
    """이름에 '살아있는'이 포함되면 도메인 해석 성공, 아니면 실패(결정적, 네트워크 0)."""

    def __init__(self, settings, *, cost_ledger=None, **_kw):
        pass

    def resolve(self, dc: DiscoveredCompany) -> str | None:
        if "무연고" in dc.name:
            return None
        slug = dc.canonical_key.split(":")[-1]
        return f"{slug}.example.com"


class _FakeEnricher:
    def __init__(self, settings, *, cost_ledger=None, **_kw):
        self.last_home_html = None
        self.last_home_rendered_html = None

    def enrich(self, dc: DiscoveredCompany) -> list[Contact]:
        return [Contact(type=ContactType.EMAIL, value=f"ir@{dc.domain}")]

    def close(self) -> None:
        pass


class _FakeExistence:
    def __init__(self, settings, *, registry_checker=None, **_kw):
        pass

    def verify(self, domain, *, registry=None, registry_id=None, home_html=None, rendered_html=None):
        return ExistenceResult(is_active=True, site_alive=True, confidence=0.9)

    def close(self) -> None:
        pass


class _FakeValidator:
    def __init__(self, settings, *, cost_ledger=None, **_kw):
        self.settings = settings

    def validate(self, email, company_domain=None, *, deep=True):
        return EmailValidation(status=ValidationStatus.VALID, mx=True)

    def close(self) -> None:
        pass


class _StubClassifier:
    model = "stub"

    def classify(self, name, domain, text) -> IndustryVerdict:
        return IndustryVerdict(label=None, model="stub", billed=False)


def _patch(monkeypatch):
    monkeypatch.setattr(fill_mod, "DomainResolver", _FakeResolver)
    monkeypatch.setattr(fill_mod, "Enricher", _FakeEnricher)
    monkeypatch.setattr(fill_mod, "ExistenceVerifier", _FakeExistence)
    monkeypatch.setattr(fill_mod, "EmailValidator", _FakeValidator)
    monkeypatch.setattr(fill_mod, "build_classifier", lambda settings, **kw: _StubClassifier())
    monkeypatch.setattr(fill_mod, "build_registry_checker", lambda settings, **kw: None)


def test_resolve_batch_fills_domain_and_promotes(tmp_path, monkeypatch) -> None:
    """도메인없음+미승격 행 → 해석성공+실존 → 도메인 기록 + company 승격."""
    _patch(monkeypatch)
    s = Settings(database_url=f"sqlite:///{tmp_path}/rb.db", dry_run=False, resolve_domains=True)
    init_db(s)
    sm = get_sessionmaker(s)
    with sm() as session:
        session.add(
            DiscoveredCompanyRow(
                canonical_key="name:kr:살아있는상사", name="살아있는상사", country="KR",
                industry="화학·석유화학", source="nps",
            )
        )
        session.commit()

    processed, resolved, promoted = fill_mod.resolve_batch(s, sm, limit=50, workers=2)
    assert (processed, resolved, promoted) == (1, 1, 1)

    with sm() as session:
        row = session.get(DiscoveredCompanyRow, "name:kr:살아있는상사")
        assert row.domain == "살아있는상사.example.com"
        co = session.query(CompanyRow).filter_by(canonical_key=row.canonical_key).one()
        assert co.is_active and co.homepage == "https://살아있는상사.example.com"


def test_resolve_batch_domain_recorded_even_when_no_match(tmp_path, monkeypatch) -> None:
    """해석 실패(무연고) 행은 처리는 되지만 도메인 미기록·승격 없음(다음 배치 재시도 대상 유지)."""
    _patch(monkeypatch)
    s = Settings(database_url=f"sqlite:///{tmp_path}/rb2.db", dry_run=False, resolve_domains=True)
    init_db(s)
    sm = get_sessionmaker(s)
    with sm() as session:
        session.add(
            DiscoveredCompanyRow(
                canonical_key="name:kr:무연고상사", name="무연고상사", country="KR",
                industry="화학·석유화학", source="nps",
            )
        )
        session.commit()

    processed, resolved, promoted = fill_mod.resolve_batch(s, sm, limit=50, workers=2)
    assert (processed, resolved, promoted) == (1, 0, 0)
    with sm() as session:
        row = session.get(DiscoveredCompanyRow, "name:kr:무연고상사")
        assert not row.domain  # 여전히 비어있음 → count_resolve_targets 대상 유지.
    assert fill_mod.count_resolve_targets(sm) == 1


def test_resolve_batch_country_scope_excludes_unselected(tmp_path, monkeypatch) -> None:
    """국가 스코프 — ``countries`` 지정 시 KR('대한민국' 자유표기 포함)을 되짚지 않는다.

    2026-07-27 사고(당시 크롤 병행 companion): 원장 정렬(last_crawled_at desc)상 직전
    KR 크롤 물량이 맨 앞이라, KR 제외 크롤에서도 KR 이 승격돼 큐에 섞였다. companion 은
    이후 제거됐지만 국가 스코프는 향후 CLI 옵션 노출용으로 유지.
    """
    _patch(monkeypatch)
    s = Settings(database_url=f"sqlite:///{tmp_path}/rb4.db", dry_run=False, resolve_domains=True)
    init_db(s)
    sm = get_sessionmaker(s)
    with sm() as session:
        session.add(
            DiscoveredCompanyRow(
                canonical_key="name:kr:살아있는상사", name="살아있는상사", country="KR",
                industry="화학·석유화학", source="nps",
            )
        )
        session.add(  # 원장 자유표기('대한민국')도 KR 별칭으로 접혀 제외돼야 한다.
            DiscoveredCompanyRow(
                canonical_key="name:kr:살아있는무역", name="살아있는무역", country="대한민국",
                industry="화학·석유화학", source="import",
            )
        )
        session.add(
            DiscoveredCompanyRow(
                canonical_key="name:us:살아있는inc", name="살아있는inc", country="US",
                industry="화학·석유화학", source="gleif",
            )
        )
        session.commit()

    assert fill_mod.count_resolve_targets(sm, ["US", "JP"]) == 1  # KR 2건 제외.
    assert fill_mod.count_resolve_targets(sm) == 3  # 무스코프=전세계(현행).

    processed, resolved, promoted = fill_mod.resolve_batch(
        s, sm, limit=50, workers=2, countries=["US", "JP"]
    )
    assert (processed, resolved, promoted) == (1, 1, 1)  # US 만 처리·승격.
    with sm() as session:
        assert session.get(DiscoveredCompanyRow, "name:kr:살아있는상사").domain is None
        assert session.get(DiscoveredCompanyRow, "name:kr:살아있는무역").domain is None
        [co] = session.query(CompanyRow).all()
        assert co.canonical_key == "name:us:살아있는inc"  # KR 승격 0.


def test_resolve_batch_skips_promotion_on_domain_collision(tmp_path, monkeypatch) -> None:
    """이미 원장에 있는 도메인과 겹치면 도메인은 기록하되 company 승격은 스킵(제약① 중복방지)."""
    _patch(monkeypatch)
    s = Settings(database_url=f"sqlite:///{tmp_path}/rb3.db", dry_run=False, resolve_domains=True)
    init_db(s)
    sm = get_sessionmaker(s)
    with sm() as session:
        # 기존 원장에 이미 같은 도메인을 가진 행(다른 canonical_key, 이미 승격됨 가정).
        session.add(
            DiscoveredCompanyRow(
                canonical_key="reg:dart:00000001", name="원조상사", country="KR",
                industry="화학·석유화학", source="dart",
                domain="살아있는상사.example.com",
            )
        )
        # 백필 대상 — 해석하면 같은 도메인이 나오도록 canonical_key 슬러그를 맞춘다.
        session.add(
            DiscoveredCompanyRow(
                canonical_key="name:kr:살아있는상사", name="살아있는상사", country="KR",
                industry="화학·석유화학", source="nps",
            )
        )
        session.commit()

    processed, resolved, promoted = fill_mod.resolve_batch(s, sm, limit=50, workers=2)
    assert processed == 1 and resolved == 1 and promoted == 0  # 도메인은 채워지되 승격 0.
    with sm() as session:
        row = session.get(DiscoveredCompanyRow, "name:kr:살아있는상사")
        assert row.domain == "살아있는상사.example.com"
        assert session.query(CompanyRow).count() == 0  # 중복 company 생성 안 됨.


class _CrashingEnricher(_FakeEnricher):
    """enrich 단계에서 항상 예외 — 일시적 크래시(TLS·헤드리스 등) 재현."""

    def enrich(self, dc: DiscoveredCompany) -> list:  # noqa: ANN001
        raise RuntimeError("simulated enrich crash")


def test_resolve_batch_exception_leaves_domain_unrecorded_for_retry(tmp_path, monkeypatch) -> None:
    """enrich 예외 시 도메인을 기록하지 않는다 — 다음 배치가 재시도 가능해야 한다(리뷰 HIGH-MED)."""
    _patch(monkeypatch)
    monkeypatch.setattr(fill_mod, "Enricher", _CrashingEnricher)
    s = Settings(database_url=f"sqlite:///{tmp_path}/rb4.db", dry_run=False, resolve_domains=True)
    init_db(s)
    sm = get_sessionmaker(s)
    with sm() as session:
        session.add(
            DiscoveredCompanyRow(
                canonical_key="name:kr:살아있는상사", name="살아있는상사", country="KR",
                industry="화학·석유화학", source="nps",
            )
        )
        session.commit()

    processed, resolved, promoted = fill_mod.resolve_batch(s, sm, limit=50, workers=2)
    assert (processed, resolved, promoted) == (1, 0, 0)
    with sm() as session:
        row = session.get(DiscoveredCompanyRow, "name:kr:살아있는상사")
        assert not row.domain  # 해석은 성공했었지만 기록되지 않음 — 재시도 가능.
    assert fill_mod.count_resolve_targets(sm) == 1


def test_resolve_batch_intra_batch_domain_collision_promotes_once(tmp_path, monkeypatch) -> None:
    """같은 배치 안의 두 서로 다른 신규 행이 같은 도메인으로 해석되면 1건만 승격한다."""
    _patch(monkeypatch)

    class _SameDomainResolver(_FakeResolver):
        def resolve(self, dc: DiscoveredCompany) -> str | None:
            return "shared.example.com"  # 두 행 모두 같은 도메인으로 해석.

    monkeypatch.setattr(fill_mod, "DomainResolver", _SameDomainResolver)
    s = Settings(database_url=f"sqlite:///{tmp_path}/rb5.db", dry_run=False, resolve_domains=True)
    init_db(s)
    sm = get_sessionmaker(s)
    with sm() as session:
        session.add(
            DiscoveredCompanyRow(
                canonical_key="name:kr:회사A", name="회사A", country="KR",
                industry="화학·석유화학", source="nps",
            )
        )
        session.add(
            DiscoveredCompanyRow(
                canonical_key="name:kr:회사B", name="회사B", country="KR",
                industry="화학·석유화학", source="nps",
            )
        )
        session.commit()

    processed, resolved, promoted = fill_mod.resolve_batch(s, sm, limit=50, workers=2)
    assert (processed, resolved, promoted) == (2, 2, 1)  # 둘 다 도메인 기록, 1건만 승격.
    with sm() as session:
        assert session.get(DiscoveredCompanyRow, "name:kr:회사A").domain == "shared.example.com"
        assert session.get(DiscoveredCompanyRow, "name:kr:회사B").domain == "shared.example.com"
        assert session.query(CompanyRow).count() == 1
