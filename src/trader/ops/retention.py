"""Retention under runtime/ (P6.3; the Phase 6 plan's decision 21; DECISIONS D-084).

Entries are found by the date in their names, and judged by a pure rule (``expired``):

- **Logs** go once older than ``storage.retention_days`` (60): the engine's and the watchdog's
  (``logs/<date>.jsonl``, ``logs/watchdog-<date>.jsonl``), the scheduled jobs'
  (``logs/scheduled/<job>-<date>.log``) and the replays' (``replay/logs/<date>.jsonl``).
- **Recordings and LLM artifacts** (``recordings/<date>/``, ``llm/<date>/``) also stay through
  the current epoch: they go only once older than the retention days *and* from before the epoch
  began. Epoch 0, the build, has no start, so they all stay until P7.1 begins epoch 1 (the
  human's decision of 2026-11-23). ``llm/smoke/`` is never touched.
- **Simulated and replayed days** (``sim/<date>/``, ``replay/<date>/``) go once older than the
  retention days, counted from the later of their name and their last change: a replay's folder
  is named after the day it replays, so one made this week of a day long gone stays too.

Reports and backups stay (backups prune themselves). Only exact names match, a link (a symlink
or a junction) is never followed or deleted, and whatever lies inside a folder that goes is
deleted without following a link in it. Each entry is checked again just before it goes, and an
entry that fails is collected while the rest still go.
"""

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from trader.config import AppConfig
from trader.core.clock import ET
from trader.memory.epochs import current_epoch, current_epoch_start
from trader.memory.journal import Journal
from trader.ops.disk import size_text
from trader.ops.scheduled import LOGS as SCHEDULED_LOGS

Kind = Literal[
    "log", "scheduled log", "replay log", "recording", "llm artifacts", "sim day", "replay day"
]
DAY = r"(?P<day>\d{4}-\d{2}-\d{2})"


@dataclass(frozen=True)
class Rule:
    kind: Kind
    folder: Path
    pattern: re.Pattern[str]  # the whole name; group ``day`` is its date
    folders: bool  # dated folders; else dated files
    through_epoch: bool = False  # also kept while from the current epoch
    by_change: bool = False  # counted from the later of the name and the last change


@dataclass(frozen=True)
class Found:
    rule: Rule
    path: Path
    day: date
    changed: date | None = None  # its last change, ET (by_change rules only)
    link: bool = False  # a symlink or a junction: never followed, never deleted


@dataclass(frozen=True)
class RetentionPlan:
    today: date
    days: int
    epoch: int
    epoch_start: date | None  # None: epoch 0, which keeps every recording
    expired: tuple[Found, ...]
    links: tuple[Found, ...]  # matched a name, but links: left alone


@dataclass(frozen=True)
class Removed:
    removed: tuple[Found, ...]
    failed: tuple[tuple[Found, str], ...]
    freed: int  # bytes


def rules(cfg: AppConfig) -> tuple[Rule, ...]:
    storage = cfg.settings.storage
    runtime = cfg.path(storage.db_path).parent
    dated = re.compile(DAY)
    logs = cfg.path(storage.logs_dir)
    scheduled = cfg.root / SCHEDULED_LOGS
    artifacts = cfg.path(storage.llm_artifacts_dir)
    return (
        Rule("log", logs, re.compile(rf"(?:watchdog-)?{DAY}\.jsonl"), False),
        Rule("scheduled log", scheduled, re.compile(rf"[a-z0-9_-]+-{DAY}\.log"), False),
        Rule("replay log", runtime / "replay" / "logs", re.compile(rf"{DAY}\.jsonl"), False),
        Rule("recording", cfg.path(storage.recordings_dir), dated, True, through_epoch=True),
        Rule("llm artifacts", artifacts, dated, True, through_epoch=True),
        Rule("sim day", runtime / "sim", dated, True, by_change=True),
        Rule("replay day", runtime / "replay", dated, True, by_change=True),
    )


# ---------------------------------------------------------------- the rule (pure)


def expired(
    found: Iterable[Found], *, today: date, days: int, epoch_start: date | None
) -> list[Found]:
    """The entries that go (see the module's docstring)."""
    cutoff = today - timedelta(days=days)
    out: list[Found] = []
    for entry in found:
        if entry.link:
            continue
        last = max(entry.day, entry.changed) if entry.changed is not None else entry.day
        if last >= cutoff:
            continue
        if entry.rule.through_epoch and (epoch_start is None or entry.day >= epoch_start):
            continue
        out.append(entry)
    return out


# ---------------------------------------------------------------- the folders


