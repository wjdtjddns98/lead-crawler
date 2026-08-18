"""Windows Job Object 래퍼 — 백필 자식 프로세스 트리의 수명 결속(#352 PR④).

자식(python -m leadcrawler.cli ...)과 그 손자(Playwright node/Chromium)를 Job Object 에
넣고 ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` 를 걸면, **서버 프로세스가 어떻게 죽든**
(정상 종료·크래시·강제 kill) Job 핸들이 닫히는 순간 트리 전체가 함께 정리된다 —
PID 기반 taskkill(재사용 오폭 위험) 없이. 비-Windows(CI 리눅스)는 전부 no-op.

ponytail: pywin32 의존성 대신 ctypes 최소 래퍼 — 필요한 API 3개뿐이다.
"""

from __future__ import annotations

import ctypes
import sys

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:  # pragma: no cover — CI(리눅스)에선 임포트만 되고 실행 안 됨.
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # HANDLE 은 포인터 크기 — restype 미지정(기본 c_int 32bit)이면 64bit 핸들이 잘릴 수
    # 있다. BOOL 반환 함수들도 명시(2026-08-18 리뷰 LOW).
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),  # ULONG_PTR(포인터 크기 정수 — 포인터 아님).
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9


class JobHandle:
    """Job Object 핸들 — ``create_job_for`` 로만 생성. terminate/close 로 정리."""

    def __init__(self, handle: int | None) -> None:
        self._handle = handle

    def terminate(self) -> None:
        """Job 안의 전 프로세스 트리를 즉시 종료 후 핸들 닫기. 멱등."""
        if self._handle is None:
            return
        if IS_WINDOWS:  # pragma: no cover
            _kernel32.TerminateJobObject(self._handle, 1)
        self.close()

    def close(self) -> None:
        """핸들만 닫기 — KILL_ON_JOB_CLOSE 라 마지막 핸들이면 트리도 종료. 멱등."""
        if self._handle is None:
            return
        if IS_WINDOWS:  # pragma: no cover
            _kernel32.CloseHandle(self._handle)
        self._handle = None


def create_job_for(process_handle: int) -> JobHandle:
    """KILL_ON_JOB_CLOSE Job 을 만들어 자식 프로세스를 배정한다.

    생성·배정 어느 쪽이든 실패하면 OSError(fail-closed — 호출자가 자식을 죽여야 한다:
    "Job 없이 일단 실행" 폴백은 서버 사망 시 고아 Chromium 을 남긴다). 비-Windows 는
    no-op 핸들 반환(프로세스그룹 정리는 OS 별 상이 — 이 기능은 Windows 운영 전용).
    """
    if not IS_WINDOWS:
        return JobHandle(None)
    job = _kernel32.CreateJobObjectW(None, None)  # pragma: no cover — 이하 Windows 전용.
    if not job:
        raise OSError(f"CreateJobObject 실패: {ctypes.get_last_error()}")
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _kernel32.SetInformationJobObject(
        job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    )
    if not ok:
        _kernel32.CloseHandle(job)
        raise OSError(f"SetInformationJobObject 실패: {ctypes.get_last_error()}")
    if not _kernel32.AssignProcessToJobObject(job, process_handle):
        err = ctypes.get_last_error()
        _kernel32.CloseHandle(job)
        raise OSError(f"AssignProcessToJobObject 실패: {err}")
    return JobHandle(job)
