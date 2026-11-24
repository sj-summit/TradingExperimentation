"""Disk space and drives (P6.3; DECISIONS D-084): doctor's disk line and run-day's ``disk_low``
share one threshold, and the backup copy folder must be on a local drive."""

import shutil
import sys
from pathlib import Path

MIN_FREE_BYTES = 2 * 1024**3  # doctor warns, and run-day raises a P2 disk_low, below this
DRIVE_REMOTE = 4  # GetDriveTypeW: a network drive


def free_bytes(path: Path) -> int:
    """Free space on the disk holding ``path``, or its nearest folder that exists."""
    for folder in (path, *path.parents):
        if folder.exists():
            return shutil.disk_usage(folder).free
    return shutil.disk_usage(Path.cwd()).free


def size_text(n: int) -> str:
    """Binary units: ``1 B``, ``12.5 KiB``, ``3.0 MiB``, ``1.5 GiB``."""
    if n < 1024:
        return f"{n} B"
    for unit, scale in (("KiB", 1024), ("MiB", 1024**2)):
        if n < scale * 1024:
            return f"{n / scale:.1f} {unit}"
    return f"{n / 1024**3:.1f} GiB"


def is_network_drive(path: Path) -> bool:
    """Whether ``path`` is on a network share or a mapped network drive. The scheduled tasks
    run under S4U, which reaches neither (D-080)."""
    text = str(path).replace("/", "\\")
    if text.startswith("\\\\"):
        return True
    if sys.platform != "win32" or not path.drive:
        return False
    import ctypes

    call = ctypes.windll.kernel32.GetDriveTypeW
    call.restype = ctypes.c_uint
    call.argtypes = [ctypes.c_wchar_p]
    return int(call(path.drive + "\\")) == DRIVE_REMOTE
