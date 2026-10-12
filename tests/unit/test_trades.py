"""Trades rebuilt from the journal (A§6, P3.4; DECISIONS D-044)."""

from collections.abc import AsyncIterator
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trader.core.clock import ET
from trader.memory.journal import Journal
from trader.memory.trades import write_trades

D = Decimal
DAY = date(2026, 10, 13)
SID = "20261013-XYZ-orb5"
TRIGGERED = datetime.combine(DAY, time(9, 40), ET)


def at(hh: int, mm: int, ss: float = 0) -> str:
    return (datetime.combine(DAY, time(hh, mm), ET) + timedelta(seconds=ss)).isoformat()


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    await opened.execute(
        "INSERT INTO signals (id, date, symbol, direction, status, trigger_price, triggered_at, "
        "eval_epoch) VALUES (?, ?, 'XYZ', 'long', 'triggered', '12.72', ?, 0)",
        (SID, DAY.isoformat(), TRIGGERED.isoformat()),
    )
    # A 39-share entry filled in two parts: 20 @ 12.73, then 19 @ 12.74.
    await order(opened, "A-20261013-XYZ-entry-1", "e1", "buy", 39, signal=SID, stop="12.64")
    await fill(opened, "e1", "buy", 20, "12.73", at(9, 40, 1.5), cumulative=20)
    await fill(opened, "e1", "buy", 19, "12.74", at(9, 40, 2), cumulative=39)
    yield opened
    await opened.aclose()


async def order(journal: Journal, cid: str, oid: str, side: str, qty: int, *, signal: str | None,
                stop: str | None = None, purpose: str | None = None) -> None:  # fmt: skip
    await journal.execute(
        "INSERT INTO orders (client_order_id, broker_order_id, book, signal_id, symbol, side, "
        "type, qty, stop_price, status, submitted_at, purpose) "
        "VALUES (?, ?, 'A', ?, 'XYZ', ?, 'limit', ?, ?, 'filled', ?, ?)",
        (cid, oid, signal, side, qty, stop, at(9, 40), purpose),
    )


async def fill(journal: Journal, oid: str, side: str, qty: int, price: str, when: str, *,
               cumulative: int, fee: str | None = None) -> None:  # fmt: skip
    fill_id = f"{oid}:{cumulative}"
    await journal.execute(
        "INSERT INTO fills (id, broker_order_id, book, symbol, side, qty, price, filled_at) "
        "VALUES (?, ?, 'A', 'XYZ', ?, ?, ?, ?)",
        (fill_id, oid, side, qty, price, when),
    )
    if fee:
        await journal.execute(
            "INSERT INTO ledger_entries (book, kind, symbol, amount, trade_date, settle_date, ref) "
            "VALUES ('A', 'fee', 'XYZ', ?, ?, '2026-10-14', ?)",
            (f"-{fee}", DAY.isoformat(), f"fee-est:{fill_id}"),
        )


ENTRY = (20 * D("12.73") + 19 * D("12.74")) / 39  # the entry's VWAP


async def test_a_stop_leg_exit(journal: Journal) -> None:
    await order(journal, "leg-1", "s1", "sell", 39, signal=SID, purpose="stop_leg")
    await fill(journal, "s1", "sell", 39, "12.63", at(10, 15), cumulative=39, fee="0.02")
    (trade,) = await write_trades(journal, "A", DAY)
    assert (trade.id, trade.qty, trade.entry_price) == ("A-" + SID, 39, ENTRY)
    assert (trade.exit_price, trade.exit_reason, trade.fees_est) == (D("12.63"), "stop", D("0.02"))
    assert trade.pnl == 39 * (D("12.63") - ENTRY)
    assert trade.r_multiple == pytest.approx(float((D("12.63") - ENTRY) / (ENTRY - D("12.64"))))
    assert trade.entry_slippage == ENTRY - D("12.72")
    assert trade.entry_delay_ms == 1_500  # 09:40:00 trigger -> 09:40:01.5 first fill


async def test_the_sell_with_the_most_shares_names_the_reason(journal: Journal) -> None:
    await order(journal, "A-20261013-XYZ-stop-1", "s1", "sell", 20, signal=SID,
                purpose="protective_stop")  # fmt: skip
    await fill(journal, "s1", "sell", 20, "12.70", at(11, 0), cumulative=20)
    await order(journal, "A-20261013-XYZ-exit-1", "s2", "sell", 19, signal=SID,
                purpose="eod_flatten")  # fmt: skip
    await fill(journal, "s2", "sell", 19, "13.10", at(15, 55), cumulative=19)
    (trade,) = await write_trades(journal, "A", DAY)
    assert trade.exit_reason == "stop"  # 20 shares beat 19
    assert trade.exit_price == (20 * D("12.70") + 19 * D("13.10")) / 39
    assert trade.exit_ts is not None
    assert trade.exit_ts.time() == time(15, 55)


async def test_a_tie_goes_to_the_last_sell(journal: Journal) -> None:
    await journal.execute("UPDATE fills SET qty = 20, id = 'e1:40' WHERE id = 'e1:39'")
    await order(journal, "A-20261013-XYZ-exit-1", "s1", "sell", 20, signal=SID, purpose="manual")
    await fill(journal, "s1", "sell", 20, "12.80", at(12, 0), cumulative=20)
    await order(journal, "A-20261013-XYZ-exit-2", "s2", "sell", 20, signal=SID,
                purpose="eod_flatten")  # fmt: skip
    await fill(journal, "s2", "sell", 20, "12.90", at(15, 55), cumulative=20)
    (trade,) = await write_trades(journal, "A", DAY)
    assert (trade.qty, trade.exit_reason) == (40, "eod_flatten")


async def test_a_position_still_open_has_no_exit(journal: Journal) -> None:
    await order(journal, "A-20261013-XYZ-exit-1", "s1", "sell", 20, signal=SID, purpose="manual")
    await fill(journal, "s1", "sell", 20, "12.80", at(12, 0), cumulative=20)
    (trade,) = await write_trades(journal, "A", DAY)
    assert (trade.exit_price, trade.exit_reason, trade.pnl, trade.r_multiple) == (
        None, None, None, None,
    )  # fmt: skip


async def test_rebuilding_is_idempotent_and_skips_what_never_filled(journal: Journal) -> None:
    await order(journal, "leg-1", "s1", "sell", 39, signal=SID, purpose="stop_leg")
    await fill(journal, "s1", "sell", 39, "12.63", at(10, 15), cumulative=39)
    # an entry that never filled, and a smoke test's round trip
    await order(journal, "A-20261013-ABC-entry-1", "e2", "buy", 10, signal="20261013-ABC-orb5")
    await order(journal, "A-20261013-F-smoke-1", "e3", "buy", 1, signal="smoke-20261013-F")
    await fill(journal, "e3", "buy", 1, "12.00", at(13, 0), cumulative=1)
    await write_trades(journal, "A", DAY)
    again = await write_trades(journal, "A", DAY)
    assert [t.signal_id for t in again] == [SID]
    count = await journal.fetch_one("SELECT COUNT(*) FROM trades")
    assert count is not None
    assert count[0] == 1
