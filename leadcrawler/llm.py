"""Anthropic SDK 클라이언트 공용 생성기 — 호출자가 인스턴스에 보관해 재사용한다.

배경: 분류기·판정기·Vision·AI디렉토리·도메인중재기 5곳이 **API 호출마다** ``anthropic.Anthropic()``
을 새로 만들고 있었다(SSL 컨텍스트·커넥션풀 매번 새로 → 콜마다 TLS 핸드셰이크; 업종분류는 런당
최대 5,000콜). SDK 클라이언트는 스레드-안전하므로 인증정보당 1개면 충분하다.

``import anthropic`` 은 여기서 지연 import 한다 — 미설치 시 ImportError 가 호출자의
graceful(abstain) 경로로 그대로 흘러가야 한다(호출 시점에 try 안에서 불릴 것).
"""

from __future__ import annotations

from typing import Any


def anthropic_client(*, api_key: str = "", auth_token: str = "", max_retries: int = 2) -> Any:
    """auth_token(OAuth Bearer, 구독) 우선, 없으면 api_key(x-api-key, 종량) 클라이언트."""
    import anthropic

    if auth_token:
        return anthropic.Anthropic(auth_token=auth_token, max_retries=max_retries)
    return anthropic.Anthropic(api_key=api_key, max_retries=max_retries)
