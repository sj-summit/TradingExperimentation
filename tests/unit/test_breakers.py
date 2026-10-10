from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trader.core.clock import ET, SimClock
from trader.memory.journal import Journal
from trader.risk.breakers import (
    BookStats,
    HaltError,
    Halts,
    daily_loss_breached,
    drawdown_breached,
    drawdown_pct,
    losing_streak_breached,
    staleness,
)

D = Decimal
T0 = datetime(2026, 10, 9, 11, 0, tzinfo=ET)
NEXT_OPEN = datetime(2026, 10, 12, 9, 30, tzinfo=ET)


# ---------------------------------------------------------------- the breaker checks


def test_daily_loss_counts_realized_and_unrealized() -> None:
    assert daily_loss_breached(D(-20), D(-10), D(1000), D("3.0"))  # $30 = 3% of $1,000
    assert not daily_loss_breached(D(-20), D("-9.99"), D(1000), D("3.0"))
    assert not daily_loss_breached(D(40), D(-60), D(1000), D("3.0"))  # net -$20


def test_daily_loss_limit_never_exceeds_its_cap() -> None:
    assert daily_loss_breached(D(-50), D(0), D(1000), D("9.0"))  # capped at 5%


def test_drawdown_is_peak_to_trough() -> None:
    assert drawdown_pct(D(1000), D(900)) == D(10)
    assert drawdown_breached(D(1000), D(900), D("10.0"))
    assert not drawdown_breached(D(1000), D("900.01"), D("10.0"))
    assert drawdown_pct(D(0), D(900)) == 0
    assert drawdown_pct(D(1000), D(1100)) == 0


def test_losing_streak_and_staleness() -> None:
    assert losing_streak_breached(5, 5)
    assert not losing_streak_breached(4, 5)
    assert [staleness(age, 30, 120) for age in (10, 30, 119.9, 120)] == [
        "ok",
        "warn",
        "warn",
        "halt",
    ]


# ---------------------------------------------------------------- halts


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    yield opened
    await opened.aclose()


async def test_halts_stack_and_resume_lifts_only_its_own(journal: Journal) -> None:
    clock = SimClock(T0)
    halts = Halts(journal, clock)
    await halts.halt("A", "daily_loss", detail="-$30.10", expires_at=NEXT_OPEN)
    await clock.advance(timedelta(minutes=30))
    await halts.halt("A", "manual", detail="trader halt")
    assert [h.reason for h in await halts.active("A")] == ["daily_loss", "manual"]
    cleared = await halts.resume("A", by="test", ack_drawdown=False)
    assert [h.reason for h in cleared] == ["manual"]
    assert [h.reason for h in await halts.active("A")] == ["daily_loss"]  # still halted today
    await clock.advance_to(NEXT_OPEN)
    assert await halts.active("A") == []  # the session halt ended at the next open
    assert await halts.active("B") == []


async def test_a_repeated_halt_is_one_row(journal: Journal) -> None:
    halts = Halts(journal, SimClock(T0))
    first = await halts.halt("A", "manual")
    again = await halts.halt("A", "manual", detail="again")
    assert first.id == again.id
    assert len(await halts.active("A")) == 1


async def test_drawdown_needs_an_acknowledgement(journal: Journal) -> None:
    halts = Halts(journal, SimClock(T0))
    await halts.halt("B", "drawdown", detail="peak $1,000, now $899")
    with pytest.raises(HaltError, match="ack-drawdown"):
        await halts.resume("B", by="test", ack_drawdown=False)
    assert [h.reason for h in await halts.resume("B", by="test", ack_drawdown=True)] == ["drawdown"]


async def test_session_halts_and_markers_need_an_expiry(journal: Journal) -> None:
    halts = Halts(journal, SimClock(T0))
    with pytest.raises(HaltError, match="expiry"):
        await halts.halt("A", "daily_loss")
    with pytest.raises(HaltError, match="expiry"):
        await halts.halt("A", "flattening")


async def test_a_marker_lapses_unless_renewed(journal: Journal) -> None:
    clock = SimClock(T0)
    halts = Halts(journal, clock)
    await halts.renew_marker("A", "flattening", timedelta(seconds=30))
    await clock.advance(timedelta(seconds=20))
    renewed = await halts.renew_marker("A", "flattening", timedelta(seconds=30))
    assert renewed.expires_at == T0 + timedelta(seconds=50)
    await clock.advance(timedelta(seconds=29))
    assert [h.reason for h in await halts.active("A")] == ["flattening"]
    await clock.advance(timedelta(seconds=2))
    assert await halts.active("A") == []  # its owner stopped renewing it


async def test_book_state_keeps_a_summary(journal: Journal) -> None:
    halts = Halts(journal, SimClock(T0))
    await halts.halt("A", "manual")
    await halts.renew_marker("A", "flattening", timedelta(seconds=30))
    row = await journal.fetch_one("SELECT halted, halt_reason FROM book_state WHERE book = 'A'")
    assert row is not None
    assert (row["halted"], row["halt_reason"]) == (1, "manual")  # markers aren't halts
    await halts.resume("A", by="test", ack_drawdown=False)
    row = await journal.fetch_one("SELECT halted, halt_reason FROM book_state WHERE book = 'A'")
    assert row is not None
    assert (row["halted"], row["halt_reason"]) == (0, None)


# ---------------------------------------------------------------- peak and streak


async def test_peak_only_rises_and_streaks_reset_on_a_good_day(journal: Journal) -> None:
    stats = BookStats(journal, SimClock(T0))
    assert await stats.peak_equity("A") is None
    assert await stats.raise_peak("A", D(1000)) == D(1000)
    assert await stats.raise_peak("A", D(980)) == D(1000)
    assert await stats.raise_peak("A", D("1010.50")) == D("1010.50")
    assert await stats.peak_equity("A") == D("1010.50")
    streak = [await stats.record_day("A", D(pnl)) for pnl in (-5, -3, 0, -1, -2)]
    assert streak == [1, 2, 0, 1, 2]
