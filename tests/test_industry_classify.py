"""구분(업종) 실질화 — 택소노미·코드역매핑·구분결정·AI분류기·파이프라인 배선 테스트.

네트워크 없이 통과한다: 실제 anthropic 호출은 sys.modules 에 주입한 페이크로 대체한다.
"""

from __future__ import annotations

import sys
import types

import pytest

from leadcrawler.enrich import industry_classify as IC
from leadcrawler.enrich.industry_classify import (
    ClaudeClassifier,
    StubClassifier,
    _accept_label,
    _text_from_html,
    build_classifier,
)
from leadcrawler.models import Contact, ContactType, EmailValidation, ValidationStatus
from leadcrawler.sources import industry as I
from leadcrawler.sources.base import Segment, build_company
from leadcrawler.sources.taxonomy import (
    AMBIGUOUS_LABELS,
    INDUSTRY_TAXONOMY,
    UNCLASSIFIED,
    is_taxonomy_label,
)
from leadcrawler.verify.existence import ExistenceResult


# ── 택소노미 ────────────────────────────────────────────────────────────────
def test_taxonomy_closed_set_no_dupes():
    assert len(INDUSTRY_TAXONOMY) == len(set(INDUSTRY_TAXONOMY))
    assert len(INDUSTRY_TAXONOMY) >= 35  # "훨씬 많은" 대분류
    assert UNCLASSIFIED not in INDUSTRY_TAXONOMY  # 미분류는 별도(닫힌집합 밖)
    assert is_taxonomy_label(INDUSTRY_TAXONOMY[0])
    assert is_taxonomy_label(UNCLASSIFIED)
    assert not is_taxonomy_label("존재하지않는업종")
    assert UNCLASSIFIED in AMBIGUOUS_LABELS  # 미분류는 무조건 LLM 대상


# ── 코드 역매핑(명확 단일매치만) ────────────────────────────────────────────
def test_reverse_ksic_single_match():
    assert I.industry_from_ksic("212") == "제약·바이오"  # 21 → 제약·바이오
    assert I.industry_from_ksic("4100") == "건설·엔지니어링"
    assert I.industry_from_ksic("30110") == "자동차·모빌리티"


def test_reverse_ambiguous_and_unmapped_return_none():
    # 금융(64/65/66)은 은행/증권/보험으로 갈려 단일 확정 불가 → None(모호) → LLM.
    assert I.industry_from_ksic("64110") is None
    # 완전 미매핑 코드도 None.
    assert I.industry_from_ksic("99999") is None
    assert I.industry_from_ksic(None) is None
    assert I.industry_from_ksic("") is None


def test_reverse_sic_and_uk():
    assert I.industry_from_sic("3674") == "반도체·디스플레이"
    assert I.industry_from_sic("2834") == "제약·바이오"
    # 정밀 에너지 코드는 유지(석유정제 29·석탄 13).
    assert I.industry_from_sic("2911") == "에너지·전력"
    # 저정밀 접두는 역매핑에서 제외 → None → LLM: SIC 49(위생 포함)·8731(일반연구).
    assert I.industry_from_sic("4953") is None  # 폐기물수거(구 오라벨 '에너지·전력')
    assert I.industry_from_sic("8731") is None  # 일반 물리·생물연구(구 오라벨 '제약·바이오')
    # 모든 역매핑 라벨은 반드시 닫힌 택소노미 소속.
    for tbl in (I._KSIC_TAXO, I._SIC_TAXO, I._UK_SIC_TAXO):
        assert all(is_taxonomy_label(lbl) for lbl in tbl)


# ── 구분 결정 규칙 ──────────────────────────────────────────────────────────
def test_is_broad_industry():
    for b in ("", "전체", "ALL", "기타", "미분류", None):
        assert I.is_broad_industry(b)
    for s in ("제약", "반도체·디스플레이", "게임"):
        assert not I.is_broad_industry(s)


def test_resolve_industry_label():
    # broad + 코드복원 → 코드 라벨
    assert I.resolve_industry_label("전체", code_label="제약·바이오") == "제약·바이오"
    # broad + 코드없음 → 미분류(파이프라인이 이후 LLM)
    assert I.resolve_industry_label("전체") == UNCLASSIFIED
    # 비-broad(구체·자유텍스트) → 원문 보존(오라벨 방지)
    assert I.resolve_industry_label("제약") == "제약"
    assert I.resolve_industry_label("우주항공") == "우주항공"


def test_build_company_applies_resolution():
    # broad 검색 + 코드 라벨 전달 → 구분 복원.
    dc = build_company(
        source="dart", segment=Segment(country="KR", industry="전체"),
        name="A", registry="dart", registry_id="1", industry_code_label="제약·바이오",
    )
    assert dc.industry == "제약·바이오"
    # broad + 코드없음 → 미분류.
    dc2 = build_company(source="s", segment=Segment(country="KR", industry="전체"), name="B")
    assert dc2.industry == UNCLASSIFIED
    # 구체 검색 → 그대로.
    dc3 = build_company(source="s", segment=Segment(country="KR", industry="제약"), name="C")
    assert dc3.industry == "제약"


