"""Retention (P6.3; the Phase 6 plan's decision 21; DECISIONS D-084): what goes and what stays
under runtime/, decided from the dates in the names."""

import os
import re
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trader.cli import app
from trader.config import load_config
from trader.core.clock import ET
from trader.memory.epochs import current_epoch_start, start_epoch
from trader.memory.journal import Journal
from trader.ops import retention
from trader.ops.retention import Found, Rule, expired, plan_retention, rules, survey

TODAY = date(2026, 10, 13)
DAYS = 60
CUTOFF = TODAY - timedelta(days=DAYS)  # 2026-08-14: an entry of this day still stays

LOGS = Rule("log", Path("logs"), re.compile(r"(?P<day>\d{4}-\d{2}-\d{2})\.jsonl"), folders=False)
RECORDINGS = Rule(
    "recording", Path("recordings"), re.compile(r"(?P<day>\d{4}-\d{2}-\d{2})"), folders=True,
    through_epoch=True,
)  # fmt: skip
REPLAYS = Rule(
    "replay day", Path("replay"), re.compile(r"(?P<day>\d{4}-\d{2}-\d{2})"), folders=True,
    by_change=True,
)  # fmt: skip


def found(rule: Rule, day: date, *, changed: date | None = None, link: bool = False) -> Found:
    return Found(rule=rule, path=rule.folder / day.isoformat(), day=day, changed=changed,
                 link=link)  # fmt: skip


def gone(entries: list[Found], *, today: date = TODAY, start: date | None = None) -> list[date]:
    return [f.day for f in expired(entries, today=today, days=DAYS, epoch_start=start)]


# ---------------------------------------------------------------- the rule (pure)


def test_logs_go_once_older_than_the_retention_days() -> None:
    days = [CUTOFF - timedelta(days=1), CUTOFF, TODAY, TODAY + timedelta(days=1)]
    assert gone([found(LOGS, d) for d in days]) == [date(2026, 8, 13)]


def test_recordings_and_llm_artifacts_stay_through_epoch_0() -> None:
    """Epoch 0, the build, has no start: everything it recorded stays until P7.1 begins
    epoch 1 (the human's decision 4)."""
    assert gone([found(RECORDINGS, date(2025, 1, 2))], start=None) == []


def test_after_an_epoch_begins_older_ones_from_before_it_go() -> None:
    today, start = date(2027, 1, 15), date(2026, 9, 1)  # the cutoff is 2026-11-16
    days = [date(2026, 8, 31), date(2026, 9, 1), date(2026, 11, 15), date(2026, 12, 31)]
    assert gone([found(RECORDINGS, d) for d in days], today=today, start=start) == [
        date(2026, 8, 31)
    ]


def test_an_epoch_begun_lately_keeps_what_is_still_within_the_days() -> None:
    start = TODAY - timedelta(days=1)
    days = [TODAY - timedelta(days=30), CUTOFF - timedelta(days=1)]
    assert gone([found(RECORDINGS, d) for d in days], start=start) == [date(2026, 8, 13)]


def test_a_replayed_day_stays_while_it_was_replayed_lately() -> None:
    """A replay's folder is named after the day it replays: one made this week of a day long
    gone stays until it, too, is old."""
    old = date(2026, 6, 1)
    entries = [
        found(REPLAYS, old, changed=TODAY - timedelta(days=1)),
        found(REPLAYS, old, changed=old),
    ]
    assert [f.changed for f in expired(entries, today=TODAY, days=DAYS, epoch_start=None)] == [old]


def test_simulated_and_replayed_days_ignore_the_epoch() -> None:
    assert gone([found(REPLAYS, date(2026, 6, 1))], start=None) == [date(2026, 6, 1)]


def test_links_never_go() -> None:
    assert gone([found(LOGS, date(2020, 1, 1), link=True)]) == []


# ---------------------------------------------------------------- the survey (files)


