"""What every scheduled task runs: ``python -m trader.ops.scheduled --root <root> <command...>``
(P6.1a; the Phase 6 plan's decision 3; DECISIONS D-080).

- **Output first.** Under the scheduler nobody sees a console, so before trader's CLI is imported
  (``configure_logging`` binds ``sys.stderr`` when its module loads), file descriptors 1 and 2 and
  ``sys.stdout``/``sys.stderr`` go to ``runtime/logs/scheduled/<job>-<date>.log``, between a
  header (the arguments, the process id) and a footer (the exit code). A process with no console
  at all (``pythonw.exe``, the interactive logon) first gets a hidden one, so console children get
  one too and an interrupt can still reach them.
- **A failure** is a non-zero exit or an exception: ``cli._run`` turns most failures into an exit
  1, which no exception handler would see. It is journaled once per job per day as a P2
  ``scheduled_job_failed`` naming the log and its last error line, redacted, and pushed at once
  unless an engine is running to pump it.
- **run-day:** a session already DONE, or another engine holding the lock, is a quiet skip. A
  failure before BOOT is retried every 2 minutes until 09:20 ET. After BOOT it isn't: a crashed
  engine is not restarted automatically.
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import time as clock_time
from pathlib import Path
from subprocess import list2cmdline
from typing import Literal

# Nothing that configures logging may be imported before the output is redirected (above).
from trader.core.clock import Clock, RealClock, fmt_iso

LOGS = Path("runtime") / "logs" / "scheduled"
FAILED = "scheduled_job_failed"
RETRY_S = 120.0
RETRY_UNTIL = clock_time(9, 20)  # ET: the brief's last attempt starts at 09:27
TAIL_CHARS = 300
DayState = Literal["done", "locked", "booted", "not_booted"]
Invoke = Callable[[list[str]], int]


def job_name(args: Sequence[str]) -> str:
    """``run-day``, ``universe-build``, ``schedule-probe``...: the command's words, before its
    first option."""
    words: list[str] = []
    for arg in args:
        if arg.startswith("-"):
            break
        words.append(arg)
    return re.sub(r"[^a-z0-9-]", "_", "-".join(words).lower()) or "trader"


def split_root(args: Sequence[str]) -> tuple[Path, list[str]]:
    if len(args) < 3 or args[0] != "--root":
        raise SystemExit("usage: python -m trader.ops.scheduled --root <root> <command...>")
    return Path(args[1]).resolve(), list(args[2:])


def log_path(root: Path, args: Sequence[str], clock: Clock) -> Path:
    return root / LOGS / f"{job_name(args)}-{clock.now():%Y-%m-%d}.log"


# ---------------------------------------------------------------- the run


@dataclass(frozen=True)
class Scheduled:
    root: Path
    args: list[str]  # trader's arguments after --root
    clock: Clock
    invoke: Invoke
    sleep: Callable[[float], None]
    alert: Callable[[str, int], None]  # (job, exit code): journaled once a day, pushed
    day_state: Callable[[], DayState]  # run-day only

    @property
    def job(self) -> str:
        return job_name(self.args)

    def run(self) -> int:
        while True:
            code = self.invoke(["--root", str(self.root), *self.args])
            if code == 0:
                return 0
            state: DayState | None = None
            if self.job == "run-day":
                state = self.day_state()
                if state == "done":
                    say("run-day: this session already ran to the end; nothing to do")
                    return 0
                if state == "locked":
                    say("run-day: another run-day is running; this one stands down")
                    return 0
            self.alert(self.job, code)
            if state == "not_booted" and self.clock.now().time() < RETRY_UNTIL:
                say(f"run-day failed before BOOT: retrying in {RETRY_S:.0f} s")
                self.sleep(RETRY_S)
                continue
            return code


def invoke_cli(args: list[str]) -> int:
    """Run trader's CLI in this process and return its exit code, however it ends. Without
    standalone mode typer returns an Exit's code, and raises the rest."""
    import typer

    from trader.cli import app

    try:
        result = app(args=args, prog_name="trader", standalone_mode=False)
    except typer.Abort:
        say("aborted", err=True)
        return 1
    except typer.TyperException as exc:  # a usage error and the like
        say(f"Error: {exc.format_message()}", err=True)
        return exc.exit_code or 1
    except SystemExit as exc:
        return _code(exc.code)
    except Exception:
        traceback.print_exc()
        return 1
    return _code(result)


def _code(value: object) -> int:
    if value is None:
        return 0
    return value if isinstance(value, int) else 1


# ---------------------------------------------------------------- run-day's state


def day_state(root: Path, clock: Clock) -> DayState:
    """Where today's session stands, read with sqlite3 alone (the engine may hold the journal)."""
    from trader.config import ConfigError, load_config
    from trader.orchestrator.lock import LOCK_NAME, RunLock, RunLockError

    try:
        cfg = load_config(root)
    except ConfigError:
        return "not_booted"
    db = cfg.path(cfg.settings.storage.db_path)
    today = clock.now().date().isoformat()
    status, booted = None, False
    if db.exists():
        conn = sqlite3.connect(db)
        try:
            conn.execute("PRAGMA query_only = 1")
            row = conn.execute(
                "SELECT run_status FROM trading_days WHERE date = ?", (today,)
            ).fetchone()
            status = row[0] if row else None
            booted = (
                conn.execute(
                    "SELECT 1 FROM day_runs WHERE date = ? AND kind = 'state' AND name = 'BOOT'",
                    (today,),
                ).fetchone()
                is not None
            )
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    if status == "DONE":
        return "done"
    lock = RunLock(db.parent / LOCK_NAME)
    try:
        lock.acquire()  # only after our own run-day failed: no engine of ours is starting
    except RunLockError:
        return "locked"
    lock.release()
    return "booted" if booted else "not_booted"


