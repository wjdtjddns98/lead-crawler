"""dry_run 파이프라인 + 중복제거 테스트."""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.models import ValidationStatus
from leadcrawler.pipeline import run_pipeline
from leadcrawler.sources.base import DiscoveredCompany, Segment


def test_dry_run_pipeline_produces_leads() -> None:
    leads = run_pipeline([Segment(country="KR", industry="건설")])
    assert leads
    lead = leads[0]
    assert lead.company.is_active is True
    assert lead.email is not None and lead.email.value.startswith("ir@")
    assert lead.form is not None
    assert lead.email_validation.status is ValidationStatus.VALID


def test_target_saved_stops_early(monkeypatch) -> None:
    """target_saved 도달 시 남은 세그먼트를 돌지 않고 조기 종료('정해진 양만큼 뽑고 멈춤')."""
    import leadcrawler.pipeline.run as run_mod

    def _fake_discover(segment, settings, cost_ledger=None, *, sources=None, seen_domains=None):  # noqa: ARG001
        # 세그먼트마다 활성 더미 5개(도메인 유일 → dedup 스킵 안 됨).
        return [
            DiscoveredCompany(
                canonical_key=f"dom:{segment.industry}{i}.com",
                name=f"C{i}", domain=f"{segment.industry}{i}.com",
            )
            for i in range(5)
        ]

    monkeypatch.setattr(run_mod, "discover_segment", _fake_discover)
    segs = [Segment(country="KR", industry=f"ind{j}") for j in range(10)]  # 최대 50 가능
    leads = run_pipeline(
        segs, settings=Settings(dry_run=True, enrich_workers=1), target_saved=7
    )
    saved = [ld for ld in leads if ld.company.is_active]
    assert len(saved) >= 7  # 목표 도달
    assert len(saved) < 50  # 전부 안 돌고 조기 종료(과수확 제한)


def test_target_saved_overshoots_by_batch_with_workers(monkeypatch) -> None:
    """병렬(workers>1)이면 조기종료는 flush 경계 평가라 batch_size(=workers*4)만큼 오버슈트한다.

    target_saved=1 이라도 첫 배치(8건)가 통째로 처리·적재된 뒤 카운터를 보므로 saved 가
    1 이 아니라 8 까지 점프한다(상한 아닌 하한 보장). docstring 의 오버슈트 계약을 고정.
    """
    import leadcrawler.pipeline.run as run_mod

    def _fake_discover(segment, settings, cost_ledger=None, *, sources=None, seen_domains=None):  # noqa: ARG001
        # 단일 세그먼트에서 20건 발견 → 배치(8) 경계에서 flush.
        return [
            DiscoveredCompany(
                canonical_key=f"dom:{segment.industry}{i}.com",
                name=f"C{i}", domain=f"{segment.industry}{i}.com",
            )
            for i in range(20)
        ]

    monkeypatch.setattr(run_mod, "discover_segment", _fake_discover)
    segs = [Segment(country="KR", industry="ind0")]
    leads = run_pipeline(
        segs, settings=Settings(dry_run=True, enrich_workers=2), target_saved=1
    )
    saved = [ld for ld in leads if ld.company.is_active]
    assert len(saved) >= 1  # 하한 보장(목표 도달)
    assert len(saved) == 8  # batch_size=workers*4 만큼 오버슈트 — 정확히 1 에서 안 멈춤.


def test_no_target_processes_all_segments(monkeypatch) -> None:
    """target_saved=None(기본)이면 주어진 세그먼트를 전부 소진(continuous, 회귀 0)."""
    import leadcrawler.pipeline.run as run_mod

    def _fake_discover(segment, settings, cost_ledger=None, *, sources=None, seen_domains=None):  # noqa: ARG001
        return [
            DiscoveredCompany(
                canonical_key=f"dom:{segment.industry}{i}.com",
                name=f"C{i}", domain=f"{segment.industry}{i}.com",
            )
            for i in range(5)
        ]

    monkeypatch.setattr(run_mod, "discover_segment", _fake_discover)
    segs = [Segment(country="KR", industry=f"ind{j}") for j in range(4)]  # 4×5=20
    leads = run_pipeline(segs, settings=Settings(dry_run=True, enrich_workers=1))
    assert len([ld for ld in leads if ld.company.is_active]) == 20  # 전부 처리.


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

    def _fake_discover(segment, settings, cost_ledger=None, *, sources=None, seen_domains=None):  # noqa: ARG001
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


