"""``trader schedule show|install|uninstall|check|probe`` (P6.1a; DECISIONS D-080).

- **show** prints the tasks (in ET and on the PC's clock, with their command lines and logon),
  whether each is installed as shown (read back with ``schtasks /Query /XML``), and anything
  that would make install refuse.
- **install** (HUMAN GATE G6a) refuses a time zone the schedule can't follow, notifications
  switched off (a P1 at night must reach someone, PLAN H0.5), or a missing virtual environment.
  Otherwise it prints everything, asks for ``INSTALL``, writes ``runtime/schedule/<task>.xml``,
  registers each task and records ``runtime/schedule.json``. A task an earlier install made and
  this one doesn't (the universe job) is removed.
- **uninstall** deletes the tasks it knows by name, after a yes.
- **check** proves the tasks can do their job where they run: it registers a one-off task with the
  same account and action, starts it, waits for its log, prints it, and always deletes the task.
- **probe** is what that task runs: doctor, then a test push. It exits 1 on any FAIL.
"""

import asyncio
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from trader.config import AppConfig
from trader.core.clock import Clock, fmt_iso
from trader.ops.scheduled import log_path
from trader.ops.scheduler import (
    FOLDER,
    NAMES,
    SATURDAY,
    WEEKDAYS,
    Logon,
    ScheduleError,
    Schtasks,
    Target,
    TaskEntry,
    differences,
    entries,
    local_time,
    probe_entry,
    task_xml,
    write_xml,
    zone_shift,
)

if TYPE_CHECKING:
    from trader.memory.unattended import SessionRun

SCHEDULE_DIR = Path("runtime") / "schedule"
SCHEDULE_FILE = Path("runtime") / "schedule.json"
PROBE_POLL_S = 2.0
PROBE_WAIT_S = 180.0
FOOTER = re.compile(r"^== \S+ exit (-?\d+)\s*$")
ELEVATED = (
    "If it says access is denied, run it again from an elevated PowerShell (Run as administrator)."
)


@dataclass(frozen=True)
class ScheduleDeps:
    schtasks: Schtasks
    zone: Callable[[], str]  # Windows' time zone id (tzutil /g)
    user: Callable[[], str]  # DOMAIN\name
    clock: Clock
    echo: Callable[[str], None]
    prompt: Callable[[str], str]
    wait: Callable[[float], None]  # check's polling


def default_deps() -> ScheduleDeps:
    import time

    import typer

    from trader.core.clock import RealClock
    from trader.ops.scheduler import current_user, windows_zone

    return ScheduleDeps(
        schtasks=Schtasks(),
        zone=windows_zone,
        user=current_user,
        clock=RealClock(),
        echo=typer.echo,
        prompt=lambda text: str(typer.prompt(text)),
        wait=time.sleep,
    )


def installed(root: Path) -> dict[str, object] | None:
    """``runtime/schedule.json``, the record of the last install, if any."""
    path = root / SCHEDULE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _logon(root: Path, asked: Logon | None) -> Logon:
    if asked is not None:
        return asked
    record = installed(root) or {}
    return "interactive" if record.get("logon") == "interactive" else "s4u"


def _shift(deps: ScheduleDeps) -> tuple[str, timedelta, str | None]:
    """The zone, its offset from ET, and why install would refuse it."""
    try:
        zone = deps.zone()
        return zone, zone_shift(zone), None
    except ScheduleError as exc:
        return "unknown", timedelta(0), str(exc)


def refusals(cfg: AppConfig, target: Target, zone_problem: str | None) -> list[str]:
    """Every reason install would refuse."""
    out = [zone_problem] if zone_problem else []
    notify = cfg.settings.notify
    if notify.provider == "none":
        out.append(
            "notifications are off (notify.provider: none), so a P1 at night would reach nobody: "
            "set up ntfy or Discord first (PLAN H0.5), then `trader notify test`"
        )
    else:
        env = notify.ntfy_topic_env if notify.provider == "ntfy" else notify.discord_webhook_env
        if not cfg.secrets.is_set(env):
            out.append(f"notify.provider is {notify.provider}, but {env} isn't set in .env")
    if not target.python.exists():
        out.append(f"{target.python} doesn't exist: run `uv sync` first")
    return out


