"""The journal's backups (P6.3; the Phase 6 plan's decisions 19 and 20; DECISIONS D-084).

- **Each backup** is ``backups/trader-<date>.db``: checked and self-contained
  (``Journal.backup``). After DONE the engine takes one, and ``trader backup`` takes one now.
- **Pruning** keeps the newest ``db_backups_keep`` by the date in the name. A pruned backup goes
  with any ``-wal``, ``-shm`` or ``-journal`` file beside it; such a file whose backup is gone
  goes too, and so does a ``.partial`` an interrupted backup left. Only these exact names are
  touched, never a link, and nothing below the folder.
- **A copy folder** (``storage.backup_copy_dir``: a OneDrive or USB folder, off this disk) gets
  a byte copy of each backup, read back and compared before it takes the name, and is pruned the
  same way. The folder must exist: a missing one is a drive not connected, and making it would
  put the copy wherever that drive letter now points. Only a live paper day names one, so a
  replay's copy never takes the name of the day's real one.
"""

import hashlib
import os
import re
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from trader.memory.journal import BackupError, Journal, side_files

BACKUP = re.compile(r"trader-(?P<day>\d{4}-\d{2}-\d{2})\.db")
SIDE = re.compile(r"(?P<backup>trader-\d{4}-\d{2}-\d{2}\.db)-(?:wal|shm|journal)")
PARTIAL = re.compile(r"trader-\d{4}-\d{2}-\d{2}\.db\.[0-9a-f]{12}\.partial")
CHUNK = 1024 * 1024
MISSING_FOLDER = "the folder doesn't exist: is its drive connected?"


def backups_dir(db: Path) -> Path:
    return db.parent / "backups"


def backup_name(day: date) -> str:
    return f"trader-{day.isoformat()}.db"


def reason(exc: BaseException) -> str:
    """Why it failed, without the paths an OSError carries: alerts are pushed (A§4.10)."""
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc) or type(exc).__name__


@dataclass(frozen=True)
class Pruned:
    removed: tuple[Path, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class Taken:
    path: Path
    size: int
    pruned: Pruned
    copy: Path | None = None
    copy_error: str | None = None  # the backup stands; only its copy failed
    copy_pruned: Pruned = field(default_factory=Pruned)


async def take_backup(
    journal: Journal, *, folder: Path, day: date, keep: int, copy_dir: Path | None = None
) -> Taken:
    """Back the journal up as ``day``'s, prune, then copy it into ``copy_dir`` if there is one.
    Raises when the backup itself fails, leaving the earlier ones as they were."""
    path = folder / backup_name(day)
    await journal.backup(path)
    size = path.stat().st_size
    pruned = prune(folder, keep)
    if copy_dir is None:
        return Taken(path, size, pruned)
    try:
        copy = copy_backup(path, copy_dir)
    except (OSError, BackupError) as exc:
        return Taken(path, size, pruned, copy_error=reason(exc))
    return Taken(path, size, pruned, copy=copy, copy_pruned=prune(copy_dir, keep))


def copy_backup(source: Path, folder: Path) -> Path:
    """A byte copy of a checked backup into ``folder``, which must exist: written to a
    ``.partial``, read back and compared by SHA-256, then moved into place, once whatever an
    older copy left beside its name is gone."""
    if not folder.is_dir():
        raise BackupError(MISSING_FOLDER)
    dest = folder / source.name
    partial = dest.with_name(f"{dest.name}.{uuid.uuid4().hex[:12]}.partial")
    try:
        written = _copy(source, partial)
        if _sha256(partial) != written:
            raise BackupError("the copy doesn't match the backup it was made from")
        for side in side_files(dest):
            side.unlink(missing_ok=True)
        os.replace(partial, dest)
    except BaseException:
        with suppress(OSError):
            partial.unlink(missing_ok=True)
        raise
    return dest


def prune(folder: Path, keep: int) -> Pruned:
    """Keep the newest ``keep`` backups in ``folder`` (see the module's docstring)."""
    try:
        with os.scandir(folder) as entries:
            names = sorted(e.name for e in entries if e.is_file(follow_symlinks=False))
    except OSError as exc:
        return Pruned(problems=(f"{folder.name}: {reason(exc)}",))
    dated = sorted((m["day"], name) for name in names if (m := BACKUP.fullmatch(name)) and _day(m))
    kept = {name for _, name in dated[-keep:]}
    doomed = [name for _, name in dated[:-keep]]
    for name in names:
        side = SIDE.fullmatch(name)
        if (side is not None and side["backup"] not in kept) or PARTIAL.fullmatch(name):
            doomed.append(name)
    removed: list[Path] = []
    problems: list[str] = []
    for name in sorted(doomed):
        path = folder / name
        try:
            path.unlink()
        except OSError as exc:
            problems.append(f"{name}: {reason(exc)}")
        else:
            removed.append(path)
    return Pruned(tuple(removed), tuple(problems))


def _day(match: re.Match[str]) -> bool:
    try:
        date.fromisoformat(match["day"])
    except ValueError:
        return False
    return True


def _copy(source: Path, dest: Path) -> str:
    """Copy ``source`` to ``dest`` (flushed to the disk); returns the SHA-256 of what it read."""
    digest = hashlib.sha256()
    with source.open("rb") as reader, dest.open("xb") as writer:
        while chunk := reader.read(CHUNK):
            digest.update(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while chunk := reader.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
