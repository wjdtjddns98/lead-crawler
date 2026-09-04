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
