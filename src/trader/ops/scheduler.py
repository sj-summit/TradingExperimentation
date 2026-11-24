"""Windows Task Scheduler entries (P6.1a; the Phase 6 plan's decisions 1-4; DECISIONS D-080).

- **The jobs** (A§1), in ET:
  - ``run-day`` five minutes before ``schedule.boot`` (08:25) on weekdays;
  - ``watchdog`` every minute from five minutes before the regular open to five after the close
    (09:25-16:05) on weekdays: RISK.md §6, every minute by the human's decision of 2026-11-23;
  - ``weekly-job`` on Saturdays at 10:00;
  - ``universe build`` at 20:00 on weekdays, only when asked for.

  Each job reads the trading calendar itself, so holidays and early closes need no entries.
- **Time zones.** Task Scheduler fires on the local wall clock, so the entries follow DST by
  themselves when the PC's zone keeps a constant offset to ET: Windows' US Eastern, Central,
  Mountain, Pacific and Alaskan zones. Any other zone, or one with daylight saving switched off
  (``tzutil``'s ``_dstoff``), is refused: Windows only, by the human's decision 3.
- **Each task** runs ``<root>\\.venv\\Scripts\\python.exe -m trader.ops.scheduled --root <root>
  <command>`` under S4U, whether or not anyone is logged on; with ``--logon interactive``,
  ``pythonw.exe`` while the human is. Every task: IgnoreNew, the battery conditions off,
  RunOnlyIfNetworkAvailable off, LeastPrivilege, and a StartBoundary in local time without an
  offset, so the trigger moves with the wall clock.
- **schtasks** registers them, always by its full System32 path: ``/Create /XML`` with a UTF-16
  file (the encoding Task Scheduler exports), ``/Query /XML`` to read one back (XML isn't
  localized; schtasks' own messages are), ``/Delete`` and ``/Run``.
"""

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from trader.config import Settings
from trader.core.clock import REGULAR_CLOSE, REGULAR_OPEN

Logon = Literal["s4u", "interactive"]

FOLDER = "\\ai-day-trader\\"
NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
WRAPPER = "trader.ops.scheduled"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
SATURDAY = ("Saturday",)
NAMES = ("run-day", "watchdog", "weekly-job", "universe", "probe")  # every task we may create
RUN_DAY_LEAD = timedelta(minutes=5)  # A§3: the process starts at 08:25, boot is at 08:30
WATCHDOG_MARGIN = timedelta(minutes=5)  # RISK.md §6: 09:25 to 16:05
WATCHDOG_EVERY = timedelta(minutes=1)
PRIORITY_NORMAL = 4  # 4-6 are NORMAL_PRIORITY_CLASS; Task Scheduler's default, 7, is below normal
PRIORITY_LOW = 7
# Windows zone id -> hours from ET to its wall clock, constant all year: these zones change their
# clocks on the same dates as US Eastern ("US Mountain" is Arizona and "Hawaiian" Hawaii, which
# keep standard time, so they are not here).
ZONE_SHIFT_H: Mapping[str, int] = {
    "Eastern Standard Time": 0,
    "Central Standard Time": -1,
    "Mountain Standard Time": -2,
    "Pacific Standard Time": -3,
    "Alaskan Standard Time": -4,
}
DST_OFF = "_dstoff"
# Task Scheduler's own value when an element is left out (its schema), for comparing tasks
DEFAULTS: Mapping[str, str] = {
    "instances": "IgnoreNew",
    "on_batteries": "true/true",
    "catch_up": "false",
    "network": "false",
    "wake": "false",
    "time_limit": "PT72H",
    "priority": "7",
    "enabled": "true",
    "run_level": "LeastPrivilege",
}


class ScheduleError(RuntimeError):
    """The schedule can't be built or installed as asked."""


@dataclass(frozen=True)
class TaskEntry:
    name: str
    description: str
    days: tuple[str, ...]  # XML day names; empty for a task that only runs on demand
    at: time  # ET
    command: tuple[str, ...]  # trader's arguments after --root
    time_limit: timedelta
    priority: int
    wake: bool
    every: timedelta | None = None  # repeated this often...
    until: time | None = None  # ...through this time (ET), inclusive

    @property
    def path(self) -> str:
        return FOLDER + self.name

    @property
    def span(self) -> timedelta | None:
        if self.until is None:
            return None
        day = date(2000, 1, 3)
        return datetime.combine(day, self.until) - datetime.combine(day, self.at)


