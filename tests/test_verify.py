"""이메일 검증 테스트 — MX/도메인 1차 + SMTP 프로브 보정(네트워크 없음)."""

from __future__ import annotations

import pytest

from leadcrawler.config import Settings
from leadcrawler.models import ValidationStatus
from leadcrawler.verify import email_validator as ev_mod
from leadcrawler.verify.email_validator import (
    SMTP_DELIVERABLE,
    SMTP_UNDELIVERABLE,
    SMTP_UNKNOWN,
    EmailValidator,
    SmtpProber,
)


class FakeProber:
    """SupportsSmtpProbe 더블 — 고정 결과 반환 + 호출 기록."""

    def __init__(self, result: str) -> None:
        self.result = result
        self.calls: list[tuple[str, list[str]]] = []

    def probe(self, email: str, mx_hosts: list[str]) -> str:
        self.calls.append((email, mx_hosts))
        return self.result


def _patch_mx(monkeypatch: pytest.MonkeyPatch, hosts: tuple[str, ...] = ("mx1.test",)) -> None:
    """라이브 경로의 실 DNS 조회를 가짜 MX 호스트로 대체(네트워크 차단)."""
    monkeypatch.setattr(ev_mod, "_resolve_mx", lambda d, s: (bool(hosts), list(hosts)))


def _smtp_settings(**over: object) -> Settings:
    """SMTP 라이브 검증용 설정(실제 MAIL FROM — placeholder 가드 회피)."""
    return Settings(
        dry_run=False, email_smtp_check=True, email_smtp_from="verify@leadcrawler.io", **over
    )


# --- dry_run / 형식 -----------------------------------------------------

def test_dry_run_no_smtp() -> None:
    v = EmailValidator(Settings(dry_run=True)).validate("ir@acme.com", "acme.com")
    assert v.status is ValidationStatus.VALID
    assert v.smtp is None and v.provider == "dry_run"


def test_format_invalid_short_circuits() -> None:
    v = EmailValidator(Settings(dry_run=True)).validate("not-an-email")
    assert v.status is ValidationStatus.INVALID and v.smtp is None


# --- SMTP opt-in 보정(라이브, 가짜 프로버) -----------------------------

def test_live_smtp_off_does_not_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mx(monkeypatch)
    prober = FakeProber(SMTP_DELIVERABLE)
    v = EmailValidator(
        Settings(dry_run=False, email_smtp_check=False), smtp_prober=prober
    ).validate("ir@acme.com", "acme.com")
    assert prober.calls == []  # opt-in off → 프로브 미호출.
    assert v.smtp is None and v.provider == "mx"


def test_smtp_undeliverable_forces_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mx(monkeypatch)
    prober = FakeProber(SMTP_UNDELIVERABLE)
    v = EmailValidator(_smtp_settings(), smtp_prober=prober).validate("ir@acme.com", "acme.com")
    assert v.status is ValidationStatus.INVALID and v.smtp is False and v.provider == "smtp"


def test_smtp_deliverable_upgrades_risky_to_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mx(monkeypatch)
    prober = FakeProber(SMTP_DELIVERABLE)
    # 도메인 불일치 → 1차 RISKY, SMTP 수신확정 → VALID 승격.
    v = EmailValidator(_smtp_settings(), smtp_prober=prober).validate(
        "ir@mail.acme.com", "other.com"
    )
    assert v.status is ValidationStatus.VALID and v.smtp is True and v.provider == "smtp"


def test_smtp_catchall_unknown_keeps_status_and_mx_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_mx(monkeypatch)
    prober = FakeProber(SMTP_UNKNOWN)
    v = EmailValidator(_smtp_settings(), smtp_prober=prober).validate("ir@acme.com", "other.com")
    # 판정불가 → 1차(RISKY) 유지, SMTP 기여 없으므로 provider 는 mx.
    assert v.status is ValidationStatus.RISKY and v.smtp is None and v.provider == "mx"


def test_placeholder_mail_from_skips_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mx(monkeypatch)
    prober = FakeProber(SMTP_UNDELIVERABLE)
    # 기본 email_smtp_from=verify@example.com(예약) → 프로브 스킵, MX 판정 유지.
    v = EmailValidator(
        Settings(dry_run=False, email_smtp_check=True), smtp_prober=prober
    ).validate("ir@acme.com", "acme.com")
    assert prober.calls == [] and v.smtp is None and v.provider == "mx"
    assert v.status is ValidationStatus.VALID


def test_no_mx_is_invalid_without_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ev_mod, "_resolve_mx", lambda d, s: (False, []))
    prober = FakeProber(SMTP_DELIVERABLE)
    v = EmailValidator(_smtp_settings(), smtp_prober=prober).validate("ir@acme.com", "acme.com")
    assert v.status is ValidationStatus.INVALID and prober.calls == []  # MX 없으면 미시도.


