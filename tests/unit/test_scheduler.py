"""The Windows Task Scheduler entries and `trader schedule` (P6.1a; DECISIONS D-080). Offline:
schtasks and tzutil are fakes, and conftest refuses the real ones."""

import dataclasses
import json
import xml.etree.ElementTree as ElementTree
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tests.fixtures.simtime import JumpClock
from tests.helpers import FAKE_SECRETS, set_yaml, write_env
from trader.cli import app
from trader.config import load_config
from trader.core.clock import ET
from trader.ops import schedule_cmds
from trader.ops.schedule_cmds import ScheduleDeps
from trader.ops.scheduled import log_path
from trader.ops.scheduler import (
    FOLDER,
    NAMESPACE,
    Output,
    ScheduleError,
    Schtasks,
    Target,
    current_user,
    decode,
    differences,
    entries,
    fingerprint,
    iso_duration,
    local_time,
    probe_entry,
    task_xml,
    windows_zone,
    write_xml,
    zone_shift,
)

NOW = datetime(2026, 11, 23, 14, 0, tzinfo=ET)  # a Monday afternoon
ROOT = Path(r"C:\Trader Root")
TARGET = Target(root=ROOT, user="PC\\trader", logon="s4u")


class FakeTasks:
    """An in-memory Task Scheduler behind the schtasks command lines."""

    def __init__(self, *, refuse: Sequence[str] = ()) -> None:
        self.tasks: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.refuse = set(refuse)
        self.on_start: dict[str, object] = {}

    def __call__(self, args: Sequence[str]) -> Output:
        self.calls.append(list(args))
        verb, path = args[1], args[3]
        if verb in self.refuse:
            return Output(1, "", "ERROR: Access is denied.")
        if verb == "/Create":
            self.tasks[path] = Path(args[5]).read_text(encoding="utf-16")
            return Output(0, f'SUCCESS: The scheduled task "{path}" has been created.', "")
        if verb == "/Query":
            if path not in self.tasks:
                return Output(1, "", "ERROR: The system cannot find the file specified.")
            return Output(0, self.tasks[path], "")
        if verb == "/Delete":
            self.tasks.pop(path, None)
            return Output(0, "SUCCESS", "")
        if verb == "/Run":
            action = self.on_start.get(path)
            if callable(action):
                action()
            return Output(0, "SUCCESS", "")
        raise AssertionError(args)


def deps(
    tasks: FakeTasks,
    *,
    zone: str = "Eastern Standard Time",
    answer: str = "INSTALL",
    lines: list[str] | None = None,
) -> ScheduleDeps:
    out = lines if lines is not None else []
    return ScheduleDeps(
        schtasks=Schtasks(tasks),
        zone=lambda: zone,
        user=lambda: "PC\\trader",
        clock=JumpClock(NOW),
        echo=out.append,
        prompt=lambda _text: answer,
        wait=lambda _s: None,
    )


@pytest.fixture
def ready(repo: Path) -> Path:
    """A repo whose notifications are on (ntfy, with its topic) and with a virtual environment."""
    set_yaml(repo / "config" / "settings.yaml", "notify.provider", "ntfy")
    write_env(repo, {**FAKE_SECRETS, "CLAUDE_TOKEN_CREATED": "2026-10-01", "NTFY_TOPIC": "t0p1c"})
    for exe in ("python.exe", "pythonw.exe"):
        path = repo / ".venv" / "Scripts" / exe
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    return repo


# ---------------------------------------------------------------- the entries


def test_the_jobs_in_et(repo: Path) -> None:
    jobs = {e.name: e for e in entries(load_config(repo).settings)}
    assert list(jobs) == ["run-day", "watchdog", "weekly-job"]
    run_day, watchdog, weekly = jobs["run-day"], jobs["watchdog"], jobs["weekly-job"]
    assert (run_day.at, run_day.command) == (
        time(8, 25),
        ("run-day", "--books", "A,B", "--scheduled"),
    )
    assert (run_day.time_limit, run_day.priority, run_day.wake) == (timedelta(hours=10), 4, True)
    assert (watchdog.at, watchdog.until, watchdog.every) == (
        time(9, 25),
        time(16, 5),
        timedelta(minutes=1),
    )
    assert (watchdog.span, watchdog.time_limit, watchdog.wake) == (
        timedelta(hours=6, minutes=40),
        timedelta(minutes=4),
        True,
    )
    assert (weekly.days, weekly.at, weekly.command, weekly.priority, weekly.wake) == (
        ("Saturday",),
        time(10, 0),
        ("weekly-job",),
        7,
        False,
    )
    assert run_day.days == watchdog.days == ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
    assert run_day.path == "\\ai-day-trader\\run-day"


