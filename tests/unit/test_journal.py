import asyncio
import re
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.helpers import REPO
from trader.core.clock import ET, NaiveDatetimeError
from trader.memory.journal import (
    Journal,
    JournalClosedError,
    Migration,
    MigrationError,
    apply_migrations,
    connect,
    db_to_dec,
    db_to_ts,
    dec_to_db,
    open_database,
    ts_to_db,
)

T0 = datetime(2026, 10, 9, 9, 35, 5, tzinfo=ET)
META = "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
INSERT_INCIDENT = (
    "INSERT INTO incidents (ts, severity, component, code, message) VALUES (?, 'P3', 'test', ?, '')"
)


def schema(conn: sqlite3.Connection) -> dict[str, object]:
    """Tables with their column definitions, plus named indexes and what they cover."""
    out: dict[str, object] = {}
    names = conn.execute("SELECT type, name, tbl_name FROM sqlite_master ORDER BY name").fetchall()
    for kind, name, table in names:
        if kind == "table":
            out[name] = [tuple(row)[1:] for row in conn.execute(f"PRAGMA table_info({name})")]
        elif kind == "index" and not name.startswith("sqlite_autoindex"):
            columns = [row[2] for row in conn.execute(f"PRAGMA index_info({name})")]
            out[name] = (table, columns)
    return out


def architecture_ddl() -> str:
    text = (REPO / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    section = text[text.index("## 6. Database schema") :]
    match = re.search(r"```sql\n(.*?)```", section, re.DOTALL)
    assert match, "A§6 has no sql block"
    return match.group(1)


# ---------------------------------------------------------------- migrations


def test_fresh_database_is_migrated_in_wal_mode(tmp_path: Path) -> None:
    conn = open_database(tmp_path / "runtime" / "trader.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"meta", "signals", "orders", "llm_calls", "daily_reviews", "book_state"} <= tables
    assert len(tables) == 25
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["schema_version"] == "1"
    assert meta["eval_epoch"] == "0"


def test_migrations_match_the_architecture_schema(tmp_path: Path) -> None:
    # A§6 describes the current schema; the migrations must produce exactly that.
    reference = sqlite3.connect(tmp_path / "reference.db")
    reference.executescript(architecture_ddl())
    migrated = open_database(tmp_path / "migrated.db")
    assert schema(migrated) == schema(reference)


def test_reopening_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "trader.db"
    conn = open_database(path)
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'eval_epoch'")
    conn.close()
    again = open_database(path)
    assert apply_migrations(again) == []
    assert again.execute("SELECT value FROM meta WHERE key = 'eval_epoch'").fetchone()[0] == "1"


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    conn = open_database(tmp_path / "trader.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO decisions (signal_id, book, take) VALUES ('missing', 'A', 1)")


def test_changed_migration_is_refused(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    apply_migrations(conn, [Migration(1, "0001_a.sql", META + "CREATE TABLE t (x);")])
    with pytest.raises(MigrationError, match="changed after it was applied"):
        apply_migrations(conn, [Migration(1, "0001_a.sql", META + "CREATE TABLE t (y);")])


def test_failed_migration_leaves_no_trace(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    good = Migration(1, "0001_init.sql", META)
    bad = Migration(2, "0002_bad.sql", "CREATE TABLE half (x INTEGER);\nTHIS IS NOT SQL;")
    apply_migrations(conn, [good])
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(conn, [good, bad])
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "half" not in names
    assert not conn.in_transaction


def test_version_gaps_are_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    with pytest.raises(MigrationError, match="without gaps"):
        apply_migrations(conn, [Migration(1, "0001_a.sql", META), Migration(3, "0003_c.sql", "")])


def test_database_newer_than_code_is_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.db")
    conn.execute("PRAGMA user_version = 5")
    with pytest.raises(MigrationError, match="newer than this code"):
        apply_migrations(conn, [Migration(1, "0001_a.sql", META)])


# ---------------------------------------------------------------- value conversion


def test_decimal_and_time_round_trip() -> None:
    assert dec_to_db(Decimal("12.30")) == "12.30"
    assert db_to_dec("12.30") == Decimal("12.30")
    assert ts_to_db(T0) == "2026-10-09T09:35:05-04:00"
    assert db_to_ts(ts_to_db(T0)) == T0
    assert dec_to_db(None) is None
    assert db_to_ts(None) is None
    with pytest.raises(NaiveDatetimeError):
        ts_to_db(datetime(2026, 10, 9, 9, 35))  # noqa: DTZ001 - deliberately naive


def test_sqlite_refuses_decimals_without_explicit_conversion(tmp_path: Path) -> None:
    conn = open_database(tmp_path / "trader.db")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("INSERT INTO meta (key, value) VALUES ('x', ?)", (Decimal("1.5"),))


# ---------------------------------------------------------------- the single writer


async def test_concurrent_writes_land_in_submission_order(tmp_path: Path) -> None:
    async with await Journal.open(tmp_path / "trader.db") as journal:

        async def log(i: int) -> None:
            await journal.execute(INSERT_INCIDENT, (ts_to_db(T0), f"c{i:03d}"))

        await asyncio.gather(*(log(i) for i in range(200)))
        rows = await journal.fetch_all("SELECT code FROM incidents ORDER BY id")
    assert [row["code"] for row in rows] == [f"c{i:03d}" for i in range(200)]


async def test_failed_write_raises_and_the_writer_carries_on(tmp_path: Path) -> None:
    async with await Journal.open(tmp_path / "trader.db") as journal:
        with pytest.raises(sqlite3.OperationalError):
            await journal.execute("INSERT INTO no_such_table VALUES (1)")
        await journal.execute(INSERT_INCIDENT, (ts_to_db(T0), "after"))
        assert await journal.fetch_one("SELECT code FROM incidents") is not None


async def test_a_failing_transaction_is_rolled_back(tmp_path: Path) -> None:
    def half_then_fail(conn: sqlite3.Connection) -> None:
        conn.execute(INSERT_INCIDENT, (ts_to_db(T0), "half"))
        raise ValueError("abort")

    async with await Journal.open(tmp_path / "trader.db") as journal:
        with pytest.raises(ValueError, match="abort"):
            await journal.write(half_then_fail)
        assert await journal.fetch_all("SELECT * FROM incidents") == []


async def test_close_drains_pending_writes(tmp_path: Path) -> None:
    path = tmp_path / "trader.db"
    journal = await Journal.open(path)
    pending = [
        asyncio.create_task(journal.execute(INSERT_INCIDENT, (ts_to_db(T0), f"p{i}")))
        for i in range(50)
    ]
    await asyncio.sleep(0)  # let every task submit its write
    await journal.aclose()
    await asyncio.gather(*pending)
    conn = open_database(path)
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 50


async def test_writes_after_close_raise(tmp_path: Path) -> None:
    journal = await Journal.open(tmp_path / "trader.db")
    await journal.aclose()
    with pytest.raises(JournalClosedError):
        await journal.execute(INSERT_INCIDENT, (ts_to_db(T0), "late"))


async def test_meta_round_trip(tmp_path: Path) -> None:
    async with await Journal.open(tmp_path / "trader.db") as journal:
        assert await journal.get_meta("eval_epoch") == "0"
        await journal.set_meta("note", "hello")
        assert await journal.get_meta("note") == "hello"
        assert await journal.get_meta("missing") is None
