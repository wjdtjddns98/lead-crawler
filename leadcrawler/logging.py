"""structlog 기반 구조화 로깅 설정.

단계별 진행 상황을 key=value 로 남겨 24/7 운영 시 발견/신규/중복/검증 카운터를
추적하기 쉽게 한다.
"""

from __future__ import annotations

import logging
import sys
import threading

import structlog

_child_windows_suppressed = False
_suppress_lock = threading.Lock()


def suppress_child_windows() -> None:
    """Windows에서 모든 자식 프로세스의 콘솔창 플래시를 억제한다(idempotent·Windows 전용).

    서버·크롤러 백엔드는 어떤 자식도 콘솔창을 띄울 이유가 없다. Agent SDK(claude.exe)·
    playwright(node 드라이버)가 매 호출 콘솔창을 번쩍이던 문제 — asyncio Proactor·playwright·
    SDK 가 전부 :class:`subprocess.Popen` 으로 수렴하므로 그 경계 한 곳에서 잡는다(각 스포너를
    따로 고치지 않고 근본 경계에서 1회). ``CREATE_NO_WINDOW`` 는 콘솔 생성 자체를, STARTUPINFO
    ``SW_HIDE`` 는 앱이 강제로 AllocConsole 해도 그 창을 숨긴다(Node SEA/드라이버 대비 둘 다 필요).
    ponytail: 전역 Popen 패치지만 백엔드 자식은 창이 필요 없어 안전. 비-Windows 는 no-op.
    """
    global _child_windows_suppressed
    if _child_windows_suppressed or sys.platform != "win32":
        return
    with _suppress_lock:
        if _child_windows_suppressed:
            return
        import subprocess

        _flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        _orig_init = subprocess.Popen.__init__

        def _init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | _flag
            if kwargs.get("startupinfo") is None:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
                kwargs["startupinfo"] = si
            return _orig_init(self, *args, **kwargs)

        subprocess.Popen.__init__ = _init  # type: ignore[method-assign]
        _child_windows_suppressed = True


def patch_proactor_connection_lost() -> None:
    """Windows Proactor 루프의 접속 종료 정리에서 소켓 shutdown 실패를 삼킨다(멱등).

    CPython(3.14.5 기준) ``_ProactorBasePipeTransport._call_connection_lost`` 는 ``finally`` 에서
    ``sock.shutdown()`` 을 무방비로 호출한다 — 브라우저가 keep-alive 를 RST 로 끊으면
    ``ConnectionResetError(10054)`` 가 터져 uvicorn 콘솔에 "Exception in callback" 트레이스백이
    찍히고, 그 뒤의 ``close()``·``server._detach()`` 까지 건너뛴다(소켓이 GC 까지 안 닫힘).
    원본과 같은 본문에서 ``shutdown()`` 한 줄만 ``OSError`` 로 보호한다(10053 abort 등 형제 포함).
    프로세스 전역 패치 — HTTP 소켓뿐 아니라 모든 Proactor 전송(서브프로세스 파이프 등)에 적용된다.
    ponytail: 비-Windows 에서도 클래스는 있지만 쓰이지 않으므로 무조건 패치해도 무해.
    """
    import socket
    from asyncio.proactor_events import _ProactorBasePipeTransport as _T

    def _call_connection_lost(self, exc):  # type: ignore[no-untyped-def]
        if self._called_connection_lost:
            return
        try:
            self._protocol.connection_lost(exc)
        finally:
            if hasattr(self._sock, "shutdown") and self._sock.fileno() != -1:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # 원격이 먼저 끊은 소켓 — 어차피 바로 close 한다.
            self._sock.close()
            self._sock = None
            server = self._server
            if server is not None:
                server._detach(self)
                self._server = None
            self._called_connection_lost = True

    _T._call_connection_lost = _call_connection_lost  # type: ignore[method-assign]


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
