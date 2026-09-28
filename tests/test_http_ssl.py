"""Fetcher 의 SSL 인증서 폴백 판정(_is_ssl_error) 단위 테스트.

verify=False 폴백을 걸지(=인증서 검증 실패) 아니면 그대로 재전파할지 가르는 분기라,
이 판정이 잘못되면 정상 네트워크 오류까지 검증을 끄거나(과잉) 인증서 깨진 사이트를
계속 놓친다(과소). 순수 함수라 네트워크 없이 검증한다.
"""

from __future__ import annotations

import ssl

import httpx

from leadcrawler.sources.http import _is_ssl_error


def test_ssl_error_in_connecterror_chain_detected() -> None:
    # httpx 는 TLS 실패를 ConnectError 로 감싸고 원인에 ssl.SSLError 를 둔다.
    cause = ssl.SSLCertVerificationError("certificate verify failed: self-signed certificate")
    wrapped = httpx.ConnectError("connection failed")
    wrapped.__cause__ = cause
    assert _is_ssl_error(wrapped) is True


def test_plain_ssl_error_detected() -> None:
    assert _is_ssl_error(ssl.SSLError("CERTIFICATE_VERIFY_FAILED")) is True


def test_non_ssl_error_not_flagged() -> None:
    # 순수 연결 실패(DNS·거부 등)는 폴백 대상이 아니다 — 그대로 재전파돼야 한다.
    assert _is_ssl_error(httpx.ConnectError("getaddrinfo failed")) is False
    assert _is_ssl_error(httpx.ReadTimeout("timeout")) is False


def _ssl_connect_error() -> httpx.ConnectError:
    exc = httpx.ConnectError("connection failed")
    exc.__cause__ = ssl.SSLCertVerificationError("unable to get local issuer certificate")
    return exc


class _Client:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.calls = 0

    def post(self, url, **kw):
        self.calls += 1
        if self.fail:
            raise _ssl_connect_error()
        return httpx.Response(200, text="ok", request=httpx.Request("POST", url))


def test_form_post_falls_back_to_insecure_on_ssl_error() -> None:
    # HNX(인증서 사슬 불완전) 폼 POST 는 verify=False 폴백으로 성공해야 한다.
    from leadcrawler.sources.http import Fetcher

    f = Fetcher(min_interval=0)
    f._client, f._insecure_client = _Client(fail=True), _Client(fail=False)
    assert f.post_text("https://x.test/list", data={"a": 1}) == "ok"
    assert f._insecure_client.calls == 1


def test_json_post_never_falls_back_to_insecure() -> None:
    # JSON POST(자격증명 API)는 검증 실패를 그대로 올린다 — 폴백 금지.
    import pytest

    from leadcrawler.sources.http import Fetcher

    f = Fetcher(min_interval=0)
    f._client, f._insecure_client = _Client(fail=True), _Client(fail=False)
    with pytest.raises(httpx.ConnectError):
        f.post_json("https://x.test/api", json={"k": 1})
    assert f._insecure_client.calls == 0
