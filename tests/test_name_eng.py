"""표시명 영문 우선(KR 제외, PO 2026-09-04) — 소스(GLEIF otherNames)·적재 시점(홈페이지 영문
상호 추출)·소급 CLI(backfill_name_eng/backfill_gleif_names)·원장/company 동기화 검증."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from leadcrawler.config import Settings
from leadcrawler.enrich import name_eng as ne
from leadcrawler.enrich.enricher import Enricher
from leadcrawler.enrich.industry_classify import StubClassifier
from leadcrawler.pipeline.column_backfill import backfill_gleif_names, backfill_name_eng
from leadcrawler.pipeline.run import _build_lead
from leadcrawler.schema import CompanyRow
from leadcrawler.sources.base import (
    DiscoveredCompany,
    Segment,
    english_display,
    latin_name_or_none,
    needs_english_name,
)
from leadcrawler.sources.gleif import GleifSource, english_other_name
from leadcrawler.storage.db import init_db, session_scope
from leadcrawler.storage.repository import company_id_for, save_discovered
from leadcrawler.verify.email_validator import EmailValidator
from leadcrawler.verify.existence import ExistenceVerifier

JP = "株式会社北陸銀行"
EN = "THE HOKURIKU BANK, LTD."


# ── base 헬퍼 ────────────────────────────────────────────────────────────────────
def test_needs_english_name_excludes_kr_and_latin() -> None:
    assert needs_english_name(JP, "JP") and needs_english_name(JP, "japan")
    assert needs_english_name("บริษัท ไทย", "TH")  # 태국 문자도 원어.
    assert not needs_english_name("삼성전자", "KR") and not needs_english_name("삼성전자", "korea")
    assert not needs_english_name("Sony Corporation", "JP") and not needs_english_name("", "JP")


def test_english_display_rules() -> None:
    assert english_display(JP, EN, "JP") == (EN, JP)
    assert english_display(JP, None, "JP") == (JP, None)
    assert english_display(JP, "hokugin.co.jp", "JP") == (JP, None)  # 도메인은 상호가 아님(GLEIF 자유입력 방어).
    assert english_display(JP, "https://x.co.jp/en", "JP") == (JP, None)
    assert needs_english_name(JP, "Korea, Republic of") is False  # 미해석 국가 = fail-closed.
    assert needs_english_name(JP, "") is False
    assert english_display("삼성전자", "Samsung Electronics", "KR") == ("삼성전자", None)
    assert english_display("Sony Corp", "Sony", "JP") == ("Sony Corp", None)
    assert latin_name_or_none("X") is None and latin_name_or_none("株式会社") is None
    assert latin_name_or_none(' "ABC Holdings, Inc." ') == "ABC Holdings, Inc."


# ── GLEIF 소스 ───────────────────────────────────────────────────────────────────
def _lei_rec(legal: str, others: list[dict], *, country: str = "JP") -> dict:
    return {
        "id": "353800N4X1VP8A22MA24",
        "attributes": {
            "lei": "353800N4X1VP8A22MA24",
            "entity": {
                "status": "ACTIVE",
                "legalName": {"name": legal, "language": "ja"},
                "otherNames": others,
                "legalAddress": {"addressLines": ["1-1"], "city": "Tokyo", "country": country},
            },
        },
    }


def test_english_other_name_picks_only_en_alternative_legal_name() -> None:
    others = [
        {"name": "Hokugin", "language": "en", "type": "TRADING_OR_OPERATING_NAME"},
        {"name": "HOKURIKU GINKO", "language": "ja", "type": "ALTERNATIVE_LANGUAGE_LEGAL_NAME"},
        {"name": EN, "language": "en", "type": "ALTERNATIVE_LANGUAGE_LEGAL_NAME"},
    ]
    assert english_other_name({"otherNames": others}) == EN
    assert english_other_name({"otherNames": []}) is None and english_other_name({}) is None


def test_gleif_candidate_prefers_english_except_kr() -> None:
    src = GleifSource(Settings(dry_run=True))
    en = [{"name": EN, "language": "en", "type": "ALTERNATIVE_LANGUAGE_LEGAL_NAME"}]
    dc = src._candidate(Segment(country="JP", industry="전체"), _lei_rec(JP, en))
    assert dc is not None and dc.name == EN and dc.name_eng == JP
    # 영문명 없으면 원어 유지·name_eng None.
    dc = src._candidate(Segment(country="JP", industry="전체"), _lei_rec(JP, []))
    assert dc is not None and dc.name == JP and dc.name_eng is None
    # KR 은 한국어 유지(PO).
    kr = [{"name": "Samsung Electronics Co., Ltd.", "language": "en",
           "type": "ALTERNATIVE_LANGUAGE_LEGAL_NAME"}]
    dc = src._candidate(Segment(country="KR", industry="전체"), _lei_rec("삼성전자", kr, country="KR"))
    assert dc is not None and dc.name == "삼성전자" and dc.name_eng is None


# ── 추출기 ───────────────────────────────────────────────────────────────────────
def test_accept_english_name_rejects_junk_domain_abstain() -> None:
    assert ne.accept_english_name('"SystemEXE, Inc."', "system-exe.co.jp") == "SystemEXE, Inc."
    assert ne.accept_english_name("ABSTAIN", None) is None
    assert ne.accept_english_name("system-exe.co.jp", "system-exe.co.jp") is None
    assert ne.accept_english_name("https://x.co.jp", None) is None
    assert ne.accept_english_name("HOME", None) is None
    assert ne.accept_english_name("株式会社X", None) is None


def test_build_name_eng_stub_in_dry_run_and_without_key() -> None:
    assert ne.build_name_eng(Settings(dry_run=True)).model == "stub"
    assert ne.build_name_eng(
        Settings(dry_run=False, industry_llm_classify=True, anthropic_api_key="")
    ).model == "stub"
    live = ne.build_name_eng(
        Settings(dry_run=False, industry_llm_classify=True, anthropic_api_key="k")
    )
    assert isinstance(live, ne.ClaudeNameEng)
    assert ne.StubNameEng().extract(JP, "x.co.jp", "<html>THE HOKURIKU BANK</html>") is None


class _Ledger:
    def __init__(self, over: bool = False) -> None:
        self.over, self.recorded = over, []

    def is_over_budget(self) -> bool:
        return self.over

    def record(self, provider: str, units: int = 1) -> None:
        self.recorded.append(provider)


def _fake_client(reply: str):
    msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])
    return SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: msg))


def test_claude_name_eng_extracts_records_and_abstains(monkeypatch) -> None:
    ledger = _Ledger()
    x = ne.ClaudeNameEng(model="m", api_key="k", ledger=ledger, max_calls=2)
    monkeypatch.setattr(ne, "anthropic_client", lambda **kw: _fake_client(EN))
    assert x.extract(JP, "hokugin.co.jp", "<html>The Hokuriku Bank,Ltd. &amp; co</html>") == EN
    assert ledger.recorded == ["name_llm"]
    # 페이지에 없는 이름(환각·인젝션)은 과금됐어도 기각.
    assert x.extract(JP, "hokugin.co.jp", "<html>welcome</html>") is None
    assert ledger.recorded == ["name_llm", "name_llm"]
    assert x.extract(JP, "hokugin.co.jp", None) is None  # 홈페이지 게이트 — 호출·과금 0.
    assert ledger.recorded == ["name_llm", "name_llm"]
    assert ne.appears_in_page("ABC Holdings, Inc.", "<p>ABC HOLDINGS INC</p>")
    assert not ne.appears_in_page("ABC Holdings, Inc.", "<p>XYZ</p>")
    y = ne.ClaudeNameEng(model="m", api_key="k", ledger=_Ledger(), max_calls=1)
    monkeypatch.setattr(ne, "anthropic_client", lambda **kw: _fake_client("ABSTAIN"))
    assert y.extract(JP, "d", "<p>x</p>") is None
    assert y.extract(JP, "d", "<p>x</p>") is None  # 런당캡 1 → 두 번째는 호출 없이 abstain.
    z = ne.ClaudeNameEng(model="m", api_key="k", ledger=_Ledger(over=True))
    assert z.extract(JP, "d", "<p>x</p>") is None  # 예산 초과 → 호출 없이 abstain.


# ── 적재 시점(_build_lead) ───────────────────────────────────────────────────────
class _FakeNameEng:
    model = "fake"

    def __init__(self, out: str | None) -> None:
        self.out, self.calls = out, 0

    def extract(self, name, domain, html):  # noqa: ANN001
        self.calls += 1
        return self.out


def _lead(dc: DiscoveredCompany, name_eng, monkeypatch) -> DiscoveredCompany:
    s = Settings(dry_run=True)
    # dry_run 은 홈페이지를 안 받으므로 게이트 통과를 위해 본문을 흉내낸다.
    monkeypatch.setattr(Enricher, "last_home_html", property(lambda self: "<html>HB</html>"))
    lead = _build_lead(
        dc, enricher=Enricher(s), existence=ExistenceVerifier(s),
        email_validator=EmailValidator(s), classifier=StubClassifier(), name_eng=name_eng,
    )
    assert lead.company.is_active  # 게이트 전제(dry 더미는 전부 실존).
    return lead


def test_build_lead_replaces_non_kr_display_name_in_place(monkeypatch) -> None:
    dc = DiscoveredCompany(canonical_key="d:hokugin.co.jp", name=JP, country="JP",
                           domain="hokugin.co.jp")
    fake = _FakeNameEng(EN)
    lead = _lead(dc, fake, monkeypatch)
    assert fake.calls == 1 and lead.company.name == EN
    assert dc.name == EN and dc.name_eng == JP  # 제자리 갱신 → _persist_lead 가 원장에도 반영.


def test_build_lead_skips_kr_latin_and_keeps_name_on_abstain(monkeypatch) -> None:
    kr = DiscoveredCompany(canonical_key="d:a.kr", name="삼성전자", country="KR", domain="a.kr")
    fake = _FakeNameEng(EN)
    assert _lead(kr, fake, monkeypatch).company.name == "삼성전자" and fake.calls == 0
    latin = DiscoveredCompany(canonical_key="d:b.jp", name="Sony Corp", country="JP", domain="b.jp")
    assert _lead(latin, fake, monkeypatch).company.name == "Sony Corp" and fake.calls == 0
    jp = DiscoveredCompany(canonical_key="d:c.jp", name=JP, country="JP", domain="c.jp")
    assert _lead(jp, _FakeNameEng(None), monkeypatch).company.name == JP and jp.name_eng is None
    assert _lead(jp, None, monkeypatch).company.name == JP  # 추출기 미주입(기존 호출부) 무영향.


# ── 원장·company 동기화 + 소급 ─────────────────────────────────────────────────────
def _db(tmp_path) -> Settings:
    s = Settings(dry_run=True, database_url=f"sqlite:///{tmp_path / 't.db'}")
    init_db(s)
    return s


def _seed(session, key: str, name: str, *, country="JP", registry=None, registry_id=None,
          domain=None, promoted=False) -> None:
    save_discovered(session, DiscoveredCompany(
        canonical_key=key, name=name, country=country, registry=registry,
        registry_id=registry_id, domain=domain, source="t",
    ))
    session.flush()  # company FK(discovered_company) 선행 확정.
    if promoted:
        session.add(CompanyRow(id=company_id_for(key), canonical_key=key, name=name,
                               country=country, industry="", is_active=True))


def test_save_discovered_english_rediscovery_also_updates_company(tmp_path) -> None:
    s = _db(tmp_path)
    with session_scope(s) as db:
        _seed(db, "reg:lei:L1", JP, registry="lei", registry_id="L1", promoted=True)
    with session_scope(s) as db:
        row = save_discovered(db, DiscoveredCompany(
            canonical_key="reg:lei:L1", name=EN, name_eng=JP, country="JP",
            registry="lei", registry_id="L1", source="gleif",
        ))
        assert row.name == EN and row.name_eng == JP
        assert db.get(CompanyRow, company_id_for("reg:lei:L1")).name == EN


def test_backfill_gleif_names_batches_and_skips_kr(tmp_path) -> None:
    s = _db(tmp_path)
    with session_scope(s) as db:
        _seed(db, "reg:lei:L1", JP, registry="lei", registry_id="L1", promoted=True)
        _seed(db, "reg:lei:L2", "日本マスタートラスト信託銀行株式会社", registry="lei",
              registry_id="L2")
        _seed(db, "reg:lei:K1", "삼성전자", country="KR", registry="lei", registry_id="K1")
        _seed(db, "reg:lei:L3", "Already Latin KK", registry="lei", registry_id="L3")
    seen_params: list[dict] = []

    def fetch_json(url, params):  # noqa: ANN001
        seen_params.append(params)
        ids = params["filter[lei]"].split(",")
        en = {"L1": EN, "L2": "The Master Trust Bank of Japan, Ltd."}
        return {"data": [
            {"id": i, "attributes": {"entity": {"otherNames": [
                {"name": en[i], "language": "en", "type": "ALTERNATIVE_LANGUAGE_LEGAL_NAME"}
            ]}}} for i in ids if i in en
        ]}

    with session_scope(s) as db:
        seen, upd = backfill_gleif_names(db, fetch_json=fetch_json, batch=1)
        assert (seen, upd) == (2, 2)  # KR·라틴 행은 대상에서 제외.
        assert sorted(p["filter[lei]"] for p in seen_params) == ["L1", "L2"]
    with session_scope(s) as db:
        from leadcrawler.schema import DiscoveredCompanyRow

        r1 = db.get(DiscoveredCompanyRow, "reg:lei:L1")
        assert r1.name == EN and r1.name_eng == JP
        assert db.get(CompanyRow, company_id_for("reg:lei:L1")).name == EN
        assert db.get(DiscoveredCompanyRow, "reg:lei:K1").name == "삼성전자"
        # 멱등: 재실행은 대상 0.
        assert backfill_gleif_names(db, fetch_json=fetch_json) == (0, 0)


def test_backfill_name_eng_homepage_then_en_fallback(tmp_path) -> None:
    s = _db(tmp_path)
    with session_scope(s) as db:
        _seed(db, "d:a.jp", JP, domain="a.jp", promoted=True)  # 홈페이지에서 바로.
        _seed(db, "d:b.jp", "株式会社ビー", domain="b.jp")  # 홈페이지 abstain → /en/ 에서.
        _seed(db, "d:c.jp", "株式会社シー", domain="c.jp")  # fetch 실패 → 스킵.
        _seed(db, "d:nodomain", "株式会社ディー")  # 도메인 없음 → 대상 아님.
        _seed(db, "d:k.kr", "한국회사", country="KR", domain="k.kr")  # KR 제외.

    def fetch_html(url: str) -> str | None:
        return None if "c.jp" in url else f"<html>{url}</html>"

    class X:
        model = "fake"
        calls: list[tuple[str, str]] = []

        def extract(self, name, domain, html):  # noqa: ANN001
            X.calls.append((domain, html))
            if domain == "a.jp":
                return "A Holdings, Inc."
            if domain == "b.jp" and "/en/" in html:
                return "B Company Limited"
            return None

    with session_scope(s) as db:
        seen, upd = backfill_name_eng(db, X(), fetch_html=fetch_html, commit_every=1)
        assert (seen, upd) == (3, 2)
        assert [d for d, _ in X.calls] == ["a.jp", "b.jp", "b.jp"]  # c.jp 는 본문 없어 호출 0.
    with session_scope(s) as db:
        from leadcrawler.schema import DiscoveredCompanyRow

        a = db.get(DiscoveredCompanyRow, "d:a.jp")
        assert a.name == "A Holdings, Inc." and a.name_eng == JP
        assert db.get(CompanyRow, company_id_for("d:a.jp")).name == "A Holdings, Inc."
        assert db.get(DiscoveredCompanyRow, "d:b.jp").name == "B Company Limited"
        assert db.get(DiscoveredCompanyRow, "d:k.kr").name == "한국회사"


@pytest.mark.parametrize("country", ["KR", "korea"])
def test_kr_always_kept(country: str) -> None:
    assert english_display("한국회사", "Korea Co., Ltd.", country) == ("한국회사", None)
