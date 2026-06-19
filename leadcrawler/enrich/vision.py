"""Vision — OCR 로도 못 읽은 이미지 이메일을 Claude Vision 으로 추출(유료·최후수단).

anthropic SDK 는 **선택적 의존성**(extra ``ai``) + ``anthropic_api_key`` 필요.
escalation 최종 티어로, 정적·헤드리스·OCR 이 모두 실패한 소수 기업에만, ``enrich_vision``
을 켜고 키가 있을 때만 호출된다. **호출당 과금**되므로 기본 off + ``vision_max_images``
로 엄격히 제한하고, 호출마다 구조화 로그(``enrich.vision.call``)를 남겨 향후 cost_ledger
연결점으로 쓴다. 미설치/키없음/오류는 빈 문자열 폴백(파이프라인 무중단).

테스트는 :class:`SupportsVision` 가짜 구현을 주입해 API·네트워크 없이 검증한다.
"""

from __future__ import annotations

import base64
from typing import Protocol

from ..logging import get_logger

log = get_logger("enrich.vision")

# 확장자 → media_type(anthropic image 블록용).
_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
_EXTRACT_PROMPT = "이 이미지에 보이는 이메일 주소를 모두 그대로(텍스트로) 출력하라. 없으면 빈 응답."


def media_type_for(url: str) -> str:
    """이미지 URL 확장자로 media_type 을 추정한다(기본 image/png)."""
    ext = url.lower().split("?", 1)[0].rsplit(".", 1)[-1]
    return _MEDIA_TYPES.get(ext, "image/png")


class SupportsVision(Protocol):
    """Vision OCR 엔진 인터페이스(테스트 더블이 구현)."""

    def extract_text(self, image: bytes, *, media_type: str = "image/png") -> str:
        """이미지 바이트에서 텍스트(이메일 등)를 추출(실패 시 빈 문자열)."""
        ...


class ClaudeVision:
    """Claude Vision 기반 이미지→텍스트 추출 — 미설치/오류 시 빈 문자열(graceful)."""

    def __init__(self, api_key: str, *, model: str, max_tokens: int = 300) -> None:
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens

    def extract_text(self, image: bytes, *, media_type: str = "image/png") -> str:
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=self._api_key)
            b64 = base64.standard_b64encode(image).decode("ascii")
            msg = client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": _EXTRACT_PROMPT},
                        ],
                    }
                ],
            )
            # 과금 호출 추적(향후 cost_ledger 연결점).
            log.info("enrich.vision.call", model=self._model)
            return "".join(
                b.text for b in msg.content if getattr(b, "type", None) == "text"
            )
        except Exception as exc:  # 미설치(ImportError)·키오류·API오류 → 빈 결과.
            log.info("enrich.vision.error", err=str(exc))
            return ""