def test_the_universe_job_only_when_asked(repo: Path) -> None:
    settings = load_config(repo).settings
    universe = entries(settings, with_universe=True)[-1]
    assert (universe.name, universe.at, universe.command) == (
        "universe",
        time(20, 0),
        ("universe", "build"),
    )


def test_run_day_starts_five_minutes_before_boot(repo: Path) -> None:
    set_yaml(repo / "config" / "settings.yaml", "schedule.boot", "08:00")
    assert entries(load_config(repo).settings)[0].at == time(7, 55)


# ---------------------------------------------------------------- time zones


@pytest.mark.parametrize(
    ("zone", "hours"),
    [
        ("Eastern Standard Time", 0),
        ("Central Standard Time", -1),
        ("Mountain Standard Time", -2),
        ("Pacific Standard Time", -3),
        ("Alaskan Standard Time", -4),
        (" Eastern Standard Time\n", 0),
    ],
)
def test_zones_that_keep_et_dst(zone: str, hours: int) -> None:
    assert zone_shift(zone) == timedelta(hours=hours)


@pytest.mark.parametrize(
    ("zone", "says"),
    [
        ("Eastern Standard Time_dstoff", "daylight saving time switched off"),
        ("US Mountain Standard Time", "only US zones"),  # Arizona: no DST
        ("Hawaiian Standard Time", "only US zones"),
        ("UTC", "only US zones"),
        ("GMT Standard Time", "only US zones"),  # DST, but on other dates
    ],
)
def test_zones_the_schedule_refuses(zone: str, says: str) -> None:
    with pytest.raises(ScheduleError, match=says):
        zone_shift(zone)


def test_local_time() -> None:
    assert local_time(time(8, 25), timedelta(hours=-3)) == time(5, 25)
    assert local_time(time(8, 25), timedelta(0)) == time(8, 25)
    with pytest.raises(ScheduleError, match="another day"):
        local_time(time(2, 0), timedelta(hours=-4))


@pytest.mark.parametrize(
    ("span", "text"),
    [
        (timedelta(minutes=1), "PT1M"),
        (timedelta(hours=6, minutes=40), "PT6H40M"),
        (timedelta(hours=10), "PT10H"),
        (timedelta(seconds=90), "PT1M30S"),
        (timedelta(0), "PT0S"),
    ],
)
def test_iso_duration(span: timedelta, text: str) -> None:
    assert iso_duration(span) == text


# ---------------------------------------------------------------- the XML

WATCHDOG_XML = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{NAMESPACE}">
  <RegistrationInfo>
    <Author>ai-day-trader (trader schedule install)</Author>
    <Description>The independent safety check, every minute (RISK.md section 6).</Description>
    <URI>\\ai-day-trader\\watchdog</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <Repetition>
        <Interval>PT1M</Interval>
        <Duration>PT6H40M</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-11-24T09:25:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>
          <Monday />
          <Tuesday />
          <Wednesday />
          <Thursday />
          <Friday />
        </DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>PC\\trader</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT4M</ExecutionTimeLimit>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>C:\\Trader Root\\.venv\\Scripts\\python.exe</Command>
      <Arguments>-m trader.ops.scheduled --root "C:\\Trader Root" watchdog</Arguments>
      <WorkingDirectory>C:\\Trader Root</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def test_the_watchdog_task_xml(repo: Path) -> None:
    watchdog = entries(load_config(repo).settings)[1]
    xml = task_xml(watchdog, TARGET, shift=timedelta(0), first_day=date(2026, 11, 24))
    assert xml == WATCHDOG_XML  # Task Scheduler's own validate-only check accepted this shape
    ElementTree.fromstring(xml.split("\n", 1)[1])  # well-formed past the declaration


def test_the_trigger_follows_the_pc_clock(repo: Path) -> None:
    run_day = entries(load_config(repo).settings)[0]
    xml = task_xml(run_day, TARGET, shift=timedelta(hours=-3), first_day=date(2026, 11, 24))
    assert "<StartBoundary>2026-11-24T05:25:00</StartBoundary>" in xml  # no offset: wall clock
    assert "<Repetition>" not in xml