def make(root: Path, *paths: str) -> None:
    for rel in paths:
        path = root / rel
        if rel.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")


def rel(root: Path, entries: list[Found]) -> list[tuple[str, str]]:
    return sorted((f.rule.kind, f.path.relative_to(root).as_posix()) for f in entries)


def age(path: Path, day: date) -> None:
    """Set a folder's last change to noon ET on ``day`` (simulated and replayed days stay while
    they changed lately). Call it last: anything made inside the folder changes it again."""
    at = datetime.combine(day, time(12), ET).timestamp()
    os.utime(path, (at, at))


def test_only_exact_names_are_found(repo: Path) -> None:
    make(
        repo,
        "runtime/logs/2026-08-01.jsonl",
        "runtime/logs/watchdog-2026-08-01.jsonl",
        "runtime/logs/2026-08-01.jsonl.bak",
        "runtime/logs/x2026-08-01.jsonl",
        "runtime/logs/2026-13-01.jsonl",  # no such day
        "runtime/logs/2026-08-02.jsonl/",  # a folder where a file belongs
        "runtime/logs/scheduled/run-day-2026-08-01.log",
        "runtime/logs/scheduled/weekly-job-2026-08-01.log",
        "runtime/logs/scheduled/notes.log",
        "runtime/recordings/2026-08-01/boot.json",
        "runtime/recordings/2026-08-02",  # a file where a folder belongs
        "runtime/llm/2026-08-01/20260801-vet-01-1.input.json",
        "runtime/llm/smoke/2026-08-01/x.json",  # the smoke's: never touched
        "runtime/replay/2026-08-01/trader.db",
        "runtime/replay/logs/2026-08-01.jsonl",
        "runtime/sim/2026-08-01/trader.db",
        "runtime/backups/trader-2026-08-01.db",  # the backups prune themselves
        "runtime/reports/2026-08-01.md",  # reports stay
    )
    assert rel(repo, survey(rules(load_config(repo)))) == [
        ("llm artifacts", "runtime/llm/2026-08-01"),
        ("log", "runtime/logs/2026-08-01.jsonl"),
        ("log", "runtime/logs/watchdog-2026-08-01.jsonl"),
        ("recording", "runtime/recordings/2026-08-01"),
        ("replay day", "runtime/replay/2026-08-01"),
        ("replay log", "runtime/replay/logs/2026-08-01.jsonl"),
        ("scheduled log", "runtime/logs/scheduled/run-day-2026-08-01.log"),
        ("scheduled log", "runtime/logs/scheduled/weekly-job-2026-08-01.log"),
        ("sim day", "runtime/sim/2026-08-01"),
    ]


def test_a_folder_that_is_not_there_finds_nothing(repo: Path) -> None:
    assert survey(rules(load_config(repo))) == []


