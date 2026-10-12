"""One engine per runtime folder (DECISIONS D-048): ``run-day.lock``, held with an OS file lock.

Two engines trading the same accounts would double every order. The lock is the operating
system's, held for the life of the process and released by the OS however the process ends, so a
crash never leaves a stale lock and no PID check is needed (on Windows ``os.kill(pid, 0)`` would
terminate the process, not probe it).
"""

import os
import sys
from pathlib import Path
from types import TracebackType
from typing import Self


class RunLockError(RuntimeError):
    """Another engine holds the lock."""


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _lock(fd)
        except OSError:
            os.close(fd)
            raise RunLockError(
                f"another `trader run-day` is running ({self.path} is locked)"
            ) from None
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # raises OSError when another handle holds it

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises BlockingIOError when held

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