def test_the_interactive_logon_runs_pythonw(repo: Path) -> None:
    target = Target(root=ROOT, user="PC\\trader", logon="interactive")
    xml = task_xml(
        entries(load_config(repo).settings)[0],
        target,
        shift=timedelta(0),
        first_day=date(2026, 11, 24),
    )
    found = fingerprint(xml)
    assert (found["logon"], found["command"]) == (
        "InteractiveToken",
        r"C:\Trader Root\.venv\Scripts\pythonw.exe",
    )


def test_arguments_are_quoted_for_windows() -> None:
    assert TARGET.arguments(["run-day", "--books", "A,B"]) == (
        '-m trader.ops.scheduled --root "C:\\Trader Root" run-day --books A,B'
    )
    plain = Target(root=Path("D:/"), user="u", logon="s4u")
    assert plain.arguments(["watchdog"]) == "-m trader.ops.scheduled --root D:\\ watchdog"


def test_text_is_escaped(repo: Path) -> None:
    target = Target(root=Path(r"C:\R&D <x>"), user="PC\\a&b", logon="s4u")
    xml = task_xml(probe_entry(), target, shift=timedelta(0), first_day=date(2026, 11, 24))
    assert "<UserId>PC\\a&amp;b</UserId>" in xml
    assert fingerprint(xml)["working_directory"] == r"C:\R&D <x>"


def test_the_probe_has_no_trigger() -> None:
    xml = task_xml(probe_entry(), TARGET, shift=timedelta(0), first_day=date(2026, 11, 24))
    assert "  <Triggers />" in xml
    found = fingerprint(xml)
    assert (found["days"], found["time"], found["arguments"]) == (
        "",
        "",
        '-m trader.ops.scheduled --root "C:\\Trader Root" schedule probe',
    )


def test_fingerprint_and_differences(repo: Path) -> None:
    xml = task_xml(
        entries(load_config(repo).settings)[1],
        TARGET,
        shift=timedelta(0),
        first_day=date(2026, 11, 24),
    )
    found = fingerprint(xml)
    assert found == {
        "command": r"C:\Trader Root\.venv\Scripts\python.exe",
        "arguments": '-m trader.ops.scheduled --root "C:\\Trader Root" watchdog',
        "working_directory": r"C:\Trader Root",
        "time": "09:25:00",
        "days": "Monday,Tuesday,Wednesday,Thursday,Friday",
        "every": "PT1M",
        "for": "PT6H40M",
        "logon": "S4U",
        "run_level": "LeastPrivilege",
        "instances": "IgnoreNew",
        "on_batteries": "false/false",
        "catch_up": "true",
        "network": "false",
        "wake": "true",
        "time_limit": "PT4M",
        "priority": "4",
        "enabled": "true",
    }
    assert differences(xml, xml) == []
    later = xml.replace("2026-11-24T", "2027-01-05T")  # the install date doesn't matter
    assert differences(xml, later) == []
    # Task Scheduler leaves out what equals its defaults: a missing Priority reads as 7
    stored = xml.replace("    <Priority>4</Priority>\n", "")
    assert differences(xml, stored) == ["priority: '7' -> '4'"]


