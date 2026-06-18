"""structlog 기반 구조화 로깅 설정.

단계별 진행 상황을 key=value 로 남겨 24/7 운영 시 발견/신규/중복/검증 카운터를
추적하기 쉽게 한다.
"""

from __future__ import annotations

import logging

import structlog


def configure_logging(level: int = logging.INFO) -> None:
    """structlog 를 표준 로깅 위에 구성한다(중복 호출 안전)."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.KeyValueRenderer(
                key_order=["timestamp", "level", "event"]
            ),
        ],
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """이름 바인딩된 structlog 로거를 반환한다."""
    return structlog.get_logger(name)