def test_simulated_and_replayed_days_carry_when_they_last_changed(repo: Path) -> None:
    make(repo, "runtime/replay/2026-06-01/trader.db", "runtime/sim/2026-06-02/trader.db",
         "runtime/recordings/2026-06-03/boot.json")  # fmt: skip
    at = datetime(2026, 10, 12, 21, 0, tzinfo=ET).timestamp()  # 01:00 the next day in UTC
    for folder in ("replay/2026-06-01", "sim/2026-06-02", "recordings/2026-06-03"):
        os.utime(repo / "runtime" / folder, (at, at))
    found = {f.rule.kind: (f.day, f.changed) for f in survey(rules(load_config(repo)))}
    assert found == {
        "replay day": (date(2026, 6, 1), date(2026, 10, 12)),
        "sim day": (date(2026, 6, 2), date(2026, 10, 12)),
        "recording": (date(2026, 6, 3), None),  # a recording is its session's, whenever touched
    }


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows'")
def test_a_junction_is_never_followed(repo: Path, tmp_path: Path) -> None:
    import _winapi

    outside = tmp_path / "outside"
    make(outside, "keep.txt", "inner/keep.txt")
    recordings = repo / "runtime" / "recordings"
    recordings.mkdir(parents=True)
    _winapi.CreateJunction(str(outside), str(recordings / "2020-01-01"))  # a dated link
    make(repo, "runtime/replay/2020-01-02/trader.db")
    _winapi.CreateJunction(str(outside / "inner"), str(repo / "runtime/replay/2020-01-02/link"))
    age(repo / "runtime/replay/2020-01-02", date(2020, 1, 2))
    cfg = load_config(repo)
    entries = survey(rules(cfg))
    assert [(f.rule.kind, f.link) for f in entries] == [("recording", True), ("replay day", False)]
    plan = retention.RetentionPlan(
        today=TODAY, days=DAYS, epoch=1, epoch_start=TODAY,
        expired=tuple(expired(entries, today=TODAY, days=DAYS, epoch_start=TODAY)),
        links=tuple(f for f in entries if f.link),
    )  # fmt: skip
    assert [f.rule.kind for f in plan.expired] == ["replay day"]
    done = retention.apply(plan)
    assert done.failed == ()
    assert not (repo / "runtime/replay/2020-01-02").exists()
    assert (outside / "keep.txt").is_file()
    assert (outside / "inner" / "keep.txt").is_file()  # the link went, not what it pointed to
    assert (recordings / "2020-01-01").is_junction()


