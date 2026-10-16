"""Fees (P4.0a; DECISIONS D-055): the read-only probe, and booking what Alpaca actually charged."""

import asyncio
from collections.abc import AsyncIterator
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from typer.testing import CliRunner

from tests.fixtures.simtime import JumpClock
from trader import runtime
from trader.cli import app
from trader.core.clock import ET, SimClock
from trader.execution import alpaca_broker
from trader.execution.broker import FeeActivity
from trader.execution.records import read_book_day
from trader.execution.sim_broker import SimBroker
from trader.memory.journal import Journal
from trader.risk.ledger import (
    LedgerEntry,
    SettlementLedger,
    cash,
    fee_reconciliation,
    fee_window_start,
    insert_entries,
    start_of_day_cash,
)

D = Decimal


def test_the_fees_probe_lists_the_charges_and_the_cash(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = JumpClock(datetime(2026, 10, 13, 20, 30, tzinfo=ET))
    sim = SimBroker(clock=clock, cash=D("1000.29"))
    sim.charge_fee(D("0.01"), sub_type="REG", symbol="FIGS")
    sim.charge_fee(D("0.01"), sub_type="CAT", symbol="FIGS")
    asyncio.run(clock.advance_to(datetime(2026, 10, 14, 19, 0, tzinfo=ET)))
    monkeypatch.setattr(runtime, "RealClock", lambda: clock)
    monkeypatch.setattr(alpaca_broker.AlpacaBroker, "from_config", lambda _c, _b, _k: sim)
    result = CliRunner().invoke(app, ["--root", str(repo), "fees", "--book", "A", "--days", "5"])
    assert result.exit_code == 0, result.output
    assert "Book A: 2 fee activities since 2026-10-09 19:00" in result.output
    assert "2026-10-13 REG     -0.01 executed  FIGS" in result.output
    assert "2026-10-13: -0.02 in all" in result.output
    assert "cash now 1000.27" in result.output


# ---------------------------------------------------------------- booking what was charged


D12, D13, D14, D15 = (date(2026, 10, d) for d in (12, 13, 14, 15))


def row(kind: str, amount: str, trade: date, settle: date, ref: str) -> LedgerEntry:
    return LedgerEntry(
        book="A",
        kind=kind,  # type: ignore[arg-type]
        amount=D(amount),
        trade_date=trade,
        settle_date=settle,
        ref=ref,
    )


SEED = row("deposit", "1000", D12, D12, "seed")
F_SALE = [row("sell", "11.99", D12, D13, "fill:f:1"), row("fee", "-0.01", D12, D13, "fee-est:f:1")]
FIGS_SALE = [
    row("sell", "300.16", D13, D14, "fill:g:16"),
    row("fee", "-0.01", D13, D14, "fee-est:g:16"),
]
BWA_SALE = [
    row("sell", "419.44", D14, D15, "fill:b:8"),
    row("fee", "-0.02", D14, D15, "fee-est:b:8"),
]


EVENING = time(20, 30)  # when paper posted its charges on 10-13 and 10-14 (D-054)


def fee(
    fee_id: str,
    day: date,
    amount: str,
    sub: str = "REG",
    status: str = "executed",
    created: datetime | Literal["evening"] | None = "evening",
) -> FeeActivity:
    """A charge dated ``day``; by default posted that evening, after the close."""
    return FeeActivity(
        id=fee_id,
        date=day,
        sub_type=sub,
        net_amount=D(amount),
        status=status,
        symbol=None,
        created_at=datetime.combine(day, EVENING, ET) if created == "evening" else created,
    )


def test_charged_fees_are_booked_and_retire_that_days_estimates() -> None:
    charged = [fee("r13", D13, "-0.01"), fee("c13", D13, "-0.01", "CAT")]
    rows = fee_reconciliation([SEED, *FIGS_SALE], charged, book="A", today=D14)
    assert rows == [
        row("fee", "-0.01", D13, D13, "fee:c13"),
        row("fee", "-0.01", D13, D13, "fee:r13"),
        row("fee", "0.01", D13, D14, "fee-est-reversal:2026-10-13:2026-10-14"),
    ]
    assert cash([SEED, *FIGS_SALE, *rows]) == D("1300.14")  # 0.02 charged, not 0.01 estimated


def test_booking_twice_books_nothing_more() -> None:
    charged = [fee("r13", D13, "-0.01")]
    first = fee_reconciliation([SEED, *FIGS_SALE], charged, book="A", today=D14)
    assert fee_reconciliation([SEED, *FIGS_SALE, *first], charged, book="A", today=D14) == []


def test_todays_estimates_wait_for_tonights_charge() -> None:
    entries = [SEED, *FIGS_SALE, *BWA_SALE]
    rows = fee_reconciliation(entries, [fee("r14", D14, "-0.03")], book="A", today=D14)
    assert [r.ref for r in rows] == ["fee:r14", "fee-est-reversal:2026-10-13:2026-10-14"]
    later = fee_reconciliation([*entries, *rows], [fee("r14", D14, "-0.03")], book="A", today=D15)
    assert [r.ref for r in later] == ["fee-est-reversal:2026-10-14:2026-10-15"]


def test_an_earlier_day_with_no_charge_of_its_own_counts_as_charged_nothing() -> None:
    entries = [SEED, *F_SALE, *FIGS_SALE]
    rows = fee_reconciliation(entries, [fee("r13", D13, "-0.02")], book="A", today=D14)
    assert [r.ref for r in rows] == [
        "fee:r13",
        "fee-est-reversal:2026-10-12:2026-10-13",
        "fee-est-reversal:2026-10-13:2026-10-14",
    ]


def test_no_charge_yet_keeps_every_estimate() -> None:
    assert fee_reconciliation([SEED, *FIGS_SALE], [], book="A", today=D14) == []


def test_a_canceled_charge_is_never_booked_and_a_booked_one_is_undone() -> None:
    canceled = fee("r13", D13, "-0.01", status="canceled")
    assert fee_reconciliation([SEED], [canceled], book="A", today=D14) == []
    booked = [SEED, row("fee", "-0.01", D13, D13, "fee:r13")]
    assert fee_reconciliation(booked, [canceled], book="A", today=D14) == [
        row("fee", "0.01", D13, D13, "fee-void:r13")
    ]
    corrected = fee("r13b", D13, "-0.02", status="correct")
    assert [r.ref for r in fee_reconciliation(booked, [corrected], book="A", today=D14)] == [
        "fee:r13b"
    ]


def test_a_charge_is_dated_when_its_cash_left() -> None:
    """Dated a later day than it posted (by settlement, say: Friday's charge dated Tuesday
    across Columbus Day), a charge still counts from the day it posted; dated earlier, from
    its own date."""
    friday = date(2026, 10, 9)
    late = fee("r9", date(2026, 10, 13), "-0.02", created=datetime.combine(friday, EVENING, ET))
    (charged,) = fee_reconciliation([SEED], [late], book="A", today=D12)
    assert (charged.trade_date, charged.settle_date) == (friday, friday)
    after_midnight = fee("r12", D12, "-0.02", created=datetime(2026, 10, 13, 1, 30, tzinfo=ET))
    (charged,) = fee_reconciliation([SEED], [after_midnight], book="A", today=D14)
    assert charged.trade_date == D12


def test_a_day_retires_only_once_a_charge_posted_after_its_close() -> None:
    """Review (D-055): Alpaca may date a charge the day after its trades. Here 10-12's charge
    is dated 10-13 and posted that night; 10-13's hasn't posted by the 14th's boot, so 10-13's
    estimate stays. A rule keyed on the latest charged date would have retired it too."""
    charged = [fee("r12", D13, "-0.02", created=datetime.combine(D12, EVENING, ET))]
    rows = fee_reconciliation([SEED, *F_SALE, *FIGS_SALE], charged, book="A", today=D14)
    assert [r.ref for r in rows] == ["fee:r12", "fee-est-reversal:2026-10-12:2026-10-13"]


def test_an_older_days_late_charge_leaves_a_later_days_estimates() -> None:
    """Verification of D-058 (D-060): 10-12's charge posts a day late, after 10-13's close,
    and 10-13's own charge hasn't posted by the 14th's boot. Posted after 10-13's close, the
    late charge used to retire 10-13's estimate too, counting 10-13 as charged nothing until
    its charge came. A charge only retires the days it could belong to: its own and earlier."""
    late = fee("r12", D12, "-0.02", created=datetime.combine(D13, EVENING, ET))
    entries = [SEED, *F_SALE, *FIGS_SALE]
    rows = fee_reconciliation(entries, [late], book="A", today=D14)
    assert [r.ref for r in rows] == ["fee:r12", "fee-est-reversal:2026-10-12:2026-10-13"]
    assert cash([*entries, *rows]) == D("1312.12")  # 10-13's 0.01 estimate still counts


def test_without_a_posting_time_only_a_later_dated_charge_retires_a_day() -> None:
    charged = [fee("r13", D13, "-0.02", created=None)]
    rows = fee_reconciliation([SEED, *F_SALE, *FIGS_SALE], charged, book="A", today=D14)
    # 10-13's estimate stays until a later-dated charge: a day's double count, on the safe side
    assert [r.ref for r in rows] == ["fee:r13", "fee-est-reversal:2026-10-12:2026-10-13"]


def test_a_charge_the_seed_paid_is_ignored() -> None:
    """Review F4 (D-055): the seeded cash was already net of it."""
    seeded = datetime(2026, 10, 14, 8, 11, tzinfo=ET)
    charged = [fee("r13", D13, "-0.02"), fee("r14", D14, "-0.02")]
    rows = fee_reconciliation([SEED], charged, book="A", today=D15, seeded_at=seeded)
    assert [r.ref for r in rows] == ["fee:r14"]
    # With no posting times, the date decides: a charge dated the seed's day is booked, a
    # double count on the safe side.
    undated = [fee("u13", D13, "-0.02", created=None), fee("u14", D14, "-0.02", created=None)]
    rows = fee_reconciliation([SEED], undated, book="A", today=D15, seeded_at=seeded)
    assert [r.ref for r in rows] == ["fee:u14"]


def test_the_window_reaches_back_to_the_oldest_estimate_still_open() -> None:
    later = date(2026, 10, 27)
    assert fee_window_start([SEED], later) == date(2026, 10, 17)  # FEE_LOOKBACK
    assert fee_window_start([SEED, *F_SALE, *FIGS_SALE], later) == date(2026, 10, 11)
    retired = row("fee", "0.01", D12, D13, "fee-est-reversal:2026-10-12:2026-10-13")
    assert fee_window_start([SEED, *F_SALE, retired, *FIGS_SALE], later) == D12


# ---------------------------------------------------------------- the ledger and a broker


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=ET)


