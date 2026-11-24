"""The scheduled-job wrapper, `python -m trader.ops.scheduled` (P6.1a; DECISIONS D-080)."""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tests.fixtures.simtime import JumpClock
from tests.helpers import FAKE_SECRETS
from trader.core.clock import ET, fmt_iso
from trader.memory.journal import open_database
from trader.ops import scheduled
from trader.ops.scheduled import (
    FAILED,
    DayState,
    JournalAlert,
    Scheduled,
    day_state,
    invoke_cli,
    job_name,
    last_error,
    split_root,
)
from trader.orchestrator.lock import LOCK_NAME, RunLock

MORNING = datetime(2026, 11, 24, 8, 25, tzinfo=ET)


@pytest.mark.parametrize(
    ("args", "name"),
    [
        (["run-day", "--books", "A,B", "--scheduled"], "run-day"),
        (["universe", "build"], "universe-build"),
        (["schedule", "probe"], "schedule-probe"),
        (["watchdog"], "watchdog"),
        (["--version"], "trader"),
        (["Odd Name!"], "odd_name_"),
    ],
)
def test_job_name(args: list[str], name: str) -> None:
    assert job_name(args) == name


def test_split_root() -> None:
    root, args = split_root(["--root", ".", "watchdog"])
    assert root == Path(".").resolve()
    assert args == ["watchdog"]
    for bad in (["watchdog"], ["--root", "."], []):
        with pytest.raises(SystemExit, match="usage"):
            split_root(bad)


# ---------------------------------------------------------------- the run's rules


class Calls:
    def __init__(self, codes: list[int], states: list[DayState] | None = None) -> None:
        self.codes, self.states = list(codes), list(states or [])
        self.invoked: list[list[str]] = []
        self.alerts: list[tuple[str, int]] = []
        self.slept: list[float] = []

    def run(self, args: list[str], at: datetime = MORNING) -> int:
        return Scheduled(
            root=Path("R").resolve(),
            args=args,
            clock=JumpClock(at),
            invoke=lambda argv: self.invoked.append(argv) or self.codes.pop(0),
            sleep=self.slept.append,
            alert=lambda job, code: self.alerts.append((job, code)),
            day_state=lambda: self.states.pop(0),
        ).run()


def test_success_is_quiet() -> None:
    calls = Calls([0])
    assert calls.run(["watchdog"]) == 0
    assert calls.alerts == []
    assert calls.invoked == [["--root", str(Path("R").resolve()), "watchdog"]]


def test_a_failed_job_is_alerted() -> None:
    calls = Calls([1])
    assert calls.run(["weekly-job"]) == 1
    assert calls.alerts == [("weekly-job", 1)]
    assert calls.slept == []


@pytest.mark.parametrize("state", ["done", "locked"])
def test_run_day_stands_down_quietly(state: DayState) -> None:
    calls = Calls([1], [state])
    assert calls.run(["run-day", "--scheduled"]) == 0
    assert calls.alerts == []


def test_run_day_failing_before_boot_is_retried() -> None:
    calls = Calls([1, 1, 0], ["not_booted", "not_booted"])
    assert calls.run(["run-day", "--scheduled"]) == 0
    assert calls.slept == [scheduled.RETRY_S, scheduled.RETRY_S]
    assert calls.alerts == [("run-day", 1), ("run-day", 1)]  # the alert itself keeps one a day


def test_run_day_is_not_retried_after_0920() -> None:
    calls = Calls([1], ["not_booted"])
    assert calls.run(["run-day"], at=datetime(2026, 11, 24, 9, 20, tzinfo=ET)) == 1
    assert calls.slept == []
    assert calls.alerts == [("run-day", 1)]


def test_a_crashed_engine_is_not_restarted() -> None:
    calls = Calls([1], ["booted"])
    assert calls.run(["run-day"]) == 1
    assert calls.slept == []
    assert calls.alerts == [("run-day", 1)]


# ---------------------------------------------------------------- the CLI in-process


