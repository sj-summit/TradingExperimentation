"""The journal's backups (P6.3; DECISIONS D-084): verified, self-contained copies, pruned by the
date in their names, with an optional second folder."""

import sqlite3
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

import trader.ops.backup as backup_module
from tests.helpers import set_yaml
from trader.cli import app
from trader.memory import journal as journal_module
from trader.memory.journal import BackupError, Journal, check_copy
from trader.ops.backup import backup_name, copy_backup, prune, take_backup

DAY = date(2026, 10, 13)


def beside(path: Path) -> list[str]:
    """Everything in ``path``'s folder whose name starts with ``path``'s, other than itself."""
    return sorted(
        p.name for p in path.parent.iterdir() if p.name.startswith(path.name) and p != path
    )


def meta(path: Path, key: str) -> str | None:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


@pytest.fixture(params=[False, True], ids=["threads", "inline"])
async def journal(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db", inline=request.param)
    await opened.set_meta("k", "v1")
    yield opened
    await opened.aclose()


# ---------------------------------------------------------------- the copy


async def test_a_backup_is_self_contained_and_checked(journal: Journal, tmp_path: Path) -> None:
    """A plain online backup keeps the WAL flag, and reading it later leaves -wal and -shm files
    beside it: the copy leaves WAL mode, and is checked before it takes the backup's name."""
    dest = tmp_path / "backups" / "trader-2026-10-13.db"
    await journal.backup(dest)
    assert dest.read_bytes()[18:20] == b"\x01\x01"  # the header's rollback-journal versions
    assert check_copy(dest) == []
    conn = sqlite3.connect(dest)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert conn.execute("SELECT value FROM meta WHERE key = 'k'").fetchone()[0] == "v1"
    assert beside(dest) == []  # nothing beside it while a reader has it open
    conn.close()
    assert beside(dest) == []  # no .partial either
    assert sorted(p.name for p in dest.parent.iterdir()) == ["trader-2026-10-13.db"]


async def test_the_journal_itself_stays_in_wal_mode(journal: Journal, tmp_path: Path) -> None:
    await journal.backup(tmp_path / "copy.db")
    row = await journal.fetch_one("PRAGMA journal_mode")
    assert row is not None
    assert row[0] == "wal"
    await journal.set_meta("k", "v2")  # and still takes writes
    assert await journal.get_meta("k") == "v2"


async def test_a_copy_that_fails_its_check_keeps_the_previous_backup(
    journal: Journal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "backups" / "trader-2026-10-13.db"
    await journal.backup(dest)
    await journal.set_meta("k", "v2")
    monkeypatch.setattr(journal_module, "check_copy", lambda _path: ["*** Page 7: corrupt (test)"])
    with pytest.raises(BackupError, match="Page 7"):
        await journal.backup(dest)
    assert meta(dest, "k") == "v1"  # the previous copy stands
    assert sorted(p.name for p in dest.parent.iterdir()) == ["trader-2026-10-13.db"]


def small_db(path: Path, *, wal: bool = False) -> Path:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [(f"row {i} " * 20,) for i in range(400)])
    conn.commit()
    conn.close()
    return path


def test_a_copy_that_stays_in_wal_mode_fails_its_check(tmp_path: Path) -> None:
    assert check_copy(small_db(tmp_path / "wal.db", wal=True)) == ["journal_mode wal"]


def test_a_sound_copy_passes_and_one_quick_check_faults_fails(tmp_path: Path) -> None:
    db = small_db(tmp_path / "copy.db")
    assert check_copy(db) == []
    data = bytearray(db.read_bytes())
    size, pages = int.from_bytes(data[16:18], "big"), int.from_bytes(data[28:32], "big")
    data += b"\x00" * size  # a page that nothing uses...
    data[28:32] = (pages + 1).to_bytes(4, "big")  # ...counted in the header
    db.write_bytes(bytes(data))
    assert check_copy(db) == [f"*** in database main ***\nPage {pages + 1}: never used"]


def test_a_copy_too_broken_to_read_fails_its_check(tmp_path: Path) -> None:
    db = small_db(tmp_path / "copy.db")
    data = bytearray(db.read_bytes())
    size = int.from_bytes(data[16:18], "big")
    data[size : size + 8] = b"\xff" * 8  # page 2's b-tree header: no such page type
    db.write_bytes(bytes(data))
    assert check_copy(db) == ["unreadable (database disk image is malformed)"]


def test_a_copy_with_a_file_beside_it_fails_its_check(tmp_path: Path) -> None:
    db = small_db(tmp_path / "copy.db")
    (tmp_path / "copy.db-shm").write_bytes(b"\x00" * 64)  # rollback mode never touches it
    assert check_copy(db) == ["copy.db-shm left beside it"]


async def test_files_left_beside_the_old_backup_go_before_the_new_one_replaces_it(
    journal: Journal, tmp_path: Path
) -> None:
    """SQLite applies a -wal it finds beside a database, even one another file of the same name
    left there (D-084's experiment): it would turn the new copy into something else."""
    dest = tmp_path / "trader-2026-10-13.db"
    await journal.backup(dest)
    for suffix in ("-wal", "-shm", "-journal"):
        dest.with_name(dest.name + suffix).write_bytes(b"left by a reader of the old copy")
    await journal.set_meta("k", "v2")
    await journal.backup(dest)
    assert beside(dest) == []
    assert meta(dest, "k") == "v2"
    assert check_copy(dest) == []


async def test_a_backup_names_and_makes_its_folder(journal: Journal, tmp_path: Path) -> None:
    assert backup_name(DAY) == "trader-2026-10-13.db"
    dest = tmp_path / "deep" / "er" / backup_name(DAY)
    await journal.backup(dest)
    assert dest.is_file()


async def test_a_failed_backup_leaves_no_partial(
    journal: Journal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(_path: Path) -> list[str]:
        raise sqlite3.OperationalError("database or disk is full (test)")

    monkeypatch.setattr(journal_module, "check_copy", full)
    with pytest.raises(sqlite3.OperationalError):
        await journal.backup(tmp_path / "b" / "trader-2026-10-13.db")
    assert list((tmp_path / "b").iterdir()) == []


# ---------------------------------------------------------------- pruning


def touch(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"x")


def test_pruning_keeps_the_newest_by_the_date_in_the_name(tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    touch(
        folder,
        "trader-2026-10-09.db",
        "trader-2026-10-09.db-wal",
        "trader-2026-10-09.db-shm",
        "trader-2026-10-12.db",
        "trader-2026-10-13.db",
        "trader-2026-10-13.db-shm",  # a kept backup's: left alone
        "trader-2026-10-14.db",
        "trader-2026-10-08.db-wal",  # its backup is gone
        "trader-2026-10-13.db.0123456789ab.partial",  # an interrupted backup's
        "trader-2026-10-13.db.partial",  # not a name a backup writes
        "trader-latest.db",
        "trader-2026-13-01.db",  # no such day
        "notes.txt",
    )
    (folder / "trader-2026-10-01.db").mkdir()  # a folder, not a backup
    pruned = prune(folder, keep=2)
    assert pruned.problems == ()
    assert sorted(p.name for p in pruned.removed) == [
        "trader-2026-10-08.db-wal",
        "trader-2026-10-09.db",
        "trader-2026-10-09.db-shm",
        "trader-2026-10-09.db-wal",
        "trader-2026-10-12.db",
        "trader-2026-10-13.db.0123456789ab.partial",
    ]
    assert sorted(p.name for p in folder.iterdir()) == [
        "notes.txt",
        "trader-2026-10-01.db",
        "trader-2026-10-13.db",
        "trader-2026-10-13.db-shm",
        "trader-2026-10-13.db.partial",
        "trader-2026-10-14.db",
        "trader-2026-13-01.db",
        "trader-latest.db",
    ]


def test_pruning_fewer_than_it_keeps_removes_none(tmp_path: Path) -> None:
    touch(tmp_path, "trader-2026-10-13.db", "trader-2026-10-14.db")
    assert prune(tmp_path, keep=30).removed == ()
    assert len(list(tmp_path.iterdir())) == 2


def test_a_file_that_will_not_go_is_reported_and_the_rest_still_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(tmp_path, "trader-2026-10-09.db", "trader-2026-10-12.db", "trader-2026-10-13.db")
    real = Path.unlink

    def stuck(self: Path, missing_ok: bool = False) -> None:
        if self.name == "trader-2026-10-09.db":
            raise PermissionError(13, "Access is denied")
        real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", stuck)
    pruned = prune(tmp_path, keep=1)
    assert [p.name for p in pruned.removed] == ["trader-2026-10-12.db"]
    assert pruned.problems == ("trader-2026-10-09.db: Access is denied",)


def test_pruning_a_missing_folder_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    pruned = prune(tmp_path / "gone", keep=3)
    assert pruned.removed == ()
    assert len(pruned.problems) == 1


# ---------------------------------------------------------------- the copy folder


async def test_take_backup_prunes_and_fills_the_copy_folder(
    journal: Journal, tmp_path: Path
) -> None:
    folder, copies = tmp_path / "backups", tmp_path / "onedrive"
    touch(folder, "trader-2026-10-08.db", "trader-2026-10-09.db")
    touch(copies, "trader-2026-10-07.db", "trader-2026-10-09.db")
    taken = await take_backup(journal, folder=folder, day=DAY, keep=2, copy_dir=copies)
    assert taken.path == folder / "trader-2026-10-13.db"
    assert taken.size == taken.path.stat().st_size
    assert [p.name for p in taken.pruned.removed] == ["trader-2026-10-08.db"]
    assert taken.copy == copies / "trader-2026-10-13.db"
    assert taken.copy_error is None
    assert taken.copy.read_bytes() == taken.path.read_bytes()
    assert [p.name for p in taken.copy_pruned.removed] == ["trader-2026-10-07.db"]
    assert sorted(p.name for p in copies.iterdir()) == [
        "trader-2026-10-09.db",
        "trader-2026-10-13.db",
    ]


async def test_take_backup_without_a_copy_folder(journal: Journal, tmp_path: Path) -> None:
    taken = await take_backup(journal, folder=tmp_path, day=DAY, keep=30)
    assert (taken.copy, taken.copy_error) == (None, None)
    assert taken.path.is_file()


async def test_a_missing_copy_folder_is_never_made_and_the_backup_stands(
    journal: Journal, tmp_path: Path
) -> None:
    """A missing folder is a drive not plugged in (a USB disk): making it would put the copy
    wherever that letter now points."""
    copies = tmp_path / "usb" / "trader"
    taken = await take_backup(journal, folder=tmp_path / "backups", day=DAY, keep=30,
                              copy_dir=copies)  # fmt: skip
    assert taken.path.is_file()
    assert taken.copy is None
    assert taken.copy_error == "the folder doesn't exist: is its drive connected?"
    assert not copies.exists()


def test_a_copy_that_does_not_read_back_the_same_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "trader-2026-10-13.db"
    source.write_bytes(b"verified backup")
    copies = tmp_path / "copies"
    copies.mkdir()
    (copies / "trader-2026-10-13.db").write_bytes(b"yesterday's copy")
    monkeypatch.setattr(backup_module, "_sha256", lambda _path: "0" * 64)
    with pytest.raises(BackupError, match="doesn't match"):
        copy_backup(source, copies)
    assert sorted(p.name for p in copies.iterdir()) == ["trader-2026-10-13.db"]
    assert (copies / "trader-2026-10-13.db").read_bytes() == b"yesterday's copy"


def test_a_copy_replaces_the_files_beside_an_older_copy(tmp_path: Path) -> None:
    source = tmp_path / "trader-2026-10-13.db"
    source.write_bytes(b"verified backup")
    copies = tmp_path / "copies"
    touch(copies, "trader-2026-10-13.db", "trader-2026-10-13.db-wal", "trader-2026-10-13.db-shm")
    assert copy_backup(source, copies) == copies / "trader-2026-10-13.db"
    assert sorted(p.name for p in copies.iterdir()) == ["trader-2026-10-13.db"]
    assert (copies / "trader-2026-10-13.db").read_bytes() == b"verified backup"


# ---------------------------------------------------------------- trader backup


def trader(repo: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, ["--root", str(repo), *args])
    return result.exit_code, result.output


def test_trader_backup_takes_a_checked_one_now(repo: Path) -> None:
    code, out = trader(repo, "backup")
    assert code == 0, out
    (made,) = (repo / "runtime" / "backups").glob("trader-*.db")
    assert check_copy(made) == []
    assert out.startswith("backup: ")
    assert made.name in out


def test_trader_backup_copies_to_the_configured_folder(repo: Path) -> None:
    copies = repo / "onedrive"
    copies.mkdir()
    set_yaml(repo / "config" / "settings.yaml", "storage.backup_copy_dir", str(copies))
    code, out = trader(repo, "backup")
    assert code == 0, out
    (made,) = (repo / "runtime" / "backups").glob("trader-*.db")
    assert (copies / made.name).read_bytes() == made.read_bytes()
    assert "copied to " in out


def test_trader_backup_says_when_its_copy_failed(repo: Path) -> None:
    set_yaml(repo / "config" / "settings.yaml", "storage.backup_copy_dir", str(repo / "usb"))
    code, out = trader(repo, "backup")
    assert code == 1
    assert "is its drive connected?" in out
    assert len(list((repo / "runtime" / "backups").glob("trader-*.db"))) == 1


def test_trader_backup_to_a_folder_writes_there_and_prunes_nothing(repo: Path) -> None:
    usb = repo / "usb"
    touch(usb, *(f"trader-2020-01-{d:02d}.db" for d in range(1, 32)))  # more than it keeps
    code, out = trader(repo, "backup", "--to", str(usb))
    assert code == 0, out
    assert len(list(usb.glob("trader-*.db"))) == 32
    assert not (repo / "runtime" / "backups").exists()


def test_trader_backup_to_a_missing_folder_is_refused(repo: Path) -> None:
    code, out = trader(repo, "backup", "--to", str(repo / "nope"))
    assert code == 1
    assert "doesn't exist" in out
    assert not (repo / "nope").exists()