def test_write_xml_is_utf16_with_a_bom(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "t.xml"
    write_xml(WATCHDOG_XML, path)
    assert path.read_bytes()[:2] == b"\xff\xfe"
    assert path.read_text(encoding="utf-16").replace("\r\n", "\n") == WATCHDOG_XML


# ---------------------------------------------------------------- schtasks and tzutil


def test_schtasks_command_lines(tmp_path: Path) -> None:
    tasks = FakeTasks()
    schtasks = Schtasks(tasks)
    file = tmp_path / "w.xml"
    write_xml(WATCHDOG_XML, file)
    path = FOLDER + "watchdog"
    assert schtasks.query_xml(path) is None
    assert schtasks.create(path, file).code == 0
    assert schtasks.query_xml(path) is not None
    schtasks.start(path)
    schtasks.delete(path)
    exe = schtasks.exe
    assert exe.lower().endswith("system32\\schtasks.exe")
    assert tasks.calls == [
        [exe, "/Query", "/TN", path, "/XML"],
        [exe, "/Create", "/TN", path, "/XML", str(file), "/F"],
        [exe, "/Query", "/TN", path, "/XML"],
        [exe, "/Run", "/TN", path],
        [exe, "/Delete", "/TN", path, "/F"],
    ]


def test_decode() -> None:
    assert decode(b"<a/>\r\r\n<b/>\r\r\n") == "<a/>\n<b/>\n"  # schtasks' own line ends
    assert decode("x\r\ny".encode("utf-16")) == "x\ny"
    assert decode(b"caf\xe9") in ("caf\N{LATIN SMALL LETTER E WITH ACUTE}", "caf\ufffd")


def test_windows_zone() -> None:
    seen: list[list[str]] = []

    def run(args: Sequence[str]) -> Output:
        seen.append(list(args))
        return Output(0, "Eastern Standard Time", "")

    assert windows_zone(run) == "Eastern Standard Time"
    assert seen[0][0].lower().endswith("system32\\tzutil.exe")
    assert seen[0][1:] == ["/g"]
    with pytest.raises(ScheduleError, match="exited 5"):
        windows_zone(lambda _a: Output(5, "", "denied"))


def test_current_user() -> None:
    assert current_user({"USERDOMAIN": "PC", "USERNAME": "trader"}) == "PC\\trader"
    assert current_user({"USERNAME": "trader"}) == "trader"
    with pytest.raises(ScheduleError):
        current_user({})


# ---------------------------------------------------------------- show, install, uninstall


def test_show_changes_nothing(ready: Path) -> None:
    tasks, lines = FakeTasks(), []
    cfg = load_config(ready)
    assert (
        schedule_cmds.show(ready, cfg, deps(tasks, lines=lines), logon=None, with_universe=False)
        == 0
    )
    text = "\n".join(lines)
    assert "Runs as PC\\trader, whether you're logged on or not (S4U" in text
    assert "run-day     Mon-Fri 08:25 ET" in text
    assert "watchdog    Mon-Fri 09:25-16:05 ET, every 1 min" in text
    assert "weekly-job  Sat 10:00 ET" in text
    assert (
        "trader run-day --books A,B --scheduled; stopped after 10 h, wakes the PC, normal" in text
    )
    assert text.count("not installed") == 3
    assert "install would refuse" not in text
    assert all(call[1] == "/Query" for call in tasks.calls)


def test_show_says_why_install_would_refuse(repo: Path) -> None:
    lines: list[str] = []
    cfg = load_config(repo)
    schedule_cmds.show(
        repo, cfg, deps(FakeTasks(), zone="UTC", lines=lines), logon=None, with_universe=False
    )
    text = "\n".join(lines)
    assert "install would refuse:" in text
    assert "only US zones" in text
    assert "notifications are off" in text
    assert "uv sync" in text


def test_install_refuses_without_notifications(repo: Path) -> None:
    tasks, lines = FakeTasks(), []
    cfg = load_config(repo)
    assert (
        schedule_cmds.install(repo, cfg, deps(tasks, lines=lines), logon="s4u", with_universe=False)
        == 1
    )
    assert any("notifications are off" in line for line in lines)
    assert tasks.tasks == {}
    assert not (repo / "runtime" / "schedule.json").exists()


def test_install_refuses_a_provider_without_its_secret(ready: Path) -> None:
    write_env(ready, {**FAKE_SECRETS, "CLAUDE_TOKEN_CREATED": "2026-10-01"})
    lines: list[str] = []
    assert (
        schedule_cmds.install(
            ready,
            load_config(ready),
            deps(FakeTasks(), lines=lines),
            logon="s4u",
            with_universe=False,
        )
        == 1
    )
    assert "cannot install: notify.provider is ntfy, but NTFY_TOPIC isn't set in .env" in lines


def test_install_refuses_a_zone_it_cant_follow(ready: Path) -> None:
    lines: list[str] = []
    code = schedule_cmds.install(
        ready,
        load_config(ready),
        deps(FakeTasks(), zone="Eastern Standard Time_dstoff", lines=lines),
        logon="s4u",
        with_universe=False,
    )
    assert code == 1
    assert any("daylight saving time switched off" in line for line in lines)


def test_install_needs_the_typed_confirmation(ready: Path) -> None:
    tasks, lines = FakeTasks(), []
    code = schedule_cmds.install(
        ready,
        load_config(ready),
        deps(tasks, answer="install", lines=lines),
        logon="s4u",
        with_universe=False,
    )
    assert code == 1
    assert "nothing installed" in lines
    assert tasks.tasks == {}


def test_install(ready: Path) -> None:
    tasks, lines = FakeTasks(), []
    code = schedule_cmds.install(
        ready, load_config(ready), deps(tasks, lines=lines), logon="s4u", with_universe=False
    )
    assert code == 0, lines
    assert sorted(tasks.tasks) == [FOLDER + n for n in ("run-day", "watchdog", "weekly-job")]
    watchdog = fingerprint(tasks.tasks[FOLDER + "watchdog"])
    assert (watchdog["time"], watchdog["every"], watchdog["logon"]) == ("09:25:00", "PT1M", "S4U")
    assert "<StartBoundary>2026-11-24T09:25:00" in tasks.tasks[FOLDER + "watchdog"]  # tomorrow
    record = json.loads((ready / "runtime" / "schedule.json").read_text(encoding="utf-8"))
    assert (record["logon"], record["user"], record["zone"]) == (
        "s4u",
        "PC\\trader",
        "Eastern Standard Time",
    )
    assert sorted(record["tasks"]) == sorted(tasks.tasks)
    assert (ready / "runtime" / "schedule" / "watchdog.xml").read_bytes()[:2] == b"\xff\xfe"
    assert lines[-1].startswith("registered 3 tasks in \\ai-day-trader\\")
    # what show reports afterwards
    shown: list[str] = []
    schedule_cmds.show(
        ready, load_config(ready), deps(tasks, lines=shown), logon=None, with_universe=False
    )
    assert sum("installed, as shown" in line for line in shown) == 3


def test_install_stops_at_a_refusal(ready: Path) -> None:
    tasks, lines = FakeTasks(refuse=["/Create"]), []
    code = schedule_cmds.install(
        ready, load_config(ready), deps(tasks, lines=lines), logon="s4u", with_universe=False
    )
    assert code == 1
    assert any("Access is denied" in line for line in lines)
    assert any("elevated PowerShell" in line for line in lines)
    assert not (ready / "runtime" / "schedule.json").exists()


def test_reinstalling_drops_a_task_no_longer_scheduled(ready: Path) -> None:
    tasks = FakeTasks()
    cfg = load_config(ready)
    assert schedule_cmds.install(ready, cfg, deps(tasks), logon="s4u", with_universe=True) == 0
    assert FOLDER + "universe" in tasks.tasks
    lines: list[str] = []
    assert (
        schedule_cmds.install(
            ready, cfg, deps(tasks, lines=lines), logon="s4u", with_universe=False
        )
        == 0
    )
    assert FOLDER + "universe" not in tasks.tasks
    assert "removed \\ai-day-trader\\universe: no longer scheduled" in lines


def test_show_spots_a_task_that_differs(ready: Path) -> None:
    tasks = FakeTasks()
    cfg = load_config(ready)
    schedule_cmds.install(ready, cfg, deps(tasks), logon="interactive", with_universe=False)
    lines: list[str] = []
    schedule_cmds.show(ready, cfg, deps(tasks, lines=lines), logon="s4u", with_universe=False)
    assert any(
        "installed, but differs: command:" in line and "logon: 'InteractiveToken' -> 'S4U'" in line
        for line in lines
    )
    # with no --logon, show compares with what was installed
    lines.clear()
    schedule_cmds.show(ready, cfg, deps(tasks, lines=lines), logon=None, with_universe=False)
    assert sum("installed, as shown" in line for line in lines) == 3


def test_uninstall(ready: Path) -> None:
    tasks = FakeTasks()
    schedule_cmds.install(ready, load_config(ready), deps(tasks), logon="s4u", with_universe=False)
    lines: list[str] = []
    assert schedule_cmds.uninstall(ready, deps(tasks, answer="n", lines=lines)) == 1
    assert len(tasks.tasks) == 3
    assert "nothing deleted" in lines
    assert schedule_cmds.uninstall(ready, deps(tasks, answer="y")) == 0
    assert tasks.tasks == {}
    assert not (ready / "runtime" / "schedule.json").exists()
    lines.clear()
    assert schedule_cmds.uninstall(ready, deps(tasks, lines=lines)) == 0
    assert lines == ["no ai-day-trader tasks are installed"]


# ---------------------------------------------------------------- check


def test_check_runs_the_probe_and_always_deletes_it(ready: Path) -> None:
    tasks, lines = FakeTasks(), []
    d = deps(tasks, lines=lines)
    log = log_path(ready, ("schedule", "probe"), d.clock)

    def probe_ran() -> None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "== 2026-11-23T14:00:01-05:00 start pid 7: trader schedule probe\nPROBE PASSED\n"
            "== 2026-11-23T14:00:09-05:00 exit 0\n",
            encoding="utf-8",
        )

    tasks.on_start[FOLDER + "probe"] = probe_ran
    assert schedule_cmds.check(ready, d, logon=None) == 0
    assert "PROBE PASSED" in lines
    assert lines[-1] == "CHECK PASSED"
    assert FOLDER + "probe" not in tasks.tasks
    probe = [call for call in tasks.calls if call[3] == FOLDER + "probe"]
    assert [call[1] for call in probe] == ["/Create", "/Run", "/Delete"]