def test_on_progress_reports_counters() -> None:
    # on_progress 가 단계마다 카운터 dict 를 통지하고, 최종값이 결과와 일치한다.
    events: list[dict[str, int]] = []
    leads = run_pipeline(
        [Segment(country="KR", industry="건설")],
        on_progress=events.append,
    )
    assert events  # 최소 1회(초기 + 기업별 + 세그먼트 완료) 통지.
    first = events[0]
    assert first["segments_total"] == 1  # 초기 통지에 총 세그먼트 수.
    last = events[-1]
    assert last["segments_done"] == 1  # 세그먼트 완료까지 진행.
    assert last["discovered"] == len(leads)
    assert last["enriched"] == len(leads)
    assert last["saved"] == sum(1 for ld in leads if ld.company.is_active)


def test_should_cancel_stops_before_processing() -> None:
    # should_cancel 이 처음부터 True → 어떤 기업도 처리하지 않고 빈 결과로 중단.
    leads = run_pipeline(
        [Segment(country="KR", industry="건설")],
        should_cancel=lambda: True,
    )
    assert leads == []


def test_should_cancel_midway_preserves_processed(monkeypatch) -> None:
    # 첫 기업 처리 후 취소 신호 → 처리된 분은 보존, 이후 기업은 중단.
    from leadcrawler.sources.base import DiscoveredCompany

    def _fake_discover(segment, settings, cost_ledger=None, *, sources=None, seen_domains=None):  # noqa: ARG001
        return [
            DiscoveredCompany(
                canonical_key="dom:a.com", name="A", domain="a.com", source="search"
            ),
            DiscoveredCompany(
                canonical_key="dom:b.com", name="B", domain="b.com", source="search"
            ),
        ]

    import leadcrawler.pipeline.run as run_mod

    monkeypatch.setattr(run_mod, "discover_segment", _fake_discover)
    calls = {"n": 0}

    def _cancel() -> bool:
        # 호출 순서: ①세그먼트 시작 ②A 처리 직전 ③B 처리 직전. ③에서만 취소.
        calls["n"] += 1
        return calls["n"] > 2

    # 명시 설정으로 밀폐 — 취소콜 순서(①②③)는 순차(workers=1) 경로 전제라, 로컬 .env 의
    # LEADCRAWLER_DISCOVERY_WORKERS 값에 흔들리지 않게 고정한다.
    leads = run_pipeline(
        [Segment(country="KR", industry="건설")],
        settings=Settings(dry_run=True, discovery_workers=1),
        should_cancel=_cancel,
    )
    assert len(leads) == 1
    assert leads[0].company.canonical_key == "dom:a.com"


# --- 이메일 상한(MAX_EMAILS) 통합 경로 -------------------------------------


class _RecordingValidator:
    """호출 순서·deep 플래그를 기록하는 가짜 검증기(네트워크 없음)."""

    def __init__(self, statuses: dict[str, ValidationStatus], settings: Settings) -> None:
        self._statuses = statuses
        self.settings = settings
        self.calls: list[tuple[str, bool]] = []

    def validate(self, email: str, domain: str | None = None, *, deep: bool = True):  # noqa: ARG002
        from leadcrawler.models import EmailValidation

        self.calls.append((email, deep))
        return EmailValidation(status=self._statuses[email])


