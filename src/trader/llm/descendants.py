"""The processes a child starts, and which of them outlive it (``trader smoke llm --kill-test``).

D-064's residual: without a Job Object, a process the CLI starts can outlive the CLI's stop.
``DescendantWatch`` finds out whether the real CLI leaves one.

- **Windows:** while the child runs, the watch lists the system's processes (Toolhelp32) and
  keeps the child's descendants: those linked to it by their parent ids that weren't running
  when the watch began. A process that already ran can't be the child's, even when its parent id
  names a dead process whose id the child got. After the stop, it reports the descendants still
  running under the same name.
- **POSIX:** the child leads its own process group (``start_new_session``), and its descendants
  stay in it unless they start their own session. After the stop, the watch reports whether the
  group still has a member.
"""

import asyncio
import contextlib
import os
import sys
from collections.abc import Callable, Iterable

Listing = Callable[[], list[tuple[int, int, str]]]  # (pid, parent pid, executable name)

POLL_S = 0.2


class DescendantWatch:
    """Pass ``started`` as the transport's ``on_spawn``; call ``survivors`` once it returns."""

    def __init__(self, *, listing: Listing | None = None, poll_s: float = POLL_S) -> None:
        self._listing = listing if listing is not None else processes
        self._poll_s = poll_s
        self._before = frozenset(pid for pid, _, _ in self._list())
        self._task: asyncio.Task[None] | None = None
        self.root: int | None = None
        self.seen: dict[int, str] = {}  # the descendants seen, by pid (from the listing)

    def started(self, proc: asyncio.subprocess.Process) -> None:
        self.root = proc.pid
        self._task = asyncio.get_running_loop().create_task(self._follow(proc))

    async def _follow(self, proc: asyncio.subprocess.Process) -> None:
        while True:
            self.look()
            if proc.returncode is not None:
                return
            await asyncio.sleep(self._poll_s)

    def look(self) -> None:
        """Add the root's descendants running now to ``seen``."""
        if self.root is not None:
            rows = [row for row in self._list() if row[0] not in self._before]
            self.seen.update(descendants(self.root, rows))

    def survivors(self) -> list[str]:
        """What the child started that still runs, as ``name (pid)``."""
        if self._task is not None:
            self._task.cancel()
        if self.root is None:
            return []
        self.look()
        running = {pid: name for pid, _, name in self._list()}
        left = [
            f"{name} ({pid})" for pid, name in sorted(self.seen.items()) if running.get(pid) == name
        ]
        if not left and group_alive(self.root):
            left.append(f"process group {self.root}")
        return left

    def _list(self) -> list[tuple[int, int, str]]:
        try:
            return self._listing()
        except OSError:
            return []


def descendants(root: int, rows: Iterable[tuple[int, int, str]]) -> dict[int, str]:
    """``root``'s descendants among ``rows`` (pid, parent pid, name), by their parent links. A
    link back to ``root`` or to one already found (the idle process is its own parent) ends."""
    children: dict[int, list[tuple[int, str]]] = {}
    for pid, parent, name in rows:
        children.setdefault(parent, []).append((pid, name))
    found: dict[int, str] = {}
    queue = [root]
    while queue:
        for pid, name in children.get(queue.pop(), []):
            if pid != root and pid not in found:
                found[pid] = name
                queue.append(pid)
    return found


def group_alive(pgid: int) -> bool:
    """Whether process group ``pgid`` still has a member (POSIX; always False on Windows)."""
    if sys.platform == "win32":
        return False
    else:  # an else, so mypy checks only this platform's branch
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # a member we may not signal still exists
            return True
        return True


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _SNAPPROCESS = 0x00000002  # TH32CS_SNAPPROCESS
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    class _ProcessEntry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    def processes() -> list[tuple[int, int, str]]:
        """Every process now: (pid, parent pid, executable name)."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        snapshot = kernel32.CreateToolhelp32Snapshot
        snapshot.restype = wintypes.HANDLE
        snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        first, following = kernel32.Process32FirstW, kernel32.Process32NextW
        for step in (first, following):
            step.restype = wintypes.BOOL
            step.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = snapshot(_SNAPPROCESS, 0)
        if handle is None or handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(_ProcessEntry)
            found: list[tuple[int, int, str]] = []
            more = first(handle, ctypes.byref(entry))
            while more:
                found.append(
                    (int(entry.th32ProcessID), int(entry.th32ParentProcessID), str(entry.szExeFile))
                )
                more = following(handle, ctypes.byref(entry))
            return found
        finally:
            with contextlib.suppress(OSError):
                kernel32.CloseHandle(handle)

else:

    def processes() -> list[tuple[int, int, str]]:
        """POSIX doesn't list them: the process group answers instead."""
        return []