# --- SmtpProber catch-all 로직(가짜 smtplib) ---------------------------

class _FakeSMTP:
    """smtplib.SMTP 컨텍스트매니저 더블 — rcpt 코드를 주소→코드 함수로 라우팅."""

    def __init__(self, code_fn) -> None:
        self._code_fn = code_fn

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def ehlo_or_helo_if_needed(self) -> None:
        pass

    def mail(self, sender: str) -> None:
        pass

    def rcpt(self, addr: str) -> tuple[int, bytes]:
        return self._code_fn(addr), b"ok"


def _fake_smtp_factory(code_fn):
    return lambda host, port, timeout=10.0: _FakeSMTP(code_fn)


def test_prober_deliverable_when_probe_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    import smtplib

    # 실주소 250, 비존재 catch-all 주소 550 → 진짜 수신 확정.
    def code(addr: str) -> int:
        return 550 if "no-such-mailbox" in addr else 250

    monkeypatch.setattr(smtplib, "SMTP", _fake_smtp_factory(code))
    assert SmtpProber("v@x.com").probe("ir@acme.com", ["mx.acme.com"]) == SMTP_DELIVERABLE


def test_prober_catchall_when_probe_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", _fake_smtp_factory(lambda a: 250))  # 전부 250.
    assert SmtpProber("v@x.com").probe("ir@acme.com", ["mx.acme.com"]) == SMTP_UNKNOWN


def test_prober_undeliverable_on_550(monkeypatch: pytest.MonkeyPatch) -> None:
    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", _fake_smtp_factory(lambda a: 550))
    assert SmtpProber("v@x.com").probe("ir@acme.com", ["mx.acme.com"]) == SMTP_UNDELIVERABLE


def test_prober_unknown_when_all_hosts_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    import smtplib

    def _boom(host, port, timeout=10.0):
        raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", _boom)
    assert SmtpProber("v@x.com").probe("ir@acme.com", ["mx.acme.com"]) == SMTP_UNKNOWN


# --- 딜리버러빌리티 API 티어(opt-in·유료, 가짜 체커 주입) --------------

from leadcrawler.verify.deliverability import (  # noqa: E402
    DELIVERABLE,
    UNDELIVERABLE,
    UNKNOWN,
    NeverBounceChecker,
    ZeroBounceChecker,
    build_deliverability_checker,
)


class FakeChecker:
    """SupportsDeliverability 더블 — 고정 verdict 반환 + 호출 기록."""

    name = "fake"

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.calls: list[str] = []

    def check(self, email: str) -> str:
        self.calls.append(email)
        return self.verdict


def _deliv_settings(**over: object) -> Settings:
    """딜리버러빌리티 라이브 검증용 설정(키는 주입 체커가 대신해 불필요)."""
    return Settings(dry_run=False, email_deliverability_check=True, **over)