def test_check_fails_when_the_probe_does(ready: Path) -> None:
    tasks, lines = FakeTasks(), []
    d = deps(tasks, lines=lines)
    log = log_path(ready, ("schedule", "probe"), d.clock)

    def probe_failed() -> None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("PROBE FAILED: alpaca.A\n== 2026-11-23T14:00:09-05:00 exit 1\n", "utf-8")

    tasks.on_start[FOLDER + "probe"] = probe_failed
    assert schedule_cmds.check(ready, d, logon=None) == 1
    assert lines[-1] == "CHECK FAILED: the probe exited 1"
    assert FOLDER + "probe" not in tasks.tasks


def test_check_gives_up_on_a_silent_probe(ready: Path) -> None:
    tasks, lines = FakeTasks(), []
    waited: list[float] = []
    d = deps(tasks, lines=lines)
    d = dataclasses.replace(d, wait=waited.append)
    stale = log_path(ready, ("schedule", "probe"), d.clock)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("== 2026-11-23T09:00:00-05:00 exit 0\n", encoding="utf-8")  # an old run
    assert schedule_cmds.check(ready, d, logon=None) == 1
    assert "didn't finish" in lines[-1]
    assert sum(waited) >= schedule_cmds.PROBE_WAIT_S
    assert FOLDER + "probe" not in tasks.tasks