def survey(found_by: Iterable[Rule]) -> list[Found]:
    """Every entry whose name a rule matches exactly, in rule order, then by name. Reads only."""
    out: list[Found] = []
    for rule in found_by:
        try:
            with os.scandir(rule.folder) as scan:
                entries = sorted(scan, key=lambda e: e.name)
        except OSError:  # not there (yet): nothing to find
            continue
        for entry in entries:
            match = rule.pattern.fullmatch(entry.name)
            day = _day(match["day"]) if match else None
            if day is None:
                continue
            path = Path(entry.path)
            if entry.is_symlink() or entry.is_junction():
                out.append(Found(rule, path, day, link=True))
                continue
            if rule.folders != entry.is_dir(follow_symlinks=False):
                continue  # a file where a folder belongs, or the other way round
            if not rule.folders and not entry.is_file(follow_symlinks=False):
                continue
            changed = _changed(entry) if rule.by_change else None
            out.append(Found(rule, path, day, changed=changed))
    return out


async def plan_retention(cfg: AppConfig, journal: Journal, today: date) -> RetentionPlan:
    """What retention would delete today: the folders surveyed, the epoch read from the
    journal."""
    epoch, start = await journal.read(lambda c: (current_epoch(c), current_epoch_start(c)))
    began = None if start is None else start.date()
    days = cfg.settings.storage.retention_days
    found = survey(rules(cfg))
    return RetentionPlan(
        today=today,
        days=days,
        epoch=epoch,
        epoch_start=began,
        expired=tuple(expired(found, today=today, days=days, epoch_start=began)),
        links=tuple(entry for entry in found if entry.link),
    )


def apply(plan: RetentionPlan) -> Removed:
    """Delete what the plan names; an entry that fails is collected, and the rest still go."""
    removed: list[Found] = []
    failed: list[tuple[Found, str]] = []
    freed = 0
    for entry in plan.expired:
        size = size_of(entry.path)
        try:
            _remove(entry)
        except OSError as exc:
            failed.append((entry, f"{type(exc).__name__}: {exc.strerror or exc}"))
        else:
            removed.append(entry)
            freed += size
    return Removed(tuple(removed), tuple(failed), freed)


def describe(plan: RetentionPlan, root: Path) -> list[str]:
    """The plan in lines, for ``trader retention``."""
    if plan.epoch_start is None:
        epoch = f"epoch {plan.epoch}, which has no start: all kept"
    else:
        epoch = f"epoch {plan.epoch}, from {plan.epoch_start}"
    lines = [
        f"retention on {plan.today}: logs, simulated and replayed days kept {plan.days} days; "
        f"recordings and LLM artifacts also through {epoch}"
    ]
    if plan.expired:
        size = sum(size_of(entry.path) for entry in plan.expired)
        lines.append(f"{len(plan.expired)} to delete ({size_text(size)}):")
        lines += [f"  {e.rule.kind:<14} {shown(e.path, root)}" for e in plan.expired]
    else:
        lines.append("nothing to delete")
    lines += [f"  left alone, a link: {shown(e.path, root)}" for e in plan.links]
    return lines


def shown(path: Path, root: Path) -> str:
    """``path`` relative to ``root`` where it lies inside it."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def size_of(path: Path) -> int:
    """Bytes in a file, or in a folder's files, never following a link; 0 where unreadable."""
    try:
        if path.is_symlink() or path.is_junction():
            return 0
        if not path.is_dir():
            return path.stat().st_size
        total = 0
        with os.scandir(path) as scan:
            for entry in scan:
                total += size_of(Path(entry.path))
        return total
    except OSError:
        return 0


# ---------------------------------------------------------------- deleting


def _remove(entry: Found) -> None:
    """Delete one entry, checked again first: still the name its rule matches, still right in
    the rule's folder, still not a link."""
    path, rule = entry.path, entry.rule
    if path.parent != rule.folder or not rule.pattern.fullmatch(path.name):
        raise OSError(f"{path} is not where its rule looks")
    if path.is_symlink() or path.is_junction():
        raise OSError(f"{path} became a link: never followed")
    if rule.folders:
        _delete_tree(path)
    else:
        path.unlink()


def _delete_tree(folder: Path) -> None:
    """Delete a folder and what it holds without following a link: a symlink or a junction in
    it is removed as a link (on Windows ``os.unlink`` removes a folder link or a junction
    itself), and what it points to is left alone."""
    with os.scandir(folder) as scan:
        entries = list(scan)
    for entry in entries:
        path = Path(entry.path)
        if entry.is_symlink() or entry.is_junction():
            os.unlink(path)
        elif entry.is_dir(follow_symlinks=False):
            _delete_tree(path)
        else:
            path.unlink()
    folder.rmdir()


def _day(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _changed(entry: os.DirEntry[str]) -> date:
    stamp = entry.stat(follow_symlinks=False).st_mtime
    return datetime.fromtimestamp(stamp, tz=ET).date()


def failures(removed: Removed, root: Path) -> Sequence[str]:
    return [f"{shown(entry.path, root)}: {why}" for entry, why in removed.failed]
