"""Children that die with the engine (P6.2b; the Phase 6 plan's decision 17; DECISIONS D-083).

A killed engine (the fire drill's ``Stop-Process``, or Task Scheduler's time limit) would leave a
``claude`` call running: uv's launcher lets the interpreter's own children break away from its job
object, so nothing would take them down. ``KillOnCloseJob`` is a Windows job object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. Its owner (the CLI transport, for the life of the engine)
assigns each ``claude`` child to it as it starts. When the owning process ends, however it ends,
Windows closes the job's last handle and every process in it is killed. The handle isn't
inheritable, so no child keeps the job alive.

Best effort: a child that starts a process of its own before it is assigned can leave that one
outside, and a failed assignment is logged, not raised. On other systems this does nothing.

From Microsoft's docs: ``CreateJobObjectW`` returns NULL on failure; ``SetInformationJobObject``
takes ``JobObjectExtendedLimitInformation`` (9) and a ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION``;
``AssignProcessToJobObject`` needs a process handle with ``PROCESS_SET_QUOTA`` and
``PROCESS_TERMINATE``, and since Windows 8 a process already in a job can join another (nested
jobs).
"""

import sys

import structlog

log = structlog.get_logger(__name__)

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # JOBOBJECTINFOCLASS
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class KillOnCloseJob:
    """A job object whose processes are killed when its last handle closes."""

    def __init__(self) -> None:
        self._handle = _create()

    @property
    def active(self) -> bool:
        return self._handle is not None

    def assign(self, pid: int) -> bool:
        """Put the process in the job; False if it couldn't be (logged)."""
        return self._handle is not None and _assign(self._handle, pid)

    def close(self) -> None:
        """Close the job now, killing every process in it (the engine never does: its exit does)."""
        handle, self._handle = self._handle, None
        if handle is not None:
            _close(handle)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in ("reads", "writes", "others", "read_bytes", "write_bytes", "other_bytes")
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_int64),  # LARGE_INTEGER
            ("per_job_user_time_limit", ctypes.c_int64),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),  # SIZE_T
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),  # ULONG_PTR
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", _BasicLimits),
            ("io", _IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _create() -> int | None:
        handle = _kernel32.CreateJobObjectW(None, None)  # no name; not inheritable
        if not handle:
            log.warning("job object not created", error=ctypes.get_last_error())
            return None
        limits = _ExtendedLimits()
        limits.basic.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            log.warning("job object limits not set", error=ctypes.get_last_error())
            _kernel32.CloseHandle(handle)
            return None
        return int(handle)

    def _assign(job: int, pid: int) -> bool:
        process = _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            log.warning("child not opened for its job", pid=pid, error=ctypes.get_last_error())
            return False
        try:
            if not _kernel32.AssignProcessToJobObject(job, process):
                log.warning("child not put in its job", pid=pid, error=ctypes.get_last_error())
                return False
            return True
        finally:
            _kernel32.CloseHandle(process)

    def _close(job: int) -> None:
        _kernel32.CloseHandle(job)

else:

    def _create() -> int | None:
        return None

    def _assign(job: int, pid: int) -> bool:
        return False

    def _close(job: int) -> None:
        return None
