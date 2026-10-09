"""SQLite journal: migrations plus a single-writer repository (ARCHITECTURE.md §4.8, §6).

Every write goes through one asyncio queue drained by one task, which runs it inside a
transaction on a dedicated thread that owns the only write connection. WAL mode lets the
separate query-only reader connection proceed concurrently. Money is stored as Decimal text and
time as ISO-8601 with offset, converted explicitly by the helpers below (no global adapters).
"""

import asyncio
import hashlib
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

from trader.core.clock import fmt_iso, parse_et_iso

MIGRATIONS_PACKAGE = "trader.memory.migrations"
_MIGRATION_NAME = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")

SqlParams = Sequence[Any] | Mapping[str, Any]


class MigrationError(RuntimeError):
    """The database and the migration files disagree; nothing was changed."""


class JournalClosedError(RuntimeError):
    """A write was submitted after ``Journal.aclose()``."""


# ---------------------------------------------------------------- migrations


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def bundled_migrations() -> list[Migration]:
    """The ``NNNN_name.sql`` files shipped in trader/memory/migrations, in version order."""
    found = []
    for entry in resources.files(MIGRATIONS_PACKAGE).iterdir():
        match = _MIGRATION_NAME.fullmatch(entry.name)
        if match:
            found.append(Migration(int(match[1]), entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found, key=lambda m: m.version)


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection in WAL mode with foreign keys on. Transactions are explicit."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = FULL")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        conn.close()
        raise RuntimeError(f"could not enable WAL mode on {path} (got {mode})")
    return conn


def apply_migrations(
    conn: sqlite3.Connection, migrations: Sequence[Migration] | None = None
) -> list[int]:
    """Apply pending migrations in order, each in one transaction; returns the versions applied.

    Refuses to run if an already-applied migration file has changed, or if the database is newer
    than the code.
    """
    todo = bundled_migrations() if migrations is None else list(migrations)
    versions = [m.version for m in todo]
    if versions != list(range(1, len(todo) + 1)):
        raise MigrationError(f"migration versions must run 1..N without gaps, got {versions}")
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > len(todo):
        raise MigrationError(f"database schema v{current} is newer than this code (v{len(todo)})")
    for done in todo[:current]:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (f"migration_{done.version:04d}_sha256",)
        ).fetchone()
        if row is not None and row[0] != done.sha256:
            raise MigrationError(f"{done.name} changed after it was applied; add a new migration")
    applied: list[int] = []
    for m in todo[current:]:
        # executescript commits any open transaction first, so BEGIN/COMMIT live in the script.
        checksum_key = f"migration_{m.version:04d}_sha256"
        script = (
            f"BEGIN IMMEDIATE;\n{m.sql}\n;\n"
            f"PRAGMA user_version = {m.version};\n"
            "INSERT OR REPLACE INTO meta (key, value) VALUES "
            f"('schema_version', '{m.version}'), ('{checksum_key}', '{m.sha256}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(m.version)
    return applied


def open_database(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the journal and bring its schema up to date."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        apply_migrations(conn)
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('eval_epoch', '0')")
    except BaseException:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------- value conversion


def dec_to_db(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def db_to_dec(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def ts_to_db(value: datetime | None) -> str | None:
    """ISO-8601 with offset, in ET. Naive datetimes raise."""
    return None if value is None else fmt_iso(value)


def db_to_ts(text: str | None) -> datetime | None:
    return None if text is None else parse_et_iso(text)


# ---------------------------------------------------------------- the repository


@dataclass(frozen=True)
class _WriteOp:
    fn: Callable[[sqlite3.Connection], Any]
    future: asyncio.Future[Any]


class Journal:
    """Single-writer SQLite repository. Create it with ``await Journal.open(path)``.

    ``await journal.write(fn)`` returns once ``fn``'s transaction is committed. Writes run
    strictly in submission order. A write submitted before a caller is cancelled still commits:
    journal entries are never dropped.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="journal-writer")
        self._reader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="journal-reader")
        self._queue: asyncio.Queue[_WriteOp | None] = asyncio.Queue()
        self._write_conn: sqlite3.Connection | None = None
        self._read_conn: sqlite3.Connection | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @classmethod
    async def open(cls, path: Path) -> Self:
        journal = cls(path)
        loop = asyncio.get_running_loop()
        journal._write_conn = await loop.run_in_executor(journal._writer, open_database, path)
        journal._read_conn = await loop.run_in_executor(journal._reader, _connect_reader, path)
        journal._task = asyncio.create_task(journal._drain(), name="journal-writer")
        return journal

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- writes

    async def write[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn`` on the writer thread inside one transaction; returns after COMMIT."""
        if self._closed:
            raise JournalClosedError("the journal is closed")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put(_WriteOp(fn, future))
        return cast(T, await future)

    async def execute(self, sql: str, params: SqlParams = ()) -> int:
        """Run one write statement; returns ``lastrowid``."""
        return await self.write(lambda conn: conn.execute(sql, params).lastrowid or 0)

    async def set_meta(self, key: str, value: str) -> None:
        await self.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # -- reads

    async def fetch_all(self, sql: str, params: SqlParams = ()) -> list[sqlite3.Row]:
        conn = self._live(self._read_conn)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._reader, lambda: conn.execute(sql, params).fetchall()
        )

    async def fetch_one(self, sql: str, params: SqlParams = ()) -> sqlite3.Row | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def get_meta(self, key: str) -> str | None:
        row = await self.fetch_one("SELECT value FROM meta WHERE key = ?", (key,))
        return None if row is None else str(row["value"])

    # -- lifecycle

    async def aclose(self) -> None:
        """Commit everything already submitted, then close. Later writes raise."""
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)
        if self._task is not None:
            await self._task
        loop = asyncio.get_running_loop()
        if self._write_conn is not None:
            await loop.run_in_executor(self._writer, self._write_conn.close)
        if self._read_conn is not None:
            await loop.run_in_executor(self._reader, self._read_conn.close)
        self._writer.shutdown(wait=True)
        self._reader.shutdown(wait=True)

    async def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        while (op := await self._queue.get()) is not None:
            try:
                result = await loop.run_in_executor(self._writer, self._transaction, op.fn)
            except Exception as exc:
                if not op.future.done():
                    op.future.set_exception(exc)
            else:
                if not op.future.done():
                    op.future.set_result(result)

    def _transaction(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        conn = self._live(self._write_conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return result

    @staticmethod
    def _live(conn: sqlite3.Connection | None) -> sqlite3.Connection:
        if conn is None:
            raise JournalClosedError("the journal is not open")
        return conn


def _connect_reader(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    conn.execute("PRAGMA query_only = ON")
    return conn
