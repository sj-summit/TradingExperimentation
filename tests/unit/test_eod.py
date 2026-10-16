"""End-of-day bookkeeping and breakers (P3.5; RISK.md §4-5; DECISIONS D-048)."""

from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.fixtures.exec_harness import CALENDAR, FEES, RISK
from trader.config import BookName
from trader.core.clock import ET, SimClock
from trader.execution.records import Records
from trader.execution.sim_broker import SimBroker
from trader.memory.journal import Journal
from trader.orchestrator.capabilities import CURRENT
from trader.orchestrator.eod import BookClose, EodContext, close_book, mark_step, step_done
from trader.risk.breakers import BookStats, Halts
from trader.risk.ledger import LedgerEntry, SettlementLedger, fill_entries, insert_entries

D = Decimal
DAY = date(2026, 10, 13)
EOD = datetime(2026, 10, 13, 16, 5, tzinfo=ET)


class NoPrices:
    def last_price(self, symbol: str) -> Decimal | None:
        return None


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    j = await Journal.open(tmp_path / "trader.db", inline=True)
    yield j
    await j.aclose()


async def setup(
    journal: Journal,
    *,
    broker_cash: str = "1000",
    ledger_cash: str = "1000",
    capabilities: frozenset[str] = CURRENT,
) -> tuple[EodContext, BookClose]:
    clock = SimClock(EOD)
    await SettlementLedger(journal, "A", SimClock(datetime(2026, 10, 9, 9, 0, tzinfo=ET))).seed(
        D(ledger_cash), positions=0, open_orders=0
    )
    ctx = EodContext(
        journal=journal,
        clock=clock,
        day=DAY,
        risk=RISK,
        capabilities=capabilities,
        halts=Halts(journal, clock),
        stats=BookStats(journal, clock),
        prices=NoPrices(),
        regime={"day_of_week": "Tue"},
    )
    broker = SimBroker(clock=clock, cash=D(broker_cash))
    records = Records(journal, book="A", calendar=CALENDAR, fees=FEES, clock=clock)
    close = BookClose(book="A", broker=broker, records=records, max_drawdown=D("3.50"))
    return ctx, close


async def incidents(journal: Journal) -> list[tuple[str, str]]:
    rows = await journal.fetch_all("SELECT severity, code FROM incidents ORDER BY id")
    return [(r["severity"], r["code"]) for r in rows]


async def halts(journal: Journal, book: BookName = "A") -> list[str]:
    rows = await journal.fetch_all(
        "SELECT reason FROM halts WHERE book = ? AND cleared_at IS NULL ORDER BY id", (book,)
    )
    return [r["reason"] for r in rows]


async def test_a_clean_close_writes_the_stats_once(journal: Journal) -> None:
    ctx, close = await setup(journal)
    await close_book(ctx, close)
    (row,) = await journal.fetch_all("SELECT * FROM daily_stats")
    assert (row["start_equity"], row["end_equity"], row["pnl"]) == ("1000", "1000", "0")
    assert (row["max_intraday_dd"], row["n_trades"], row["regime_json"]) == (
        "3.50",
        0,
        '{"day_of_week": "Tue"}',
    )
    steps = ["reconcile:A", "decisions:A", "trades:A", "fees:A", "stats:A", "breakers:A"]
    assert all([await step_done(journal, DAY, s) for s in steps])
    assert await incidents(journal) == []
    await journal.execute("UPDATE daily_stats SET pnl = 'touched'")
    await close_book(ctx, close)  # a rerun: every step is already done
    (row,) = await journal.fetch_all("SELECT pnl FROM daily_stats")
    assert row["pnl"] == "touched"


async def test_a_small_cash_residual_is_stored_quietly(journal: Journal) -> None:
    ctx, close = await setup(journal, broker_cash="999.98")
    await close_book(ctx, close)
    (row,) = await journal.fetch_all("SELECT cash_residual FROM daily_stats")
    assert row["cash_residual"] == "-0.02"
    assert await incidents(journal) == []