class Spy:
    """A fee source that remembers what each booking asked for."""

    def __init__(self, broker: SimBroker) -> None:
        self.broker, self.asked = broker, list[datetime]()

    async def fee_activities(self, after: datetime) -> list[FeeActivity]:
        self.asked.append(after)
        return await self.broker.fee_activities(after)


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    j = await Journal.open(tmp_path / "trader.db", inline=True)
    yield j
    await j.aclose()


async def book(journal: Journal, rows: list[LedgerEntry], when: datetime) -> None:
    await journal.write(lambda conn: insert_entries(conn, rows, when))


async def test_the_ledger_books_posted_fees_once(journal: Journal) -> None:
    clock = SimClock(at(12, 9))
    await book(journal, [SEED], clock.now())
    await book(journal, FIGS_SALE, at(13, 10))
    await clock.advance_to(at(13, 20, 30))
    sim = SimBroker(clock=clock)
    sim.charge_fee(D("0.01"), day=D13, sub_type="REG")
    sim.charge_fee(D("0.01"), day=D13, sub_type="CAT")
    await clock.advance_to(at(14, 8, 11))
    ledger = SettlementLedger(journal, "A", clock)
    assert len(await ledger.book_posted_fees(sim)) == 3
    assert await ledger.book_posted_fees(sim) == []
    assert start_of_day_cash(await ledger.entries(), D14) == D("1300.14")