class _StubEnricher:
    """고정 후보를 돌려주는 가짜 enricher(_build_lead 계약 최소 구현)."""

    last_home_html = ""
    last_home_rendered_html = ""

    def __init__(self, contacts: list) -> None:
        self._contacts = contacts

    def enrich(self, dc):  # noqa: ARG002
        return self._contacts


class _StubExistence:
    def verify(self, *a, **kw):  # noqa: ARG002
        from leadcrawler.verify.existence import ExistenceResult

        return ExistenceResult(is_active=True, confidence=1.0, site_alive=True)


class _StubClassifier:
    def classify(self, *a, **kw):  # noqa: ARG002
        raise AssertionError("홈페이지 텍스트가 없으면 분류기를 부르지 않아야 한다")


def _mk_contacts(values: list[str]) -> list:
    from leadcrawler.models import Contact, ContactType

    return [Contact(type=ContactType.EMAIL, value=v, confidence=0.5) for v in values]


def _run_build_lead(statuses: dict[str, ValidationStatus], *, deep_all: bool):
    """_build_lead 를 가짜 컴포넌트로 1회 실행하고 (lead, validator) 반환."""
    from leadcrawler.pipeline.run import _build_lead

    settings = Settings(dry_run=False, validate_all_candidates=deep_all)
    validator = _RecordingValidator(statuses, settings)
    dc = DiscoveredCompany(
        canonical_key="k1", name="X Ltd", country="KR", industry="건설", domain="x.com"
    )
    lead = _build_lead(
        dc,
        enricher=_StubEnricher(_mk_contacts(list(statuses))),
        existence=_StubExistence(),
        email_validator=validator,
        classifier=_StubClassifier(),
    )
    return lead, validator


def test_build_lead_caps_candidates_and_syncs_validations() -> None:
    """후보가 상한(3)까지 줄고, email_validations 키가 저장 대상과 정확히 일치한다."""
    statuses = {
        "ir@x.com": ValidationStatus.VALID,
        "info@x.com": ValidationStatus.VALID,
        "sales@x.com": ValidationStatus.VALID,
        "office@x.com": ValidationStatus.RISKY,
        "admin@x.com": ValidationStatus.INVALID,
    }
    lead, _ = _run_build_lead(statuses, deep_all=True)

    assert len(lead.email_candidates) == 3
    assert lead.email is not None and lead.email.value == "ir@x.com"  # IR 정상 최우선
    # 저장 대상(email_candidates)과 검증결과(email_validations)가 1:1 — stale 검증행 방지.
    assert set(lead.email_validations) == {c.value for c in lead.email_candidates}
    # INVALID 는 어디에도 남지 않는다.
    assert "admin@x.com" not in lead.email_validations


def test_build_lead_deep_all_skips_extra_revalidation() -> None:
    """라이브 기본값(validate_all_candidates=True)에선 재검증 분기를 타지 않는다."""
    statuses = {"info@x.com": ValidationStatus.VALID, "ir@x.com": ValidationStatus.VALID}
    _, validator = _run_build_lead(statuses, deep_all=True)

    assert all(deep for _, deep in validator.calls)  # 전건 deep
    assert len(validator.calls) == len(statuses)  # 후보 수만큼, 추가 호출 없음


def test_build_lead_revalidates_only_when_selection_changed() -> None:
    """얕은 검증 모드에서 선택이 바뀌면 그 1건만 추가 심층검증한다(곱셈 아님)."""
    # accepted_emails 는 IR 을 앞세우지만, 등급이 상위 축이라 최종 선택은 info(정상)로 바뀐다.
    statuses = {"ir@x.com": ValidationStatus.RISKY, "info@x.com": ValidationStatus.VALID}
    lead, validator = _run_build_lead(statuses, deep_all=False)

    assert lead.email is not None and lead.email.value == "info@x.com"
    deep_calls = [v for v, deep in validator.calls if deep]
    assert deep_calls == ["ir@x.com", "info@x.com"]  # 잠정선택 1 + 최종선택 1