async def test_a_residual_over_half_the_tolerance_is_a_p3(journal: Journal) -> None:
    ctx, close = await setup(journal, broker_cash="999.40")
    await close_book(ctx, close)
    (row,) = await journal.fetch_all("SELECT cash_residual FROM daily_stats")
    assert row["cash_residual"] == "-0.60"
    assert await incidents(journal) == [("P3", "cash_drift")]
    assert await halts(journal) == []


async def test_eod_books_the_charged_fees_before_its_cash_check(journal: Journal) -> None:
    """Yesterday's sale was estimated at 0.01; the broker charged 0.02 overnight (D-055)."""
    ctx, close = await setup(journal, broker_cash="1100")
    yesterday = date(2026, 10, 12)
    sale = fill_entries(
        book="A",
        side="sell",
        symbol="XYZ",
        qty=10,
        price=D(10),
        trade_date=yesterday,
        settle_date=DAY,
        fill_id="s:10",
        fees=FEES,
    )
    assert sale[-1].amount == D("-0.01")
    await journal.write(lambda conn: insert_entries(conn, sale, EOD))
    assert isinstance(close.broker, SimBroker)
    close.broker.charge_fee(D("0.02"), day=yesterday)
    await close_book(ctx, close)
    row = await journal.fetch_one("SELECT * FROM daily_stats")
    assert row is not None
    assert (row["end_equity"], row["cash_residual"]) == ("1099.98", "0.00")
    rows = await journal.fetch_all("SELECT ref FROM ledger_entries ORDER BY id")
    assert [r["ref"] for r in rows][-2:] == ["fee:fee-1", "fee-est-reversal:2026-10-12:2026-10-13"]
    # The charge paid yesterday's sale: the day starts after it, and today's pnl is today's.
    assert (row["start_equity"], row["pnl"]) == ("1099.98", "0.00")


async def test_an_old_recording_books_no_fees(journal: Journal) -> None:
    """Replaying a day recorded before fees were booked makes no call it never made."""
    ctx, close = await setup(journal, capabilities=frozenset())
    await close_book(ctx, close)
    assert not await step_done(journal, DAY, "fees:A")


async def test_a_cash_mismatch_halts_the_book(journal: Journal) -> None:
    ctx, close = await setup(journal, broker_cash="990")
    await close_book(ctx, close)
    assert await incidents(journal) == [("P2", "cash_mismatch")]
    assert await halts(journal) == ["cash_mismatch"]


async def test_the_drawdown_breaker(journal: Journal) -> None:
    ctx, close = await setup(journal)
    await BookStats(journal, ctx.clock).raise_peak("A", D(1120))  # 1000 is 10.7% below
    await close_book(ctx, close)
    assert await incidents(journal) == [("P2", "drawdown")]
    assert await halts(journal) == ["drawdown"]


async def test_the_fifth_losing_session_halts(journal: Journal) -> None:
    ctx, close = await setup(journal, broker_cash="990")
    await journal.execute(
        "INSERT INTO book_state (book, consecutive_losing_days, streak_through, updated_at) "
        "VALUES ('A', 4, '2026-10-12', '2026-10-12T16:05:00-04:00')"
    )
    await journal.execute(
        "INSERT INTO trades (id, book, signal_id, symbol, qty, entry_ts) "
        "VALUES ('A-x', 'A', 'x', 'XYZ', 1, '2026-10-13T10:00:00-04:00')"
    )
    lost = [
        LedgerEntry(book="A", kind="buy", amount=D(-100), trade_date=DAY, settle_date=DAY),
        LedgerEntry(book="A", kind="sell", amount=D(90), trade_date=DAY, settle_date=DAY),
    ]
    await journal.write(lambda conn: insert_entries(conn, lost, EOD))
    await mark_step(journal, DAY, "trades:A", EOD)  # keep the hand-made trade
    await close_book(ctx, close)
    assert await incidents(journal) == [("P2", "losing_streak")]
    assert await halts(journal) == ["losing_streak"]
    row = await journal.fetch_one("SELECT consecutive_losing_days FROM book_state")
    assert row is not None
    assert row["consecutive_losing_days"] == 5
