"""`trader run-day` wiring (P3.5; DECISIONS D-048), offline: no market data, no orders."""

import asyncio
import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fixtures import nyse
from trader.cli import app
from trader.config import load_config
from trader.core.clock import ET, SimClock
from trader.data.ws import StreamError
from trader.execution.sim_broker import SimBroker
from trader.memory.journal import Journal
from trader.orchestrator import live
from trader.orchestrator import replay as replay_module
from trader.orchestrator.live import BookChoiceError, RunOptions, folders, parse_books, run_day
from trader.orchestrator.lock import RunLock, RunLockError
from trader.orchestrator.provenance import source_sha256

D = Decimal
MORNING = datetime(2026, 10, 13, 8, 31, tzinfo=ET)


class FakeRest:
    def __init__(self, sim: SimClock) -> None:
        self._sim = sim

    async def calendar(self, start: date, end: date) -> list[dict[str, str]]:
        return nyse.calendar_rows(max(start, nyse.FIRST), min(end, nyse.LAST))

    async def clock(self) -> datetime:
        return self._sim.now()

    async def __aenter__(self) -> "FakeRest":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def offline(repo: Path, monkeypatch: pytest.MonkeyPatch) -> SimClock:
    clock = SimClock(MORNING)
    rest = FakeRest(clock)
    monkeypatch.setattr(live.AlpacaRest, "from_config", lambda _cfg, _clock: rest)
    monkeypatch.setattr(
        live.AlpacaBroker,
        "from_config",
        lambda _cfg, book, _clock: SimBroker(clock=clock, book=book),
    )
    return clock


def test_book_b_waits_for_phase_4(repo: Path) -> None:
    result = CliRunner().invoke(app, ["--root", str(repo), "run-day", "--books", "A,B"])
    assert result.exit_code == 1
    assert "Book B arrives in Phase 4" in result.output
    with pytest.raises(BookChoiceError):
        parse_books("C")
    assert parse_books(" a ") == ("A",)


async def test_a_dry_run_checks_boot_and_prints_the_day(repo: Path, offline: SimClock) -> None:
    lines: list[str] = []
    await run_day(repo, RunOptions(books=("A",), dry_run=True), lines.append, clock=offline)
    assert lines[0] == "session 2026-10-13"
    assert "  09:35:03  opening-range scan" in lines
    assert "clock skew +0.00 s (ok)" in lines
    assert "universe: not built yet (run-day builds it at boot)" in lines
    assert lines[-1].startswith("Book A: cash 1000, equity 1000, 0 positions")
    assert "ledger not seeded (seeds at boot); halts none" in lines[-1]
    assert not (repo / "runtime" / "recordings").exists()  # a dry run records nothing


async def test_a_second_engine_is_refused(repo: Path, offline: SimClock) -> None:
    held = RunLock(repo / "runtime" / "run-day.lock")
    held.acquire()
    try:
        with pytest.raises(RunLockError):
            await run_day(repo, RunOptions(books=("A",)), print, clock=offline)
    finally:
        held.release()


async def test_a_holiday_exits_quietly(repo: Path, offline: SimClock) -> None:
    holiday = SimClock(datetime(2026, 11, 26, 8, 30, tzinfo=ET))
    lines: list[str] = []
    await run_day(repo, RunOptions(books=("A",), dry_run=True), lines.append, clock=holiday)
    assert lines == ["2026-11-26 is not a trading day: nothing to do"]


def test_a_simulated_day_has_its_own_folder(repo: Path) -> None:
    cfg = load_config(repo)
    sim = folders(cfg, date(2026, 10, 13), sim=True)
    real = folders(cfg, date(2026, 10, 13), sim=False)
    assert sim.db == repo / "runtime" / "sim" / "2026-10-13" / "trader.db"
    assert sim.lock.parent == sim.db.parent
    assert (real.db, real.lock) == (
        repo / "runtime" / "trader.db",
        repo / "runtime" / "run-day.lock",
    )