def test_check_deletes_the_probe_even_when_it_cant_start(ready: Path) -> None:
    tasks = FakeTasks(refuse=["/Run"])
    assert schedule_cmds.check(ready, deps(tasks), logon=None) == 1
    assert FOLDER + "probe" not in tasks.tasks


# ---------------------------------------------------------------- the CLI


@pytest.fixture
def fake_deps(monkeypatch: pytest.MonkeyPatch) -> FakeTasks:
    tasks = FakeTasks()
    monkeypatch.setattr(
        schedule_cmds, "default_deps", lambda: dataclasses.replace(deps(tasks), echo=typer.echo)
    )
    return tasks


def test_cli_show(repo: Path, fake_deps: FakeTasks) -> None:
    result = CliRunner().invoke(app, ["--root", str(repo), "schedule", "show"])
    assert result.exit_code == 0, result.output
    assert "watchdog    Mon-Fri 09:25-16:05 ET, every 1 min" in result.output
    assert "install would refuse:" in result.output  # the example config has no notifier


def test_cli_install_refuses_without_notifications(repo: Path, fake_deps: FakeTasks) -> None:
    result = CliRunner().invoke(app, ["--root", str(repo), "schedule", "install"])
    assert result.exit_code == 1
    assert "cannot install: notifications are off" in result.output
    assert fake_deps.tasks == {}


def test_cli_rejects_an_unknown_logon(repo: Path, fake_deps: FakeTasks) -> None:
    result = CliRunner().invoke(app, ["--root", str(repo), "schedule", "show", "--logon", "x"])
    assert result.exit_code == 1
    assert "--logon takes s4u or interactive" in result.output


def test_show_lists_the_recent_sessions(ready: Path) -> None:
    """P6.1b (D-081): what S§8 criterion 4 will count, read from the journal."""
    from tests.fixtures.journal_days import add_book_day, add_session, add_unattended_run, journal

    conn = journal(ready / "runtime" / "trader.db")
    friday, monday = date(2026, 11, 20), date(2026, 11, 23)
    for day in (friday, monday):
        add_session(conn, day)
        for book in ("A", "B"):
            add_book_day(conn, day, book, start="1000", end="1001")
    add_unattended_run(conn, friday)
    conn.commit()
    conn.close()
    lines: list[str] = []
    schedule_cmds.show(
        ready, load_config(ready), deps(FakeTasks(), lines=lines), logon=None, with_universe=False
    )
    at = lines.index("Recent sessions (S§8 criterion 4 counts unattended ones without a P1):")
    assert lines[at + 1 : at + 3] == [
        "  2026-11-20  unattended",
        "  2026-11-23  attended: not started by the scheduler",
    ]
