"""Windows Proactor 접속 종료 패치 — 원격 RST 뒤에도 소켓 정리가 끝나는지."""

from asyncio.proactor_events import _ProactorBasePipeTransport

from leadcrawler.logging import patch_proactor_connection_lost


class _Sock:
    closed = False
    err: type[OSError] = ConnectionResetError

    def fileno(self) -> int:
        return 7

    def shutdown(self, how: int) -> None:
        raise self.err(10054, "원격 호스트에 의해 강제로 끊겼습니다")

    def close(self) -> None:
        self.closed = True


class _Server:
    detached = None

    def _detach(self, transport) -> None:
        self.detached = transport


class _Proto:
    def connection_lost(self, exc) -> None:
        pass


def _transport() -> _ProactorBasePipeTransport:
    t = object.__new__(_ProactorBasePipeTransport)
    t._called_connection_lost = False
    t._protocol = _Proto()
    t._sock = _Sock()
    t._server = _Server()
    return t


def test_patch_swallows_reset_and_finishes_cleanup() -> None:
    patch_proactor_connection_lost()
    patch_proactor_connection_lost()  # idempotent
    t = _transport()
    sock, server = t._sock, t._server
    t._call_connection_lost(None)  # 예외 없이 끝나야 한다
    assert sock.closed and t._sock is None
    assert server.detached is t and t._server is None
    assert t._called_connection_lost is True
    t._call_connection_lost(None)  # 재호출은 조기 반환(원본 가드 유지)


def test_patch_tolerates_transport_without_server() -> None:
    patch_proactor_connection_lost()
    t = _transport()
    t._server = None  # 클라이언트측 전송체(서버 없음)
    t._call_connection_lost(None)
    assert t._sock is None and t._called_connection_lost is True


def test_patch_covers_sibling_abort_error() -> None:
    patch_proactor_connection_lost()
    t = _transport()
    t._sock.err = ConnectionAbortedError  # WinError 10053
    t._call_connection_lost(None)
    assert t._sock is None and t._called_connection_lost is True
