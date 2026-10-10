"""`trader halt`, `trader flatten` and `trader resume` (RISK.md §6), offline."""

import asyncio
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fixtures import nyse
from tests.fixtures.simtime import JumpClock
from trader import runtime
from trader.cli import app
from trader.core.clock import ET
from trader.core.models import OrderIntent, Quote
from trader.execution import alpaca_broker
from trader.execution.sim_broker import SimBroker

D = Decimal
MARKET = datetime(2026, 10, 9, 11, 0, tzinfo=ET)
EVENING = datetime(2026, 10, 9, 17, 0, tzinfo=ET)


class FakeRest:
    async def calendar(self, start: date, end: date) -> list[dict[str, str]]:
        return nyse.calendar_rows(max(start, nyse.FIRST), min(end, nyse.LAST))

    async def __aenter__(self) -> "FakeRest":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def holding(clock: JumpClock) -> SimBroker:
    """Book A's account holding 40 XYZ under a working stop leg."""
    sim = SimBroker(clock=clock)
    sim.feed(Quote(symbol="XYZ", ts=MARKET, bid=D("12.30"), bid_size=5, ask=D("12.32"),
                   ask_size=5))  # fmt: skip
    intent = OrderIntent(
        book="A",
        signal_id="20261009-XYZ-orb5",
        symbol="XYZ",
        qty=40,
        entry_limit=D("12.35"),
        stop_price=D("12.21"),
        client_order_id="A-20261009-XYZ-entry-1",
        day_size_multiplier=D("1"),
    )
    asyncio.run(sim.submit_entry_oto(intent))
    return sim


@pytest.fixture
def setup(repo: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, JumpClock, SimBroker]:
    clock = JumpClock(MARKET)
    sim = holding(clock)
    monkeypatch.setattr(runtime, "RealClock", lambda: clock)
    monkeypatch.setattr(runtime.AlpacaRest, "from_config", lambda _cfg, _clock: FakeRest())
    monkeypatch.setattr(alpaca_broker.AlpacaBroker, "from_config", lambda _cfg, _book, _clock: sim)
    return repo, clock, sim


def trader(repo: Path, *args: str, stdin: str | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(app, ["--root", str(repo), *args], input=stdin)
    return result.exit_code, result.output


def active(repo: Path, book: str) -> list[str]:
    conn = sqlite3.connect(repo / "runtime" / "trader.db")
    rows = conn.execute(
        "SELECT reason FROM halts WHERE book = ? AND cleared_at IS NULL ORDER BY id", (book,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def test_halt_one_book(setup: tuple[Path, JumpClock, SimBroker]) -> None:
    repo, _, _ = setup
    code, out = trader(repo, "halt", "--book", "A")
    assert code == 0, out
    assert "Book A: halted" in out
    assert (active(repo, "A"), active(repo, "B")) == (["manual"], [])


def test_halt_defaults_to_every_book(setup: tuple[Path, JumpClock, SimBroker]) -> None:
    repo, _, _ = setup
    assert trader(repo, "halt")[0] == 0
    assert (active(repo, "A"), active(repo, "B")) == (["manual"], ["manual"])


def test_resume_needs_the_typed_confirmation(setup: tuple[Path, JumpClock, SimBroker]) -> None:
    repo, _, _ = setup
    trader(repo, "halt", "--book", "A")
    code, out = trader(repo, "resume", "--book", "A", stdin="resume a\n")
    assert code == 1
    assert "did not match" in out
    assert active(repo, "A") == ["manual"]
    code, out = trader(repo, "resume", "--book", "A", stdin="RESUME A\n")
    assert code == 0, out
    assert "lifted manual" in out
    assert active(repo, "A") == []


def test_resume_refuses_while_the_kill_file_exists(
    setup: tuple[Path, JumpClock, SimBroker],
) -> None:
    repo, _, _ = setup
    trader(repo, "halt", "--book", "A")
    (repo / "runtime" / "KILL").write_text("", encoding="utf-8")
    code, out = trader(repo, "resume", "--book", "A", stdin="RESUME A\n")
    assert code == 1
    assert "KILL exists" in out
    assert active(repo, "A") == ["manual"]


def test_resume_one_book_at_a_time(setup: tuple[Path, JumpClock, SimBroker]) -> None:
    repo, _, _ = setup
    code, out = trader(repo, "resume", "--book", "all")
    assert code == 1
    assert "one book at a time" in out


def test_a_drawdown_halt_needs_its_acknowledgement(
    setup: tuple[Path, JumpClock, SimBroker],
) -> None:
    repo, _, _ = setup
    trader(repo, "halt", "--book", "B")  # creates the database
    conn = sqlite3.connect(repo / "runtime" / "trader.db")
    conn.execute(
        "INSERT INTO halts (book, reason, scope, detail, created_at) VALUES "
        "('B', 'drawdown', 'until_resume', 'peak $1,000.00, now $899.00', ?)",
        (MARKET.isoformat(),),
    )
    conn.commit()
    conn.close()
    code, out = trader(repo, "resume", "--book", "B", stdin="RESUME B\n")
    assert code == 1
    assert "--ack-drawdown" in out
    code, out = trader(repo, "resume", "--book", "B", "--ack-drawdown", stdin="RESUME B\n")
    assert code == 0, out
    assert active(repo, "B") == []


def test_flatten_closes_everything_and_leaves_the_book_halted(
    setup: tuple[Path, JumpClock, SimBroker],
) -> None:
    repo, _, sim = setup
    code, out = trader(repo, "flatten", "--book", "A")
    assert code == 0, out
    assert "Book A: flat and halted." in out
    assert asyncio.run(sim.positions()) == []
    assert active(repo, "A") == ["manual"]  # the flattening marker is gone
    conn = sqlite3.connect(repo / "runtime" / "trader.db")
    kinds = [r[0] for r in conn.execute("SELECT kind FROM ledger_entries ORDER BY id")]
    conn.close()
    assert kinds == ["sell", "fee"]  # the exit is booked


def test_flatten_after_hours_keeps_a_stop_and_raises_a_p1(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = JumpClock(MARKET)
    sim = holding(clock)
    sim.expire_day_orders()
    asyncio.run(clock.advance_to(EVENING))
    monkeypatch.setattr(runtime, "RealClock", lambda: clock)
    monkeypatch.setattr(runtime.AlpacaRest, "from_config", lambda _cfg, _clock: FakeRest())
    monkeypatch.setattr(alpaca_broker.AlpacaBroker, "from_config", lambda _cfg, _book, _clock: sim)
    code, out = trader(repo, "flatten", "--book", "A")
    assert code == 1
    assert "market closed, nothing sold. Still held: XYZ 40" in out
    conn = sqlite3.connect(repo / "runtime" / "trader.db")
    incident = conn.execute("SELECT severity, code FROM incidents").fetchone()
    conn.close()
    assert incident == ("P1", "not_flat")