def entries(settings: Settings, *, with_universe: bool = False) -> tuple[TaskEntry, ...]:
    """The scheduled jobs, in ET."""
    jobs = [
        TaskEntry(
            name="run-day",
            description="The trading day, from boot to the report (trader run-day).",
            days=WEEKDAYS,
            at=_shifted(settings.schedule.boot, -RUN_DAY_LEAD),
            command=("run-day", "--books", "A,B", "--scheduled"),
            time_limit=timedelta(hours=10),
            priority=PRIORITY_NORMAL,
            wake=True,
        ),
        TaskEntry(
            name="watchdog",
            description="The independent safety check, every minute (RISK.md section 6).",
            days=WEEKDAYS,
            at=_shifted(REGULAR_OPEN, -WATCHDOG_MARGIN),
            command=("watchdog",),
            time_limit=timedelta(minutes=4),
            priority=PRIORITY_NORMAL,
            wake=True,
            every=WATCHDOG_EVERY,
            until=_shifted(REGULAR_CLOSE, WATCHDOG_MARGIN),
        ),
        TaskEntry(
            name="weekly-job",
            description="Saturday: trader lessons evaluate --promote, then trader report weekly.",
            days=SATURDAY,
            at=time(10, 0),
            command=("weekly-job",),
            time_limit=timedelta(hours=1),
            priority=PRIORITY_LOW,
            wake=False,
        ),
    ]
    if with_universe:
        jobs.append(
            TaskEntry(
                name="universe",
                description="The next session's universe, built the evening before.",
                days=WEEKDAYS,
                at=time(20, 0),
                command=("universe", "build"),
                time_limit=timedelta(hours=1),
                priority=PRIORITY_LOW,
                wake=False,
            )
        )
    return tuple(jobs)


def probe_entry() -> TaskEntry:
    """`trader schedule check`'s one-off task: no trigger, started by hand and then deleted."""
    return TaskEntry(
        name="probe",
        description="trader schedule check: doctor and a test push, run by the scheduler.",
        days=(),
        at=time(0, 0),
        command=("schedule", "probe"),
        time_limit=timedelta(minutes=5),
        priority=PRIORITY_NORMAL,
        wake=False,
    )


# ---------------------------------------------------------------- time zones


def zone_shift(zone: str) -> timedelta:
    """How far the PC's wall clock is from ET, all year round.

    Raises ScheduleError for a zone whose offset to ET changes during the year."""
    name = zone.strip()
    if name.endswith(DST_OFF):
        raise ScheduleError(
            f"the PC's time zone ({name.removesuffix(DST_OFF)}) has daylight saving time switched "
            "off, so its clock and ET would drift an hour apart for half the year: turn on "
            "'Adjust for daylight saving time automatically' in Windows' date and time settings"
        )
    hours = ZONE_SHIFT_H.get(name)
    if hours is None:
        raise ScheduleError(
            f"the PC's time zone is {name!r}: the schedule follows only US zones that change "
            f"their clocks with US Eastern time ({', '.join(ZONE_SHIFT_H)})"
        )
    return timedelta(hours=hours)


def local_time(at: time, shift: timedelta) -> time:
    """An ET time on the PC's wall clock. US zones west of ET never cross midnight here."""
    base = datetime.combine(date(2000, 1, 3), at)
    moved = base + shift
    if moved.date() != base.date():
        raise ScheduleError(f"{at:%H:%M} ET falls on another day in the PC's time zone")
    return moved.time()


def _shifted(at: time, delta: timedelta) -> time:
    return (datetime.combine(date(2000, 1, 3), at) + delta).time()


# ---------------------------------------------------------------- task XML


@dataclass(frozen=True)
class Target:
    """Where, and as whom, the tasks run."""

    root: Path
    user: str  # DOMAIN\name
    logon: Logon

    @property
    def python(self) -> Path:
        exe = "pythonw.exe" if self.logon == "interactive" else "python.exe"
        return self.root / ".venv" / "Scripts" / exe

    def arguments(self, command: Sequence[str]) -> str:
        return subprocess.list2cmdline(["-m", WRAPPER, "--root", str(self.root), *command])

    @property
    def logon_type(self) -> str:
        return "InteractiveToken" if self.logon == "interactive" else "S4U"