# ── 분류기: 라벨 검증 · 스텁 · HTML 전처리 ─────────────────────────────────
def test_accept_label_closed_set_only():
    assert _accept_label("제약·바이오") == "제약·바이오"
    assert _accept_label('"게임"') == "게임"  # 따옴표 제거
    assert _accept_label("아마도 게임 회사") == "게임"  # 포함 매칭
    assert _accept_label("미분류") is None  # 미분류=abstain
    assert _accept_label("우주항공") is None  # 닫힌집합 밖 → None
    assert _accept_label("") is None


def test_stub_classifier_keyword_and_abstain():
    assert StubClassifier().classify("Samsung Pharma", "x.com", None).label == "제약·바이오"
    assert StubClassifier().classify("한화 게임즈", None, None).label == "게임"
    v = StubClassifier().classify("Zzz Holdings", "z.com", None)
    assert v.label is None and v.billed is False


def test_text_from_html_strips_and_truncates():
    assert _text_from_html("<script>evil()</script><p>Hello  World</p>") == "Hello World"
    assert _text_from_html(None) == ""
    long = "<p>" + "가" * 5000 + "</p>"
    assert len(_text_from_html(long)) == IC._TEXT_LIMIT


# ── 분류기: 실호출 경로(네트워크 없이 페이크 anthropic 주입) ────────────────
class _FakeMsg:
    def __init__(self, text):
        self.content = [types.SimpleNamespace(type="text", text=text)]


class _FakeMessages:
    def __init__(self, text, calls):
        self._text = text
        self._calls = calls

    def create(self, **kwargs):
        self._calls.append(kwargs)
        return _FakeMsg(self._text)


class _FakeClient:
    last_kwargs = None

    def __init__(self, **kwargs):
        _FakeClient.last_kwargs = kwargs
        self.messages = _FakeMessages(_FakeClient.reply, _FakeClient.create_calls)


def _install_fake_anthropic(monkeypatch, reply="게임"):
    _FakeClient.reply = reply
    _FakeClient.create_calls = []
    _FakeClient.last_kwargs = None
    fake = types.ModuleType("anthropic")
    fake.Anthropic = _FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return _FakeClient


def test_claude_classifier_uses_auth_token_bearer(monkeypatch):
    fc = _install_fake_anthropic(monkeypatch, reply="게임")
    c = ClaudeClassifier(model="m", auth_token="oat-123")
    v = c.classify("X Games", "x.com", "<p>we make games</p>")
    assert v.label == "게임" and v.billed is True
    # api_key 가 아니라 auth_token(Bearer)으로 클라이언트를 만들었는지.
    assert fc.last_kwargs.get("auth_token") == "oat-123"
    assert "api_key" not in fc.last_kwargs


def test_claude_classifier_uses_api_key(monkeypatch):
    fc = _install_fake_anthropic(monkeypatch, reply="반도체·디스플레이")
    c = ClaudeClassifier(model="m", api_key="sk-x")
    v = c.classify("Chip Co", "c.com", None)
    assert v.label == "반도체·디스플레이"
    assert fc.last_kwargs.get("api_key") == "sk-x"
    assert "auth_token" not in fc.last_kwargs


def test_claude_classifier_out_of_set_reply_abstains(monkeypatch):
    _install_fake_anthropic(monkeypatch, reply="우주항공(닫힌집합밖)")
    c = ClaudeClassifier(model="m", api_key="sk-x")
    v = c.classify("N", None, None)
    assert v.label is None and v.billed is True  # 이미 과금됨(왕복 성공)


class _Ledger:
    def __init__(self, over=False):
        self._over = over
        self.records = []

    def is_over_budget(self, *_a, **_k):
        return self._over

    def record(self, provider, units=1):
        self.records.append(provider)
        return None


def test_budget_over_skips_call(monkeypatch):
    fc = _install_fake_anthropic(monkeypatch, reply="게임")
    c = ClaudeClassifier(model="m", api_key="sk-x", ledger=_Ledger(over=True))
    v = c.classify("X", None, None)
    assert v.label is None and v.billed is False
    assert fc.create_calls == []  # 예산초과 → 호출 자체 안 함


def test_max_calls_cap(monkeypatch):
    _install_fake_anthropic(monkeypatch, reply="게임")
    ledger = _Ledger(over=False)
    c = ClaudeClassifier(model="m", api_key="sk-x", ledger=ledger, max_calls=2)
    assert c.classify("a", None, None).billed is True
    assert c.classify("b", None, None).billed is True
    v3 = c.classify("c", None, None)  # 캡 초과
    assert v3.billed is False and v3.label is None
    assert ledger.records == ["industry_llm", "industry_llm"]  # 2건만 과금