# ---------------------------------------------------------------- the alert


class JournalAlert:
    """One P2 ``scheduled_job_failed`` per job per day, pushed at once unless an engine is
    running: then its own pump sends it, so two pumps never race."""

    def __init__(self, root: Path, clock: Clock, log: Path) -> None:
        self._root, self._clock, self._log = root, clock, log

    def __call__(self, job: str, code: int) -> None:
        try:
            asyncio.run(self._alert(job, code))
        except Exception:  # the job's own failure is already in the log; so is this
            say("could not journal or push the failure:", err=True)
            traceback.print_exc()

    async def _alert(self, job: str, code: int) -> None:
        from trader.config import load_config
        from trader.core.redact import redact_text
        from trader.memory.incidents import record_incident
        from trader.memory.journal import Journal
        from trader.notify import notifier_from_config, pump_alerts
        from trader.orchestrator.heartbeat import heartbeat_path, read_heartbeat

        cfg = load_config(self._root)
        db = cfg.path(cfg.settings.storage.db_path)
        now = self._clock.now()
        last = redact_text(last_error(self._log), cfg.secrets.redaction_map())
        where = self._log.relative_to(self._root).as_posix()
        async with await Journal.open(db) as journal:
            rows = await journal.fetch_all(
                "SELECT detail_json FROM incidents WHERE code = ? AND ts LIKE ?",
                (FAILED, f"{now.date().isoformat()}T%"),
            )
            if not any(json.loads(r["detail_json"] or "{}").get("job") == job for r in rows):
                await record_incident(
                    journal,
                    at=now,
                    severity="P2",
                    component="scheduler",
                    code=FAILED,
                    message=f"scheduled {job} failed (exit {code}): {last or 'no error line'}; "
                    f"see {where}",
                    detail={"job": job, "exit": code, "log": where},
                )
            if not read_heartbeat(heartbeat_path(db), self._clock.now()).fresh:
                await pump_alerts(journal, notifier_from_config(cfg), self._clock)


def last_error(log: Path) -> str:
    """The log's last line that isn't the wrapper's own, cut short."""
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        text = line.strip()
        if text and not text.startswith("== ") and not text.startswith("run-day failed before"):
            return text[:TAIL_CHARS]
    return ""


# ---------------------------------------------------------------- the process


def say(text: str, *, err: bool = False) -> None:
    """One line to the log (the redirected stdout, or stderr), flushed at once."""
    stream = sys.stderr if err else sys.stdout
    if stream is not None:  # pythonw before the redirect
        stream.write(text + "\n")
        stream.flush()


@contextmanager
def output_to(log: Path) -> Iterator[None]:
    """Send this process's output, its children's included, to ``log``."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8", errors="backslashreplace", buffering=1) as stream:
        saved = [_dup(fd) for fd in (1, 2)]
        old = sys.stdout, sys.stderr
        try:
            for fd in (1, 2):
                os.dup2(stream.fileno(), fd)
            _std_handles()
            sys.stdout = sys.stderr = stream
            yield
        finally:
            with suppress(Exception):
                stream.flush()
            sys.stdout, sys.stderr = old
            for fd, copy in zip((1, 2), saved, strict=True):
                if copy is not None:
                    os.dup2(copy, fd)
                    os.close(copy)
            _std_handles()


def _dup(fd: int) -> int | None:
    try:
        return os.dup(fd)
    except OSError:  # pythonw: no stdout or stderr at all
        return None


def _std_handles() -> None:
    """Point Windows' standard handles at file descriptors 1 and 2, for child processes."""
    if sys.platform != "win32":
        return
    import ctypes
    import msvcrt

    kernel32 = ctypes.windll.kernel32
    for fd, which in ((1, -11), (2, -12)):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
        with suppress(OSError):
            kernel32.SetStdHandle(which, msvcrt.get_osfhandle(fd))


def hidden_console() -> None:
    """pythonw has no console: give it a hidden one, so console children (claude, taskkill) don't
    each open a window, and CTRL_BREAK can reach them."""
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    if kernel32.GetConsoleWindow():
        return
    if kernel32.AllocConsole():
        window = kernel32.GetConsoleWindow()
        if window:
            ctypes.windll.user32.ShowWindow(window, 0)  # SW_HIDE


def main(argv: Sequence[str] | None = None) -> int:
    root, args = split_root(list(sys.argv[1:] if argv is None else argv))
    clock = RealClock()
    log = log_path(root, args, clock)
    hidden_console()  # before the redirect: a new console resets the standard handles
    with output_to(log):
        say(f"== {fmt_iso(clock.now())} start pid {os.getpid()}: trader {list2cmdline(args)}")
        try:
            code = Scheduled(
                root=root,
                args=args,
                clock=clock,
                invoke=invoke_cli,
                sleep=time.sleep,
                alert=JournalAlert(root, clock, log),
                day_state=lambda: day_state(root, clock),
            ).run()
        except BaseException:  # the wrapper itself must never die without a word
            traceback.print_exc()
            code = 1
        say(f"== {fmt_iso(clock.now())} exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