def iso_duration(span: timedelta) -> str:
    """``PT6H40M`` and the like (whole seconds)."""
    hours, rest = divmod(int(span.total_seconds()), 3600)
    minutes, seconds = divmod(rest, 60)
    text = "PT" + (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "")
    text += f"{seconds}S" if seconds else ""
    return text if text != "PT" else "PT0S"


def task_xml(entry: TaskEntry, target: Target, *, shift: timedelta, first_day: date) -> str:
    """The task's definition (Task Scheduler schema 1.2), with ``\\n`` line ends."""
    lines = [
        '<?xml version="1.0" encoding="UTF-16"?>',
        f'<Task version="1.2" xmlns="{NAMESPACE}">',
        "  <RegistrationInfo>",
        "    <Author>ai-day-trader (trader schedule install)</Author>",
        f"    <Description>{escape(entry.description)}</Description>",
        f"    <URI>{escape(entry.path)}</URI>",
        "  </RegistrationInfo>",
        *_triggers(entry, shift=shift, first_day=first_day),
        "  <Principals>",
        '    <Principal id="Author">',
        f"      <UserId>{escape(target.user)}</UserId>",
        f"      <LogonType>{target.logon_type}</LogonType>",
        "      <RunLevel>LeastPrivilege</RunLevel>",
        "    </Principal>",
        "  </Principals>",
        "  <Settings>",
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
        "    <AllowHardTerminate>true</AllowHardTerminate>",
        "    <StartWhenAvailable>true</StartWhenAvailable>",
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>",
        "    <IdleSettings>",
        "      <StopOnIdleEnd>false</StopOnIdleEnd>",
        "      <RestartOnIdle>false</RestartOnIdle>",
        "    </IdleSettings>",
        "    <AllowStartOnDemand>true</AllowStartOnDemand>",
        "    <Enabled>true</Enabled>",
        "    <Hidden>false</Hidden>",
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>",
        f"    <WakeToRun>{str(entry.wake).lower()}</WakeToRun>",
        f"    <ExecutionTimeLimit>{iso_duration(entry.time_limit)}</ExecutionTimeLimit>",
        f"    <Priority>{entry.priority}</Priority>",
        "  </Settings>",
        '  <Actions Context="Author">',
        "    <Exec>",
        f"      <Command>{escape(str(target.python))}</Command>",
        f"      <Arguments>{escape(target.arguments(entry.command))}</Arguments>",
        f"      <WorkingDirectory>{escape(str(target.root))}</WorkingDirectory>",
        "    </Exec>",
        "  </Actions>",
        "</Task>",
    ]
    return "\n".join(lines) + "\n"


def _triggers(entry: TaskEntry, *, shift: timedelta, first_day: date) -> list[str]:
    if not entry.days:
        return ["  <Triggers />"]
    start = f"{first_day.isoformat()}T{local_time(entry.at, shift):%H:%M:%S}"
    repetition: list[str] = []
    if entry.every is not None and entry.span is not None:
        repetition = [
            "      <Repetition>",
            f"        <Interval>{iso_duration(entry.every)}</Interval>",
            f"        <Duration>{iso_duration(entry.span)}</Duration>",
            "        <StopAtDurationEnd>false</StopAtDurationEnd>",
            "      </Repetition>",
        ]
    return [
        "  <Triggers>",
        "    <CalendarTrigger>",
        *repetition,
        f"      <StartBoundary>{start}</StartBoundary>",
        "      <Enabled>true</Enabled>",
        "      <ScheduleByWeek>",
        "        <DaysOfWeek>",
        *(f"          <{day} />" for day in entry.days),
        "        </DaysOfWeek>",
        "        <WeeksInterval>1</WeeksInterval>",
        "      </ScheduleByWeek>",
        "    </CalendarTrigger>",
        "  </Triggers>",
    ]