def test_deliverability_off_does_not_check(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mx(monkeypatch)
    checker = FakeChecker(UNDELIVERABLE)
    v = EmailValidator(
        Settings(dry_run=False, email_deliverability_check=False),
        deliverability_checker=checker,
    ).validate("ir@acme.com", "acme.com")
    assert checker.calls == []  # opt-in off → 미호출.
    assert v.provider == "mx"


def test_dry_run_no_deliverability() -> None:
    checker = FakeChecker(UNDELIVERABLE)
    v = EmailValidator(
        Settings(dry_run=True, email_deliverability_check=True),
        deliverability_checker=checker,
    ).validate("ir@acme.com", "acme.com")
    assert checker.calls == [] and v.provider == "dry_run"


def test_deliverability_undeliverable_forces_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mx(monkeypatch)
    checker = FakeChecker(UNDELIVERABLE)
    v = EmailValidator(_deliv_settings(), deliverability_checker=checker).validate(
        "ir@acme.com", "acme.com"
    )
    assert v.status is ValidationStatus.INVALID and v.provider == "fake"


def test_deliverability_deliverable_upgrades_risky(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mx(monkeypatch)
    checker = FakeChecker(DELIVERABLE)
    # 도메인 불일치 → 1차 RISKY, 딜리버러빌리티 확정 → VALID 승격.
    v = EmailValidator(_deliv_settings(), deliverability_checker=checker).validate(
        "ir@mail.acme.com", "other.com"
    )
    assert v.status is ValidationStatus.VALID and v.provider == "fake"


def test_deliverability_unknown_keeps_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mx(monkeypatch)
    checker = FakeChecker(UNKNOWN)
    v = EmailValidator(_deliv_settings(), deliverability_checker=checker).validate(
        "ir@acme.com", "other.com"
    )
    # 판정불가 → 1차(RISKY) 유지, 기여 없으므로 provider 는 mx.
    assert v.status is ValidationStatus.RISKY and v.provider == "mx"


def test_deliverability_skipped_when_already_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_mx(monkeypatch)
    prober = FakeProber(SMTP_UNDELIVERABLE)
    checker = FakeChecker(DELIVERABLE)
    # SMTP 가 먼저 INVALID 확정 → 과금 딜리버러빌리티 호출은 스킵.
    v = EmailValidator(
        Settings(
            dry_run=False,
            email_smtp_check=True,
            email_smtp_from="verify@leadcrawler.io",
            email_deliverability_check=True,
        ),
        smtp_prober=prober,
        deliverability_checker=checker,
    ).validate("ir@acme.com", "acme.com")
    assert v.status is ValidationStatus.INVALID and checker.calls == []


def test_deliverability_skipped_when_no_mx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ev_mod, "_resolve_mx", lambda d, s: (False, []))
    checker = FakeChecker(DELIVERABLE)
    v = EmailValidator(_deliv_settings(), deliverability_checker=checker).validate(
        "ir@acme.com", "acme.com"
    )
    assert v.status is ValidationStatus.INVALID and checker.calls == []  # MX 없으면 미호출.


# --- 팩토리 키게이트 ---------------------------------------------------

def test_build_checker_none_without_keys() -> None:
    assert build_deliverability_checker(Settings(), fetcher=object()) is None


def test_build_checker_prefers_zerobounce() -> None:
    s = Settings(zerobounce_api_key="zb", neverbounce_api_key="nb")
    c = build_deliverability_checker(s, fetcher=object())
    assert isinstance(c, ZeroBounceChecker) and c.name == "zerobounce"


def test_build_checker_falls_back_to_neverbounce() -> None:
    s = Settings(neverbounce_api_key="nb")
    c = build_deliverability_checker(s, fetcher=object())
    assert isinstance(c, NeverBounceChecker) and c.name == "neverbounce"


# --- 제공자 파서(가짜 페처, 네트워크 0) -------------------------------

class _FakeFetcher:
    """get_json/post_json 더블 — 고정 응답 반환 + 호출 메서드 기록."""

    def __init__(self, payload: object, *, boom: bool = False) -> None:
        self.payload = payload
        self.boom = boom
        self.method: str | None = None

    def get_json(self, url, *, params=None, headers=None):
        self.method = "GET"
        if self.boom:
            raise RuntimeError("api error")
        return self.payload

    def post_json(self, url, *, json=None, params=None, headers=None):
        self.method = "POST"
        if self.boom:
            raise RuntimeError("api error")
        return self.payload


@pytest.mark.parametrize(
    "status,expected",
    [
        ("valid", DELIVERABLE),
        ("invalid", UNDELIVERABLE),
        ("spamtrap", UNDELIVERABLE),
        ("do_not_mail", UNDELIVERABLE),
        ("catch-all", UNKNOWN),
        ("unknown", UNKNOWN),
        ("", UNKNOWN),
    ],
)
def test_zerobounce_maps_status(status: str, expected: str) -> None:
    f = _FakeFetcher({"status": status})
    assert ZeroBounceChecker("k", fetcher=f).check("ir@acme.com") == expected
    assert f.method == "GET"


def test_zerobounce_error_is_unknown() -> None:
    f = _FakeFetcher(None, boom=True)
    assert ZeroBounceChecker("k", fetcher=f).check("ir@acme.com") == UNKNOWN


@pytest.mark.parametrize(
    "result,expected",
    [
        ("valid", DELIVERABLE),
        ("invalid", UNDELIVERABLE),
        ("disposable", UNDELIVERABLE),
        ("catchall", UNKNOWN),
        ("unknown", UNKNOWN),
    ],
)
def test_neverbounce_maps_result(result: str, expected: str) -> None:
    f = _FakeFetcher({"status": "success", "result": result})
    assert NeverBounceChecker("k", fetcher=f).check("ir@acme.com") == expected
    assert f.method == "POST"  # NeverBounce 는 POST.


def test_neverbounce_non_success_is_unknown() -> None:
    f = _FakeFetcher({"status": "auth_failure", "message": "bad key"})
    assert NeverBounceChecker("k", fetcher=f).check("ir@acme.com") == UNKNOWN


def test_neverbounce_error_is_unknown() -> None:
    f = _FakeFetcher(None, boom=True)
    assert NeverBounceChecker("k", fetcher=f).check("ir@acme.com") == UNKNOWN
