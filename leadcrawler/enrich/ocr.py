"""OCR — 이미지로 노출된(스팸회피) 이메일을 텍스트로 추출.

Tesseract(pytesseract + Pillow)는 **선택적 의존성**(extra ``ocr`` —
``pip install lead-crawler[ocr]`` + 시스템 tesseract 바이너리). 미설치/실패해도
``image_to_text`` 는 빈 문자열을 돌려줄 뿐 파이프라인을 깨지 않는다(보강 폴백).
비용 0(로컬). dry_run 은 이 경로를 타지 않으며 라이브에서 ``enrich_ocr`` 켤 때만 동작.

테스트는 :class:`SupportsOcr` 를 구현한 가짜 OCR 을 주입해 바이너리·네트워크 없이 검증한다.
"""

from __future__ import annotations

import io
from typing import Protocol

from ..logging import get_logger

log = get_logger("enrich.ocr")


class SupportsOcr(Protocol):
    """OCR 엔진 인터페이스(테스트 더블이 구현)."""

    def image_to_text(self, image: bytes) -> str:
        """이미지 바이트에서 텍스트를 추출(실패 시 빈 문자열)."""
        ...


class TesseractOcr:
    """pytesseract 기반 로컬 OCR — 미설치/오류 시 빈 문자열(graceful)."""

    def __init__(self, *, langs: str = "eng+kor") -> None:
        self._langs = langs

    def image_to_text(self, image: bytes) -> str:
        try:
            import pytesseract
            from PIL import Image

            img = Image.open(io.BytesIO(image))
            return pytesseract.image_to_string(img, lang=self._langs) or ""
        except Exception as exc:  # 미설치·바이너리 부재·디코드 실패 → 빈 결과.
            log.info("ocr.unavailable", err=str(exc))
            return ""
