"""The PC's sleep: kept awake while run-day runs, and its settings read for doctor (RISK.md §9;
the Phase 6 plan's decision 10; DECISIONS D-081).

- **keep_awake()** holds ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`` for
  run-day's life, on the thread that runs the event loop, then clears it with ``ES_CONTINUOUS``
  alone. The display may still sleep. It can't override a person's own sleep request (the
  lid, the power button), Microsoft's docs say. This PC sleeps in S3, not Modern Standby.
- **sleep_settings()** reads the active power scheme's AC values through powrprof: the standby
  timeout in seconds (0 means never) and whether wake timers are allowed (0 off, 1 on, 2 only
  important ones), the values ``powercfg /query`` shows. None where they can't be read.
"""

import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
GUID_SLEEP_SUBGROUP = "238c9fa8-0aad-41ed-83f4-97be242c8f20"  # powercfg's SUB_SLEEP
GUID_STANDBY_TIMEOUT = "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"  # STANDBYIDLE
GUID_ALLOW_RTC_WAKE = "bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d"  # RTCWAKE


def set_execution_state(flags: int) -> int:
    """``SetThreadExecutionState``: the previous state, 0 when it failed or isn't Windows."""
    if sys.platform != "win32":
        return 0
    import ctypes

    call = ctypes.windll.kernel32.SetThreadExecutionState
    call.restype = ctypes.c_uint32
    call.argtypes = [ctypes.c_uint32]
    return int(call(flags))


@contextmanager
def keep_awake() -> Iterator[bool]:
    """Keep the PC from falling asleep while the block runs; yields whether Windows agreed."""
    held = set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) != 0
    try:
        yield held
    finally:
        if held:
            set_execution_state(ES_CONTINUOUS)


@dataclass(frozen=True)
class SleepSettings:
    standby_s: int  # on AC power; 0 means never
    wake_timers: int  # 0 disabled, 1 enabled, 2 important wake timers only

    @property
    def sleeps(self) -> bool:
        return self.standby_s > 0

    @property
    def wakes(self) -> bool:
        return self.wake_timers == 1


def sleep_settings() -> SleepSettings | None:
    """The active power scheme's AC sleep settings, or None where they can't be read."""
    if sys.platform != "win32":
        return None
    import ctypes

    class Guid(ctypes.Structure):
        _fields_ = [
            ("data1", ctypes.c_uint32),
            ("data2", ctypes.c_uint16),
            ("data3", ctypes.c_uint16),
            ("data4", ctypes.c_ubyte * 8),
        ]

    def guid(text: str) -> Guid:
        return Guid.from_buffer_copy(uuid.UUID(text).bytes_le)  # Windows' GUID layout

    powrprof = ctypes.windll.powrprof
    scheme = ctypes.POINTER(Guid)()
    if powrprof.PowerGetActiveScheme(None, ctypes.byref(scheme)) != 0:
        return None
    try:
        subgroup = guid(GUID_SLEEP_SUBGROUP)

        def read(setting: str) -> int | None:
            value = ctypes.c_uint32()
            key = guid(setting)
            code = powrprof.PowerReadACValueIndex(
                None, scheme, ctypes.byref(subgroup), ctypes.byref(key), ctypes.byref(value)
            )
            return None if code != 0 else int(value.value)

        standby, wake = read(GUID_STANDBY_TIMEOUT), read(GUID_ALLOW_RTC_WAKE)
    finally:
        ctypes.windll.kernel32.LocalFree(scheme)
    if standby is None or wake is None:
        return None
    return SleepSettings(standby_s=standby, wake_timers=wake)