async def test_weeks_between_sessions_lose_no_charge(journal: Journal) -> None:
    """Review F1 (D-055): a fixed 10-day window would miss 10-12..10-14's charges, and the next
    charge would then retire their estimates as if nothing had been charged."""
    clock = SimClock(at(12, 9))
    sim = SimBroker(clock=clock, cash=D("1731.59"))  # the seed plus the three sales below
    await book(journal, [SEED], clock.now())
    charges = ((F_SALE, "0.02", 12), (FIGS_SALE, "0.02", 13), (BWA_SALE, "0.04", 14))
    for sale, charge, day in charges:
        await book(journal, sale, at(day, 10))
        await clock.advance_to(at(day, 20, 30))
        sim.charge_fee(D(charge), day=date(2026, 10, day))
    await clock.advance_to(at(27, 8, 11))
    spy, ledger = Spy(sim), SettlementLedger(journal, "A", clock)
    rows = await ledger.book_posted_fees(spy)
    assert spy.asked == [at(11, 0)]  # the day before the oldest open estimate
    assert [r.ref for r in rows] == [
        "fee:fee-1",
        "fee:fee-2",
        "fee:fee-3",
        "fee-est-reversal:2026-10-12:2026-10-13",
        "fee-est-reversal:2026-10-13:2026-10-14",
        "fee-est-reversal:2026-10-14:2026-10-15",
    ]
    entries = await ledger.entries()
    assert cash(entries) == sim.cash == D("1731.51")
    await ledger.book_posted_fees(spy)
    assert spy.asked[-1] == at(17, 0)  # nothing open now: FEE_LOOKBACK