def test_invoke_cli_returns_the_exit_code(tmp_path: Path, repo: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert invoke_cli(["--version"]) == 0
    assert invoke_cli(["no-such-command"]) == 2  # a usage error, raised without standalone mode
    assert invoke_cli(["--root", str(empty), "lessons", "list"]) == 1  # no config: _run's exit
    assert invoke_cli(["--root", str(repo), "lessons", "list"]) == 0
    assert invoke_cli(["--root", str(repo), "report", "evaluation", "--through", "nope"]) == 1


# ---------------------------------------------------------------- run-day's state


def _journal(repo: Path) -> sqlite3.Connection:
    conn = open_database(repo / "runtime" / "trader.db")
    conn.isolation_level = None
    return conn


def test_day_state(repo: Path) -> None:
    clock = JumpClock(MORNING)
    assert day_state(repo, clock) == "not_booted"  # no journal yet
    conn = _journal(repo)
    conn.execute(
        "INSERT INTO day_runs (date, kind, name, at) VALUES (?, 'state', 'BOOT', ?)",
        ("2026-11-24", fmt_iso(MORNING)),
    )
    assert day_state(repo, clock) == "booted"
    lock = RunLock(repo / "runtime" / LOCK_NAME)
    lock.acquire()
    try:
        assert day_state(repo, clock) == "locked"
    finally:
        lock.release()
    conn.execute(
        "INSERT INTO trading_days (date, run_status) VALUES ('2026-11-24', 'DONE') "
        "ON CONFLICT (date) DO UPDATE SET run_status = 'DONE'"
    )
    conn.close()
    assert day_state(repo, clock) == "done"


def test_day_state_without_a_config(tmp_path: Path) -> None:
    assert day_state(tmp_path, JumpClock(MORNING)) == "not_booted"


# ---------------------------------------------------------------- the alert


def _incidents(repo: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(repo / "runtime" / "trader.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT severity, component, message, detail_json, alerted_at FROM incidents "
        "WHERE code = ?",
        (FAILED,),
    ).fetchall()
    conn.close()
    return rows


def test_the_alert_is_once_a_day_and_redacted(repo: Path) -> None:
    clock = JumpClock(MORNING)
    log = repo / "runtime" / "logs" / "scheduled" / "run-day-2026-11-24.log"
    log.parent.mkdir(parents=True)
    secret = FAKE_SECRETS["ALPACA_PAPER_A_SECRET_KEY"]
    log.write_text(f"== start\nerror: the broker said {secret}\n== exit 1\n", encoding="utf-8")
    alert = JournalAlert(repo, clock, log)
    alert("run-day", 1)
    alert("run-day", 1)
    alert("weekly-job", 1)
    rows = _incidents(repo)
    assert [json.loads(r["detail_json"])["job"] for r in rows] == ["run-day", "weekly-job"]
    first = rows[0]
    assert (first["severity"], first["component"]) == ("P2", "scheduler")
    assert secret not in first["message"]
    assert first["message"].startswith("scheduled run-day failed (exit 1): error: the broker said")
    assert "runtime/logs/scheduled/run-day-2026-11-24.log" in first["message"]
    assert first["alerted_at"] is not None  # pumped (provider none stamps it): no engine runs


def test_the_alert_leaves_pumping_to_a_running_engine(repo: Path) -> None:
    clock = JumpClock(MORNING)
    beat = repo / "runtime" / "heartbeat.json"
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text(json.dumps({"ts": fmt_iso(MORNING), "state": "TRADING"}), encoding="utf-8")
    log = repo / "runtime" / "logs" / "scheduled" / "x.log"
    JournalAlert(repo, clock, log)("weekly-job", 1)
    assert [r["alerted_at"] for r in _incidents(repo)] == [None]


def test_the_alert_never_raises(tmp_path: Path) -> None:
    JournalAlert(tmp_path, JumpClock(MORNING), tmp_path / "x.log")("run-day", 1)  # no config


def test_last_error(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    assert last_error(log) == ""
    log.write_text(
        "== start\nfirst\nerror: " + "y" * 400 + "\n\nrun-day failed before BOOT: retrying\n"
        "== exit 1\n",
        encoding="utf-8",
    )
    assert last_error(log) == ("error: " + "y" * 400)[: scheduled.TAIL_CHARS]


# ---------------------------------------------------------------- the real process


def test_importing_the_wrapper_configures_no_logging() -> None:
    """configure_logging binds sys.stderr when its module loads: it must load after the redirect."""
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, trader.ops.scheduled; print('trader.core.logging' in sys.modules, "
            "'trader.cli' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert out.stdout.split() == ["False", "False"]


def _wrapper(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "trader.ops.scheduled", "--root", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_a_job_runs_into_its_log(repo: Path) -> None:
    done = _wrapper(repo, "lessons", "list")
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout == done.stderr == ""  # everything went to the log
    (log,) = (repo / "runtime" / "logs" / "scheduled").glob("lessons-list-*.log")
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("== ")
    assert "start pid" in lines[0]
    assert lines[0].endswith(": trader lessons list")
    assert lines[-1].startswith("== ")
    assert lines[-1].endswith(" exit 0")
    assert _incidents(repo) == []


def test_a_failing_job_is_journaled_once(repo: Path) -> None:
    for _ in range(2):
        done = _wrapper(repo, "report", "evaluation", "--through", "2026-02-31")
        assert done.returncode == 1
    (log,) = (repo / "runtime" / "logs" / "scheduled").glob("report-evaluation-*.log")
    lines = log.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("--through takes YYYY-MM-DD") for line in lines) == 2  # both runs
    assert sum("incident" in line and FAILED in line for line in lines) == 1  # the first alerts
    assert lines[-1].endswith(" exit 1")
    rows = _incidents(repo)
    assert len(rows) == 1
    assert json.loads(rows[0]["detail_json"]) == {
        "job": "report-evaluation",
        "exit": 1,
        "log": f"runtime/logs/scheduled/{log.name}",
    }