def write_xml(xml: str, path: Path) -> None:
    """UTF-16 with a byte-order mark, as Task Scheduler itself exports."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-16")


def fingerprint(xml: str) -> dict[str, str]:
    """What decides how a task runs, for comparing ours with the one installed. Elements Task
    Scheduler leaves out read as its defaults; the trigger's date (the install day) is left out."""
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", xml.strip())
    root = ElementTree.fromstring(text)
    ns = {"t": NAMESPACE}

    def find(path: str, default: str = "") -> str:
        node = root.find(path, ns)
        return default if node is None or node.text is None else node.text.strip()

    trigger = "t:Triggers/t:CalendarTrigger"
    days = root.find(f"{trigger}/t:ScheduleByWeek/t:DaysOfWeek", ns)
    start = find(f"{trigger}/t:StartBoundary")
    on_batteries = (
        find("t:Settings/t:DisallowStartIfOnBatteries", "true")
        + "/"
        + find("t:Settings/t:StopIfGoingOnBatteries", "true")
    )
    found = {
        "command": find("t:Actions/t:Exec/t:Command"),
        "arguments": find("t:Actions/t:Exec/t:Arguments"),
        "working_directory": find("t:Actions/t:Exec/t:WorkingDirectory"),
        "time": start[11:19] if len(start) >= 19 else start,
        "days": "" if days is None else ",".join(child.tag.rsplit("}", 1)[-1] for child in days),
        "every": find(f"{trigger}/t:Repetition/t:Interval"),
        "for": find(f"{trigger}/t:Repetition/t:Duration"),
        "logon": find("t:Principals/t:Principal/t:LogonType"),
        "run_level": find("t:Principals/t:Principal/t:RunLevel"),
        "instances": find("t:Settings/t:MultipleInstancesPolicy"),
        "on_batteries": on_batteries,
        "catch_up": find("t:Settings/t:StartWhenAvailable"),
        "network": find("t:Settings/t:RunOnlyIfNetworkAvailable"),
        "wake": find("t:Settings/t:WakeToRun"),
        "time_limit": find("t:Settings/t:ExecutionTimeLimit"),
        "priority": find("t:Settings/t:Priority"),
        "enabled": find("t:Settings/t:Enabled"),
    }
    return {key: value or DEFAULTS.get(key, "") for key, value in found.items()}


def differences(ours: str, installed: str) -> list[str]:
    """``key: installed -> ours`` for each setting that differs."""
    want, have = fingerprint(ours), fingerprint(installed)
    return [f"{key}: {have[key]!r} -> {want[key]!r}" for key in want if want[key] != have[key]]


# ---------------------------------------------------------------- schtasks and tzutil


@dataclass(frozen=True)
class Output:
    code: int
    out: str
    err: str


Runner = Callable[[Sequence[str]], Output]


def system32(name: str) -> str:
    """A Windows tool by its full path: a bare name is searched for in the interpreter's folder
    and the working directory first."""
    return os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", name)


def run_windows(args: Sequence[str]) -> Output:
    proc = subprocess.run(
        list(args), capture_output=True, stdin=subprocess.DEVNULL, timeout=120, check=False
    )
    return Output(proc.returncode, decode(proc.stdout), decode(proc.stderr))


def decode(raw: bytes) -> str:
    """schtasks writes the console's code page, with ``\\r\\r\\n`` line ends; XML is ASCII."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("mbcs" if sys.platform == "win32" else "latin-1", errors="replace")
    return text.replace("\r\r\n", "\n").replace("\r\n", "\n")


class Schtasks:
    """The few schtasks calls the schedule needs."""

    def __init__(self, run: Runner = run_windows) -> None:
        self._run = run
        self.exe = system32("schtasks.exe")

    def create(self, path: str, xml_file: Path) -> Output:
        return self._run([self.exe, "/Create", "/TN", path, "/XML", str(xml_file), "/F"])

    def query_xml(self, path: str) -> str | None:
        """The task's XML, or None when it isn't installed."""
        out = self._run([self.exe, "/Query", "/TN", path, "/XML"])
        return out.out if out.code == 0 and out.out.strip() else None

    def delete(self, path: str) -> Output:
        return self._run([self.exe, "/Delete", "/TN", path, "/F"])

    def start(self, path: str) -> Output:
        return self._run([self.exe, "/Run", "/TN", path])


def windows_zone(run: Runner = run_windows) -> str:
    """The PC's time zone as Windows names it (``tzutil /g``): Windows' own setting, which a
    ``TZ`` environment variable doesn't change."""
    out = run([system32("tzutil.exe"), "/g"])
    if out.code != 0:
        raise ScheduleError(f"tzutil /g exited {out.code}: {(out.err or out.out).strip()}")
    return out.out.strip()


def current_user(environ: Mapping[str, str] = os.environ) -> str:
    """``DOMAIN\\name`` of the account installing the schedule: the tasks run as it."""
    name, domain = environ.get("USERNAME", ""), environ.get("USERDOMAIN", "")
    if not name:
        raise ScheduleError("USERNAME isn't set, so the account the tasks run as is unknown")
    return f"{domain}\\{name}" if domain else name