async def test_a_simulated_day_starts_from_the_main_universe(tmp_path: Path) -> None:
    main = await Journal.open(tmp_path / "main.db")
    await main.execute(
        "INSERT INTO universe_daily (date, symbol, name, exchange, prev_close, atr14, adv14, "
        "or_vol_avg14, eligible, exclusion_reason) VALUES "
        "('2026-10-13', 'XYZ', 'X', 'NASDAQ', '12', '1', 2000000, 900.0, 1, NULL), "
        "('2026-10-12', 'OLD', 'O', 'NASDAQ', '12', '1', 2000000, 900.0, 1, NULL)"
    )
    await main.aclose()
    sim = await Journal.open(tmp_path / "sim.db", inline=True)
    await live._copy_universe(tmp_path / "main.db", tmp_path / "sim.db", date(2026, 10, 13), sim)
    rows = await sim.fetch_all("SELECT date, symbol FROM universe_daily")
    assert [(r["date"], r["symbol"]) for r in rows] == [("2026-10-13", "XYZ")]
    await live._copy_universe(tmp_path / "absent.db", tmp_path / "sim.db", date(2026, 10, 13), sim)
    await sim.aclose()


async def test_stream_errors_become_incidents(tmp_path: Path) -> None:
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    sink = live._IncidentSink(journal, SimClock(MORNING))
    sink.stream_error(StreamError(405, "symbol limit exceeded"))
    await asyncio.gather(*sink._pending)
    rows = await journal.fetch_all("SELECT severity, code, message FROM incidents")
    assert [(r["severity"], r["code"]) for r in rows] == [("P2", "subscription_refused")]
    assert "405" in rows[0]["message"]
    await journal.aclose()


def test_the_boot_record_names_the_commit_without_running_git(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert live._git_sha(tmp_path) is None
    (git / "packed-refs").write_text("abc123 refs/heads/main\n", encoding="utf-8")
    assert live._git_sha(tmp_path) == "abc123"
    (git / "refs" / "heads" / "main").write_text("def456\n", encoding="utf-8")
    assert live._git_sha(tmp_path) == "def456"
    info: dict[str, Any] = dict(live._boot_info(tmp_path, RunOptions(books=("A",), sim=True)))
    assert (info["git_sha"], info["sim"]) == ("def456", True)
    assert info["source_sha256"] is None  # no source tree here


def test_the_source_hash_names_the_code_whatever_the_line_endings(tmp_path: Path) -> None:
    """An uncommitted tree still has a name in boot.json (D-055)."""
    module = tmp_path / "src" / "trader" / "x.py"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"a = 1\nb = 2\n")
    first = source_sha256(tmp_path)
    module.write_bytes(b"a = 1\r\nb = 2\r\n")  # the same file checked out on Windows
    assert source_sha256(tmp_path) == first
    module.write_bytes(b"a = 1\nb = 3\n")
    assert source_sha256(tmp_path) not in (None, first)


def test_the_source_hash_takes_files_in_posix_path_order(tmp_path: Path) -> None:
    """Windows paths compare without case, and by part: a hash in that order would differ
    from one made on Linux for the same tree (review, D-055)."""
    names = ["src/trader/B.py", "src/trader/a.py", "src/trader/a/b.py"]  # POSIX string order
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(name.encode())
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b"\0" + name.encode() + b"\0")
    assert source_sha256(tmp_path) == digest.hexdigest()


def test_a_replay_says_when_the_code_changed_since_the_recording(tmp_path: Path) -> None:
    module = tmp_path / "src" / "trader" / "x.py"
    module.parent.mkdir(parents=True)
    module.write_text("a = 1\n", encoding="utf-8")
    recorded = {"source_sha256": source_sha256(tmp_path)}
    assert replay_module._notes(tmp_path, recorded, tmp_path) == []
    module.write_text("a = 2\n", encoding="utf-8")
    notes = replay_module._notes(tmp_path, recorded, tmp_path)
    assert notes == ["the code changed since the recording"]
    assert replay_module._notes(tmp_path, {}, tmp_path) == []  # recorded before D-055
