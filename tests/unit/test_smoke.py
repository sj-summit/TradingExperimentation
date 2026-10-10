"""`trader smoke paper` (P2.7) against SimBroker. The real run is HUMAN GATE G2."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from typer.testing import CliRunner

from tests.fixtures import nyse
from tests.fixtures.exec_harness import NEXT_OPEN, TIMES, Harness
from trader import runtime
from trader.cli import app
from trader.core.clock import ET, SimClock
from trader.core.models import Asset, OrderIntent, Quote, Snapshot, Trade
from trader.execution.service import TradingDay
from trader.execution.sim_broker import SimBroker
from trader.execution.smoke import SmokeDeps, ensure_seeded, run_smoke
from trader.memory.journal import Journal
from trader.risk.ledger import LedgerError, SettlementLedger

D = Decimal


class Market:
    """The REST quote the smoke test prices from."""

    def __init__(self, clock: SimClock, bid: str, ask: str, last: str) -> None:
        self.clock, self.bid, self.ask, self.last = clock, D(bid), D(ask), D(last)

    async def snapshots(
        self, symbols: Sequence[str], *, feed: Literal["iex", "sip"]
    ) -> dict[str, Snapshot]:
        now = self.clock.now()
        return {
            s: Snapshot(
                symbol=s,
                latest_quote=Quote(symbol=s, ts=now, bid=self.bid, bid_size=3, ask=self.ask,
                                   ask_size=3),
                latest_trade=Trade(symbol=s, ts=now, price=self.last, size=100),
            )
            for s in symbols
        }  # fmt: skip

    async def asset(self, symbol: str) -> Asset | None:
        return Asset(symbol=symbol, name=symbol, exchange="NYSE", asset_class="us_equity",
                     status="active", tradable=True)  # fmt: skip


@asynccontextmanager
async def smoke_rig(
    tmp_path: Path, bid: str = "12.30", ask: str = "12.32", **options: object
) -> AsyncIterator[tuple[Harness, SmokeDeps]]:
    harness = await Harness(tmp_path, **options).open()  # type: ignore[arg-type]
    harness.quote(bid, ask)
    deps = SmokeDeps(
        service=harness.service,
        broker=harness.sim,
        records=harness.records,
        halts=harness.halts,
        journal=harness.journal,
        clock=harness.clock,
        market=Market(harness.clock, bid, ask, bid),
        day=TradingDay(times=TIMES, next_open=NEXT_OPEN),
        mode="paper",
        feed="iex",
        events=(),
        kill_file=tmp_path / "KILL",
    )
    try:
        yield harness, deps
    finally:
        await harness.close()


async def smoke(h: Harness, deps: SmokeDeps, *, marketable: bool) -> tuple[bool, list[str]]:
    lines: list[str] = []
    task = asyncio.create_task(run_smoke(deps, symbol="XYZ", marketable=marketable,
                                         echo=lines.append))  # fmt: skip
    while not task.done():
        await h.advance(0.25)
    return task.result(), lines


async def test_resting_order_is_accepted_with_a_held_leg_then_cancelled(tmp_path: Path) -> None:
    async with smoke_rig(tmp_path) as (h, deps):
        ok, lines = await smoke(h, deps, marketable=False)
        assert ok, lines
        # 95% of the 12.30 bid, rounded down; the stop 2% under that.
        assert "resting 1 x XYZ: bid 12.30 ask 12.32; limit 11.68, stop 11.44" in lines
        assert "risk gate: approved" in lines
        assert "accepted: entry new, stop leg held" in lines
        assert "cancelled: entry canceled, stop leg canceled" in lines
        assert lines[-1] == "SMOKE PASSED"
        assert await h.held() == {}
        assert await h.status("A-20261009-XYZ-smoke-1") == "canceled"


async def test_marketable_order_fills_reports_its_leg_and_flattens(tmp_path: Path) -> None:
    async with smoke_rig(tmp_path) as (h, deps):
        ok, lines = await smoke(h, deps, marketable=True)
        assert ok, lines
        assert "filled at 12.33; stop leg after the fill: new" in lines
        assert "flattened: flat" in lines
        assert await h.held() == {}
        ledger_lines = lines[lines.index("-- ledger") + 1 :]
        assert any('"kind": "buy"' in line for line in ledger_lines)
        assert any('"kind": "sell"' in line for line in ledger_lines)


async def test_smoke_entries_never_count_as_strategy_entries(tmp_path: Path) -> None:
    async with smoke_rig(tmp_path) as (h, deps):
        await smoke(h, deps, marketable=False)
        exposure = await h.service.exposure()
        assert (exposure.entries_today, exposure.symbols_entered_today) == (0, frozenset())
        ok, lines = await smoke(h, deps, marketable=False)  # a second run gets a fresh id
        assert ok, lines
        assert await h.status("A-20261009-XYZ-smoke-2") == "canceled"


async def test_the_gate_still_decides(tmp_path: Path) -> None:
    async with smoke_rig(tmp_path) as (h, deps):
        await h.halts.halt("A", "manual")
        ok, lines = await smoke(h, deps, marketable=False)
        assert not ok
        assert "risk gate: rejected R-005" in lines
        assert any(line.startswith("not submitted: rejected") for line in lines)
        assert await h.sim.open_orders() == []


async def test_outside_the_entry_window_nothing_is_sent(tmp_path: Path) -> None:
    clock = SimClock(datetime(2026, 10, 9, 9, 31, tzinfo=ET))
    async with smoke_rig(tmp_path, clock=clock) as (h, deps):
        ok, lines = await smoke(h, deps, marketable=False)
        assert not ok
        assert lines == ["run it inside the entry window, 09:35:03-15:30 ET, on a trading day"]
        assert h.sim.submits == []


async def test_a_pricey_symbol_is_refused(tmp_path: Path) -> None:
    async with smoke_rig(tmp_path, bid="60.00", ask="60.02") as (h, deps):
        ok, lines = await smoke(h, deps, marketable=True)
        assert not ok
        assert "XYZ trades at $60.00: pick a liquid symbol under $50" in lines


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    yield opened
    await opened.aclose()


async def test_first_use_seeds_the_ledger_from_a_flat_account(journal: Journal) -> None:
    clock = SimClock(datetime(2026, 10, 9, 10, 0, tzinfo=ET))
    ledger = SettlementLedger(journal, "A", clock)
    lines: list[str] = []
    await ensure_seeded(ledger, SimBroker(clock=clock, cash=D("1000.00")), lines.append)
    assert lines == ["seeded Book A's ledger with $1000.00 (first use)"]
    await ensure_seeded(ledger, SimBroker(clock=clock), lines.append)  # never twice
    assert len(lines) == 1


async def test_seeding_refuses_an_account_that_is_not_flat(journal: Journal) -> None:
    clock = SimClock(datetime(2026, 10, 9, 10, 0, tzinfo=ET))
    sim = SimBroker(clock=clock)
    sim.feed(Quote(symbol="XYZ", ts=clock.now(), bid=D("12.30"), bid_size=1, ask=D("12.32"),
                   ask_size=1))  # fmt: skip
    await sim.submit_entry_oto(  # one share held under a working stop leg
        OrderIntent(book="A", signal_id="x", symbol="XYZ", qty=1, entry_limit=D("12.35"),
                    stop_price=D("12.21"), client_order_id="manual-1",
                    day_size_multiplier=D(1))
    )  # fmt: skip
    with pytest.raises(LedgerError, match="flatten the account first"):
        await ensure_seeded(SettlementLedger(journal, "A", clock), sim, lambda _line: None)


def test_cli_refuses_a_day_without_a_session(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRest:
        async def calendar(self, start: date, end: date) -> list[dict[str, str]]:
            return nyse.calendar_rows(max(start, nyse.FIRST), min(end, nyse.LAST))

        async def __aenter__(self) -> "FakeRest":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    saturday = SimClock(datetime(2026, 10, 10, 11, 0, tzinfo=ET))
    monkeypatch.setattr(runtime, "RealClock", lambda: saturday)
    monkeypatch.setattr(runtime.AlpacaRest, "from_config", lambda _cfg, _clock: FakeRest())
    result = CliRunner().invoke(app, ["--root", str(repo), "smoke", "paper"])
    assert result.exit_code == 1
    assert "not a trading day" in result.output