def test_build_classifier_gating():
    from leadcrawler.config import Settings

    def _s(**kw):
        # _env_file=None: 로컬 .env(라이브 토큰·플래그) 오염을 차단해 hermetic 하게 검증.
        base = dict(
            dry_run=False, industry_llm_classify=False,
            anthropic_api_key="", anthropic_auth_token="",
        )
        base.update(kw)
        return Settings(_env_file=None, **base)

    # dry_run → 스텁(무네트워크).
    assert isinstance(build_classifier(_s(dry_run=True, industry_llm_classify=True,
                                          anthropic_auth_token="t")), StubClassifier)
    # 플래그 off → 스텁.
    assert isinstance(build_classifier(_s(anthropic_api_key="k")), StubClassifier)
    # 플래그 on + 인증 없음 → 스텁.
    assert isinstance(build_classifier(_s(industry_llm_classify=True)), StubClassifier)
    # 플래그 on + auth_token → Claude(구독 auth).
    assert isinstance(
        build_classifier(_s(industry_llm_classify=True, anthropic_auth_token="t")),
        ClaudeClassifier,
    )
    # 플래그 on + api_key(auth 없음) → Claude(종량 API).
    assert isinstance(
        build_classifier(_s(industry_llm_classify=True, anthropic_api_key="k")),
        ClaudeClassifier,
    )


# ── 파이프라인 배선(_build_lead 게이트) ─────────────────────────────────────
class _FakeEnricher:
    def __init__(self):
        self.last_home_html = "<p>homepage</p>"
        self.last_home_rendered_html = None
        self.settings = types.SimpleNamespace(validate_all_candidates=False)

    def enrich(self, dc):
        return [Contact(type=ContactType.EMAIL, value="ir@x.com")]


class _FakeExistence:
    def __init__(self, active=True):
        self._active = active

    def verify(self, domain, **_k):
        return ExistenceResult(is_active=self._active, site_alive=self._active, confidence=0.9)


class _FakeValidator:
    settings = types.SimpleNamespace(validate_all_candidates=False)

    def validate(self, value, domain, deep=False):
        return EmailValidation(status=ValidationStatus.VALID)


class _RecordingClassifier:
    model = "fake"

    def __init__(self, label):
        self._label = label
        self.calls = 0

    def classify(self, name, domain, text):
        self.calls += 1
        return IC.IndustryVerdict(label=self._label, model=self.model, billed=False)


def _dc(industry):
    from leadcrawler.sources.base import DiscoveredCompany

    return DiscoveredCompany(
        canonical_key="dom:x.com", name="X Corp", country="KR",
        industry=industry, domain="x.com",
    )


def test_build_lead_classifies_when_unclassified():
    from leadcrawler.pipeline.run import _build_lead

    clf = _RecordingClassifier("게임")
    lead = _build_lead(
        _dc(UNCLASSIFIED), enricher=_FakeEnricher(), existence=_FakeExistence(),
        email_validator=_FakeValidator(), classifier=clf,
    )
    assert lead.company.industry == "게임"
    assert clf.calls == 1  # 미분류라 LLM 한번 거침


def test_build_lead_classifies_catch_all_label():
    from leadcrawler.pipeline.run import _build_lead

    clf = _RecordingClassifier("자동차·모빌리티")
    lead = _build_lead(
        _dc("기타 제조"), enricher=_FakeEnricher(), existence=_FakeExistence(),
        email_validator=_FakeValidator(), classifier=clf,
    )
    assert lead.company.industry == "자동차·모빌리티"
    assert clf.calls == 1  # catch-all(모호)도 무조건 LLM


def test_build_lead_skips_confident_label():
    from leadcrawler.pipeline.run import _build_lead

    clf = _RecordingClassifier("게임")
    lead = _build_lead(
        _dc("제약·바이오"), enricher=_FakeEnricher(), existence=_FakeExistence(),
        email_validator=_FakeValidator(), classifier=clf,
    )
    assert lead.company.industry == "제약·바이오"  # 확신 라벨 → 유지
    assert clf.calls == 0  # LLM 스킵(비용)


def test_build_lead_abstain_keeps_unclassified():
    from leadcrawler.pipeline.run import _build_lead

    clf = _RecordingClassifier(None)  # abstain
    lead = _build_lead(
        _dc(UNCLASSIFIED), enricher=_FakeEnricher(), existence=_FakeExistence(),
        email_validator=_FakeValidator(), classifier=clf,
    )
    assert lead.company.industry == UNCLASSIFIED  # abstain → 미분류 유지(리드 보존)
    assert clf.calls == 1


def test_build_lead_skips_inactive_company():
    from leadcrawler.pipeline.run import _build_lead

    # 비활성(적재 안 될) 회사는 분류 안 함 — LLM 비용 낭비 방지.
    clf = _RecordingClassifier("게임")
    lead = _build_lead(
        _dc(UNCLASSIFIED), enricher=_FakeEnricher(), existence=_FakeExistence(active=False),
        email_validator=_FakeValidator(), classifier=clf,
    )
    assert lead.company.is_active is False
    assert lead.company.industry == UNCLASSIFIED  # 분류 안 돼 미분류 유지
    assert clf.calls == 0  # 비활성 → LLM 호출 안 함


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