def test_a_symlink_is_never_followed(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    make(outside, "keep.txt")
    make(repo, "runtime/sim/2020-01-02/trader.db")
    try:
        os.symlink(outside, repo / "runtime/sim/2020-01-03", target_is_directory=True)
        os.symlink(outside, repo / "runtime/sim/2020-01-02/link", target_is_directory=True)
    except OSError:
        pytest.skip("this account may not make symlinks")
    age(repo / "runtime/sim/2020-01-02", date(2020, 1, 2))
    entries = survey(rules(load_config(repo)))
    assert [(f.path.name, f.link) for f in entries] == [("2020-01-02", False), ("2020-01-03", True)]
    plan = retention.RetentionPlan(
        today=TODAY, days=DAYS, epoch=0, epoch_start=None,
        expired=tuple(expired(entries, today=TODAY, days=DAYS, epoch_start=None)),
        links=tuple(f for f in entries if f.link),
    )  # fmt: skip
    assert retention.apply(plan).failed == ()
    assert (outside / "keep.txt").is_file()
    assert not (repo / "runtime/sim/2020-01-02").exists()


# ---------------------------------------------------------------- applying a plan


def test_a_failure_is_collected_and_the_rest_still_go(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make(
        repo,
        "runtime/logs/2020-01-01.jsonl",
        "runtime/logs/2020-01-02.jsonl",
        "runtime/sim/2020-01-03/trader.db",
    )
    age(repo / "runtime/sim/2020-01-03", date(2020, 1, 3))
    entries = survey(rules(load_config(repo)))
    plan = retention.RetentionPlan(
        today=TODAY, days=DAYS, epoch=0, epoch_start=None,
        expired=tuple(expired(entries, today=TODAY, days=DAYS, epoch_start=None)), links=(),
    )  # fmt: skip
    real = retention._remove

    def stuck(entry: Found) -> None:
        if entry.path.name == "2020-01-02.jsonl":
            raise PermissionError(13, "The process cannot access the file")
        real(entry)

    monkeypatch.setattr(retention, "_remove", stuck)
    done = retention.apply(plan)
    assert [f.path.name for f in done.removed] == ["2020-01-01.jsonl", "2020-01-03"]
    assert [(f.path.name, why) for f, why in done.failed] == [
        ("2020-01-02.jsonl", "PermissionError: The process cannot access the file")
    ]
    assert done.freed == 2  # two one-byte files: the log, and the simulated day's database
    assert (repo / "runtime/logs/2020-01-02.jsonl").is_file()


def test_an_entry_that_is_not_where_its_rule_looks_is_left(repo: Path) -> None:
    """Checked again just before it goes: an entry outside its rule's folder, or whose name
    the rule doesn't match, stays."""
    make(repo, "runtime/logs/2020-01-01.jsonl", "elsewhere/2020-01-01.jsonl", "runtime/logs/x.txt")
    (entry,) = survey(rules(load_config(repo)))
    outside = Found(rule=entry.rule, path=repo / "elsewhere" / "2020-01-01.jsonl", day=entry.day)
    renamed = Found(rule=entry.rule, path=repo / "runtime" / "logs" / "x.txt", day=entry.day)
    plan = retention.RetentionPlan(today=TODAY, days=DAYS, epoch=0, epoch_start=None,
                                   expired=(outside, renamed), links=())  # fmt: skip
    done = retention.apply(plan)
    assert done.removed == ()
    assert len(done.failed) == 2
    assert (repo / "elsewhere" / "2020-01-01.jsonl").is_file()
    assert (repo / "runtime" / "logs" / "x.txt").is_file()


# ---------------------------------------------------------------- the epoch, from the journal


async def test_the_plan_reads_the_epoch_from_the_journal(repo: Path) -> None:
    cfg = load_config(repo)
    make(repo, "runtime/recordings/2026-07-01/boot.json", "runtime/recordings/2026-09-02/boot.json")
    today = date(2026, 11, 30)  # the cutoff is 2026-10-01
    async with await Journal.open(repo / "runtime" / "trader.db") as journal:
        assert await journal.read(current_epoch_start) is None  # epoch 0 has no start
        plan = await plan_retention(cfg, journal, today)
        assert (plan.epoch, plan.epoch_start, plan.expired) == (0, None, ())
        began = datetime(2026, 9, 2, 7, 0, tzinfo=ET)

        def begin(conn: sqlite3.Connection) -> object:
            return start_epoch(conn, epoch=1, at=began, reason="P7.1's freeze",
                               decision="D-084", note_id=None, hashes={})  # fmt: skip

        await journal.write(begin)
        assert await journal.read(current_epoch_start) == began
        plan = await plan_retention(cfg, journal, today)
    assert (plan.epoch, plan.epoch_start) == (1, date(2026, 9, 2))
    assert [f.path.name for f in plan.expired] == ["2026-07-01"]


# ---------------------------------------------------------------- trader retention


def test_the_command_is_a_dry_run_until_apply(repo: Path) -> None:
    make(
        repo,
        "runtime/logs/2020-01-01.jsonl",
        "runtime/logs/2099-01-01.jsonl",
        "runtime/recordings/2020-01-01/boot.json",  # epoch 0: kept
        "runtime/replay/2020-01-02/trader.db",
    )
    age(repo / "runtime/replay/2020-01-02", date(2020, 1, 2))
    runner = CliRunner()
    dry = runner.invoke(app, ["--root", str(repo), "retention"])
    assert dry.exit_code == 0, dry.output
    assert "runtime/logs/2020-01-01.jsonl" in dry.output
    assert "runtime/replay/2020-01-02" in dry.output
    assert "recordings" not in dry.output.split("to delete")[1]
    assert "epoch 0" in dry.output
    assert "--apply" in dry.output
    assert (repo / "runtime/logs/2020-01-01.jsonl").is_file()
    done = runner.invoke(app, ["--root", str(repo), "retention", "--apply"])
    assert done.exit_code == 0, done.output
    assert "deleted 2" in done.output
    assert not (repo / "runtime/logs/2020-01-01.jsonl").exists()
    assert not (repo / "runtime/replay/2020-01-02").exists()
    assert (repo / "runtime/logs/2099-01-01.jsonl").is_file()
    assert (repo / "runtime/recordings/2020-01-01/boot.json").is_file()
    again = runner.invoke(app, ["--root", str(repo), "retention"])
    assert "nothing to delete" in again.output
