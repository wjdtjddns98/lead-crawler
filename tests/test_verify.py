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