def _days(entry: TaskEntry) -> str:
    if entry.days == WEEKDAYS:
        return "Mon-Fri"
    if entry.days == SATURDAY:
        return "Sat"
    return ",".join(day[:3] for day in entry.days) or "on demand"


def when(entry: TaskEntry, shift: timedelta) -> str:
    """``Mon-Fri 09:25-16:05 ET, every 1 min`` (with the PC's clock when it isn't ET)."""
    text = f"{_days(entry)} {entry.at:%H:%M}"
    if entry.until is not None:
        text += f"-{entry.until:%H:%M}"
    text += " ET"
    if shift:
        local = f"{local_time(entry.at, shift):%H:%M}"
        if entry.until is not None:
            local += f"-{local_time(entry.until, shift):%H:%M}"
        text += f" ({local} on this PC)"
    if entry.every is not None:
        text += f", every {int(entry.every.total_seconds() // 60)} min"
    return text


def _settings(entry: TaskEntry) -> str:
    hours, minutes = divmod(int(entry.time_limit.total_seconds() // 60), 60)
    limit = f"{hours} h" if hours and not minutes else f"{hours * 60 + minutes} min"
    parts = [f"stopped after {limit}"]
    if entry.wake:
        parts.append("wakes the PC")
    parts.append("normal priority" if entry.priority <= 6 else "below-normal priority")
    return ", ".join(parts)


def summary(
    cfg: AppConfig, target: Target, zone: str, shift: timedelta, jobs: tuple[TaskEntry, ...]
) -> list[str]:
    how = (
        "whether you're logged on or not (S4U; no password stored)"
        if target.logon == "s4u"
        else "only while you're logged on (interactive)"
    )
    lines = [
        f"Windows Task Scheduler, folder {FOLDER}",
        f"Time zone: {zone} ({'the PC clock is ET' if not shift else f'ET {shift_text(shift)}'})",
        f"Runs as {target.user}, {how}",
        f"Each task runs: {target.python} -m trader.ops.scheduled --root {target.root} <command>",
        f"Output: {Path('runtime') / 'logs' / 'scheduled'}\\<task>-<date>.log",
    ]
    for entry in jobs:
        lines.append(f"  {entry.name:<11} {when(entry, shift)}")
        lines.append(f"  {'':<11} trader {' '.join(entry.command)}; {_settings(entry)}")
    if cfg.settings.mode != "paper":
        lines.append("Note: mode is not paper; run-day refuses live trading without R§7's gate.")
    return lines


def shift_text(shift: timedelta) -> str:
    hours = int(shift.total_seconds() // 3600)
    return f"{hours:+d} h"


def _state(deps: ScheduleDeps, entry: TaskEntry, xml: str) -> str:
    found = deps.schtasks.query_xml(entry.path)
    if found is None:
        return "not installed"
    try:
        diff = differences(xml, found)
    except Exception:  # unreadable output: say so rather than guess
        return "installed (its definition couldn't be read back)"
    return "installed, as shown" if not diff else "installed, but differs: " + "; ".join(diff)


# ---------------------------------------------------------------- the commands


def show(
    root: Path, cfg: AppConfig, deps: ScheduleDeps, *, logon: Logon | None, with_universe: bool
) -> int:
    target = Target(root=root, user=deps.user(), logon=_logon(root, logon))
    zone, shift, zone_problem = _shift(deps)
    jobs = entries(cfg.settings, with_universe=with_universe)
    for line in summary(cfg, target, zone, shift, jobs):
        deps.echo(line)
    first_day = deps.clock.now().date() + timedelta(days=1)
    deps.echo("Installed:")
    for entry in jobs:
        xml = task_xml(entry, target, shift=shift, first_day=first_day)
        deps.echo(f"  {entry.name:<11} {_state(deps, entry, xml)}")
    recent = recent_sessions(cfg.path(cfg.settings.storage.db_path), deps.clock)
    if recent:
        deps.echo("Recent sessions (S§8 criterion 4 counts unattended ones without a P1):")
        for run in recent:
            how = "unattended" if run.unattended else f"attended: {run.why_not}"
            deps.echo(f"  {run.day}  {how}" + (f"; {run.p1} P1" if run.p1 else ""))
    problems = refusals(cfg, target, zone_problem)
    if problems:
        deps.echo("install would refuse:")
        for problem in problems:
            deps.echo(f"  - {problem}")
    return 0


def recent_sessions(db: Path, clock: Clock, count: int = 5) -> list["SessionRun"]:
    """The last ``count`` sessions through today, unattended or why not (D-081). Read with
    sqlite3 alone: an engine may hold the journal."""
    from trader.memory.unattended import session_runs

    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA query_only = 1")
        rows = conn.execute(
            "SELECT date FROM trading_days WHERE date <= ? ORDER BY date DESC LIMIT ?",
            (clock.now().date().isoformat(), count),
        ).fetchall()
        return session_runs(conn, sorted(date.fromisoformat(r[0]) for r in rows))
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def install(
    root: Path, cfg: AppConfig, deps: ScheduleDeps, *, logon: Logon, with_universe: bool
) -> int:
    target = Target(root=root, user=deps.user(), logon=logon)
    zone, shift, zone_problem = _shift(deps)
    problems = refusals(cfg, target, zone_problem)
    if problems:
        for problem in problems:
            deps.echo(f"cannot install: {problem}")
        return 1
    jobs = entries(cfg.settings, with_universe=with_universe)
    for line in summary(cfg, target, zone, shift, jobs):
        deps.echo(line)
    typed = deps.prompt(f"Type INSTALL to register these {len(jobs)} tasks")
    if typed.strip() != "INSTALL":
        deps.echo("nothing installed")
        return 1
    first_day = deps.clock.now().date() + timedelta(days=1)  # nothing fires the day it's installed
    hashes: dict[str, str] = {}
    for entry in jobs:
        xml = task_xml(entry, target, shift=shift, first_day=first_day)
        file = root / SCHEDULE_DIR / f"{entry.name}.xml"
        write_xml(xml, file)
        out = deps.schtasks.create(entry.path, file)
        if out.code != 0:
            deps.echo(f"schtasks refused {entry.path} (exit {out.code}): {_said(out)}")
            deps.echo(ELEVATED)
            if hashes:
                deps.echo(
                    f"registered before it: {', '.join(hashes)}; `trader schedule uninstall` "
                    "removes them"
                )
            return 1
        hashes[entry.path] = hashlib.sha256(xml.encode("utf-8")).hexdigest()
    for name in NAMES:  # a task an earlier install made that this one doesn't
        path = FOLDER + name
        if name != "probe" and path not in hashes and deps.schtasks.query_xml(path) is not None:
            out = deps.schtasks.delete(path)
            deps.echo(f"removed {path}: no longer scheduled" if out.code == 0 else _said(out))
    record = {
        "installed_at": fmt_iso(deps.clock.now()),
        "logon": logon,
        "user": target.user,
        "zone": zone,
        "root": str(root),
        "python": str(target.python),
        "tasks": hashes,
    }
    (root / SCHEDULE_FILE).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    deps.echo(f"registered {len(hashes)} tasks in {FOLDER}. Next: `trader schedule check` (G6a).")
    return 0


def uninstall(root: Path, deps: ScheduleDeps) -> int:
    present = [FOLDER + n for n in NAMES if deps.schtasks.query_xml(FOLDER + n) is not None]
    record = root / SCHEDULE_FILE
    if not present:
        deps.echo("no ai-day-trader tasks are installed")
        record.unlink(missing_ok=True)
        return 0
    for path in present:
        deps.echo(f"  {path}")
    answer = deps.prompt(f"Delete these {len(present)} tasks? [y/N]")
    if answer.strip().lower() not in ("y", "yes"):
        deps.echo("nothing deleted")
        return 1
    failed = []
    for path in present:
        out = deps.schtasks.delete(path)
        if out.code != 0:
            failed.append(path)
            deps.echo(f"schtasks couldn't delete {path} (exit {out.code}): {_said(out)}")
    if failed:
        deps.echo(ELEVATED)
        return 1
    record.unlink(missing_ok=True)
    deps.echo(f"deleted {len(present)} tasks")
    return 0


def check(root: Path, deps: ScheduleDeps, *, logon: Logon | None) -> int:
    target = Target(root=root, user=deps.user(), logon=_logon(root, logon))
    if not target.python.exists():
        deps.echo(f"cannot check: {target.python} doesn't exist (run `uv sync` first)")
        return 1
    entry = probe_entry()
    xml = task_xml(entry, target, shift=timedelta(0), first_day=deps.clock.now().date())
    file = root / SCHEDULE_DIR / "probe.xml"
    write_xml(xml, file)
    log = log_path(root, entry.command, deps.clock)
    log.unlink(missing_ok=True)
    out = deps.schtasks.create(entry.path, file)
    if out.code != 0:
        deps.echo(f"schtasks refused the probe (exit {out.code}): {_said(out)}")
        deps.echo(ELEVATED)
        return 1
    try:
        out = deps.schtasks.start(entry.path)
        if out.code != 0:
            deps.echo(f"schtasks couldn't start the probe (exit {out.code}): {_said(out)}")
            return 1
        deps.echo(
            f"The probe runs `trader schedule probe` as {target.user} ({target.logon_type}); "
            f"waiting up to {PROBE_WAIT_S:.0f} s for {log.relative_to(root)}"
        )
        code = _await_footer(log, deps)
        if log.exists():
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                deps.echo(line)
        if code is None:
            deps.echo(f"CHECK FAILED: the probe didn't finish within {PROBE_WAIT_S:.0f} s")
            return 1
        deps.echo("CHECK PASSED" if code == 0 else f"CHECK FAILED: the probe exited {code}")
        return 0 if code == 0 else 1
    finally:
        out = deps.schtasks.delete(entry.path)
        if out.code != 0:
            deps.echo(f"couldn't delete {entry.path}: delete it in Task Scheduler ({_said(out)})")


def _await_footer(log: Path, deps: ScheduleDeps) -> int | None:
    """The probe's exit code, from the wrapper's footer line, or None if it never came."""
    for _ in range(int(PROBE_WAIT_S / PROBE_POLL_S) + 1):
        if log.exists():
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                found = FOOTER.match(line)
                if found:
                    return int(found[1])
        deps.wait(PROBE_POLL_S)
    return None


def _said(out: object) -> str:
    text = (getattr(out, "err", "") or getattr(out, "out", "") or "").strip()
    return text or "no message"


# ---------------------------------------------------------------- the probe


def probe(root: Path, echo: Callable[[str], None]) -> int:
    """Doctor and a test push, from wherever this runs (inside the scheduler, for check)."""
    from trader.doctor import default_deps, format_report, run_doctor

    echo("-- doctor")
    checks = run_doctor(root, default_deps())
    for line in format_report(checks):
        echo(line)
    failed = [check.name for check in checks if check.status == "FAIL"]
    echo("-- a test push")
    pushed = asyncio.run(_test_push(root, echo))
    if not pushed:
        failed.append("notify")
    echo(f"PROBE FAILED: {', '.join(failed)}" if failed else "PROBE PASSED")
    return 1 if failed else 0


async def _test_push(root: Path, echo: Callable[[str], None]) -> bool:
    from trader.notify import NullNotifier, notifier_from_config
    from trader.runtime import open_local

    async with open_local(root) as local:
        notifier = notifier_from_config(local.cfg)
        if isinstance(notifier, NullNotifier):
            echo("notify.provider is none, or its secret is missing: nothing sent")
            return False
        sent = await notifier.send(
            "trader schedule check",
            "If you can read this, alerts from the scheduled jobs reach you.",
            tags=("test_tube",),
        )
        echo(f"sent through {local.cfg.settings.notify.provider}" if sent else "the push failed")
        return sent