async def test_a_charge_the_seed_already_paid_is_not_booked(journal: Journal) -> None:
    """Review F4 (D-055): the first seed's cash is already net of earlier charges."""
    clock = SimClock(at(13, 20, 30))
    sim = SimBroker(clock=clock, cash=D("1000.02"))
    sim.charge_fee(D("0.02"), day=D13)
    await clock.advance_to(datetime(2026, 10, 14, 8, 11, 30, 500_000, tzinfo=ET))
    ledger = SettlementLedger(journal, "A", clock)
    await ledger.seed(sim.cash, positions=0, open_orders=0)
    spy = Spy(sim)
    assert await ledger.book_posted_fees(spy) == []
    assert spy.asked == [at(13, 0)]  # a day before the seed's; its posting time decides
    assert cash(await ledger.entries()) == sim.cash == D("1000.00")


class DatedSource(Spy):
    """A broker whose ``after`` compares each activity's date (as midnight ET), not its
    posting time. Alpaca's docs allow either reading (review NF1, D-055)."""

    async def fee_activities(self, after: datetime) -> list[FeeActivity]:
        self.asked.append(after)
        every = await self.broker.fee_activities(datetime(2026, 1, 1, tzinfo=ET))
        return [f for f in every if datetime.combine(f.date, time(0), ET) > after]


async def test_the_seed_days_own_charge_is_booked_however_alpaca_filters(
    journal: Journal,
) -> None:
    """Book A was seeded at 13:02 on 10-12 and sold a minute later. A read from the seed's time
    of day would miss that day's charge under a date filter, and a later charge would then
    retire its estimate as charged nothing."""
    clock = SimClock(at(12, 13, 2))
    await book(journal, [SEED], clock.now())
    await book(journal, F_SALE, at(12, 13, 3))
    await clock.advance_to(at(12, 20, 30))
    sim = SimBroker(clock=clock, cash=D("1011.99"))
    sim.charge_fee(D("0.02"), day=D12)  # dated its trade day, posted that night
    await clock.advance_to(at(27, 8, 30))
    for source in (DatedSource(sim), Spy(sim)):
        assert [f.id for f in await source.fee_activities(at(11, 0))] == ["fee-1"]
    dated, ledger = DatedSource(sim), SettlementLedger(journal, "A", clock)
    rows = await ledger.book_posted_fees(dated)
    assert dated.asked == [at(11, 0)]
    assert [r.ref for r in rows] == ["fee:fee-1", "fee-est-reversal:2026-10-12:2026-10-13"]
    assert cash(await ledger.entries()) == sim.cash == D("1011.97")


async def test_a_charge_dated_the_day_after_its_trades_counts_in_that_days_e(
    journal: Journal,
) -> None:
    """Review F3 (D-055): Alpaca may date a charge the day it posts. E and today's realized
    P&L must come out the same either way."""
    clock = SimClock(at(12, 9))
    await book(journal, [SEED], clock.now())
    await book(journal, FIGS_SALE, at(13, 10))
    await clock.advance_to(at(13, 20, 30))
    sim = SimBroker(clock=clock, cash=D("1300.16"))
    sim.charge_fee(D("0.02"), day=D14)  # dated the 14th, for the 13th's sale
    await clock.advance_to(at(14, 8, 11))
    ledger = SettlementLedger(journal, "A", clock)
    rows = await ledger.book_posted_fees(sim)
    assert [r.ref for r in rows] == ["fee:fee-1", "fee-est-reversal:2026-10-13:2026-10-14"]
    assert start_of_day_cash(await ledger.entries(), D14) == sim.cash == D("1300.14")
    today = await journal.read(lambda conn: read_book_day(conn, "A", D14))
    assert today.realized == 0  # the charge paid yesterday's sale
