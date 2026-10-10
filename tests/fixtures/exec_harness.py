"""One book's ExecutionService on SimBroker, SimClock and a real journal, for scenario tests."""

import asyncio
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.fixtures.nyse import calendar_rows
from tests.helpers import REPO
from trader.config import FeesConfig, RiskConfig, Settings
from trader.core.calendar import Session, TradingCalendar, day_times, parse_alpaca_calendar
from trader.core.clock import ET, SimClock
from trader.core.models import Asset, OrderIntent, Quote, Trade
from trader.core.yamlfile import load_yaml
from trader.execution.records import Records
from trader.execution.service import EntryResult, ExecutionService, Exposure, TradingDay
from trader.execution.sim_broker import SimBroker
from trader.memory.journal import Journal
from trader.risk.breakers import Halts
from trader.risk.gate import GateState, RiskGate
from trader.risk.ledger import SettlementLedger

D = Decimal
DAY = date(2026, 10, 9)
T0 = datetime(2026, 10, 9, 10, 0, tzinfo=ET)
RISK = RiskConfig.model_validate(load_yaml(REPO / "config" / "risk.example.yaml"))
SETTINGS = Settings.model_validate(load_yaml(REPO / "config" / "settings.example.yaml"))
FEES = FeesConfig(
    sec_fee_per_million_sold=D("20.60"),
    finra_taf_per_share_sold=D("0.000195"),
    finra_taf_max_per_trade=D("9.79"),
)
FIRST, LAST = date(2026, 9, 1), date(2026, 12, 31)
CALENDAR = TradingCalendar(parse_alpaca_calendar(calendar_rows(FIRST, LAST)), FIRST, LAST)
TIMES = day_times(
    Session(
        date=DAY,
        open=datetime.combine(DAY, time(9, 30), ET),
        close=datetime.combine(DAY, time(16, 0), ET),
    ),
    entry_window_start=RISK.entry_window_start,
    last_entry=SETTINGS.schedule.last_entry,
    flatten=SETTINGS.schedule.flatten,
)
NEXT_OPEN = datetime(2026, 10, 12, 9, 30, tzinfo=ET)


class Prices:
    """The market-data hub's latest trades, as the service sees them."""

    def __init__(self) -> None:
        self.last: dict[str, Decimal] = {}

    def last_price(self, symbol: str) -> Decimal | None:
        return self.last.get(symbol)


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        clock: SimClock | None = None,
        sim_class: type[SimBroker] = SimBroker,
        **sim: Any,
    ) -> None:
        self.path = tmp_path / "trader.db"
        self.clock = clock or SimClock(T0)
        self.sim = sim_class(clock=self.clock, **sim)
        self.prices = Prices()
        self.gate = RiskGate(RISK, SETTINGS.strategy.universe)
        self.journal: Journal
        self.records: Records
        self.halts: Halts
        self.service: ExecutionService

    async def open(self, *, seed: bool = True) -> "Harness":
        self.journal = await Journal.open(self.path)
        self.records = Records(
            self.journal, book="A", calendar=CALENDAR, fees=FEES, clock=self.clock
        )
        self.halts = Halts(self.journal, self.clock)
        if seed:
            await SettlementLedger(self.journal, "A", self.clock).seed(
                D(1000), positions=0, open_orders=0
            )
        await self.start()
        return self

    async def start(self) -> None:
        self.service = ExecutionService(
            book="A",
            broker=self.sim,
            journal=self.journal,
            records=self.records,
            halts=self.halts,
            gate=self.gate,
            prices=self.prices,
            clock=self.clock,
            risk=RISK,
            day=TradingDay(times=TIMES, next_open=NEXT_OPEN),
        )
        await self.service.start()
        await self.settle()

    async def close(self) -> None:
        await self.service.stop()
        await self.journal.aclose()

    # ---------------------------------------------------------------- time

    async def settle(self) -> None:
        """Let the service catch up before time moves: no journal read or write in flight (they
        run on threads), every stream event taken, and many loop turns with nothing new."""
        quiet = 0
        for _ in range(100_000):
            if self.journal.idle and self.sim.idle:
                quiet += 1
                if quiet >= 50:
                    return
                await asyncio.sleep(0)
            else:
                quiet = 0
                await asyncio.sleep(0.001)  # a thread is working: give it real time
        raise AssertionError("the service never settled")

    async def advance(self, seconds: float, step: float = 0.25) -> None:
        end = self.clock.now() + timedelta(seconds=seconds)
        while self.clock.now() < end:
            await self.clock.advance_to(min(end, self.clock.now() + timedelta(seconds=step)))
            await self.settle()

    # ---------------------------------------------------------------- market

    def quote(self, bid: str, ask: str, symbol: str = "XYZ") -> None:
        self.sim.feed(Quote(symbol=symbol, ts=self.clock.now(), bid=D(bid), bid_size=5,
                            ask=D(ask), ask_size=5))  # fmt: skip

    def trade(self, price: str, symbol: str = "XYZ") -> None:
        self.sim.feed(Trade(symbol=symbol, ts=self.clock.now(), price=D(price), size=100))
        self.prices.last[symbol] = D(price)

    # ---------------------------------------------------------------- entries

    def intent(self, symbol: str = "XYZ", **changes: Any) -> OrderIntent:
        base = OrderIntent(
            book="A",
            signal_id=f"20261009-{symbol}-orb5",
            symbol=symbol,
            qty=40,
            entry_limit=D("12.35"),
            stop_price=D("12.21"),
            client_order_id=f"A-20261009-{symbol}-entry-1",
            day_size_multiplier=D("1"),
        )
        return base.model_copy(update=changes)

    async def state(self, exposure: Exposure, symbol: str = "XYZ") -> GateState:
        return GateState(
            mode="paper",
            now=self.clock.now(),
            day=TIMES,
            halts=tuple(h.reason for h in await self.halts.active("A")),
            exposure_symbols=exposure.symbols,
            entries_today=exposure.entries_today,
            symbols_entered_today=exposure.symbols_entered_today,
            available_cash=exposure.available_cash,
            equity_sod=exposure.equity_sod,
            realized_pnl_today=exposure.realized_pnl_today,
            open_risk=exposure.open_risk,
            ledger_equity=exposure.ledger_equity,
            broker_equity=(await self.sim.account()).equity,
            asset=Asset(
                symbol=symbol,
                name=symbol,
                exchange="NASDAQ",
                asset_class="us_equity",
                status="active",
                tradable=True,
            ),
            quote_age_s=1.0,
            client_order_id_used=exposure.client_order_id_used,
            open_order_for_signal=exposure.open_order_for_signal,
            unprotected_symbols=exposure.unprotected,
        )

    async def enter(self, symbol: str = "XYZ", **changes: Any) -> EntryResult:
        intent = self.intent(symbol, **changes)

        async def build(exposure: Exposure) -> GateState:
            return await self.state(exposure, intent.symbol)

        result = await self.service.try_enter(intent, build)
        await self.settle()
        return result

    # ---------------------------------------------------------------- journal views

    async def status(self, client_order_id: str) -> str | None:
        row = await self.records.order(client_order_id)
        return None if row is None else row.status

    async def incidents(self) -> list[tuple[str, str]]:
        rows = await self.journal.fetch_all("SELECT severity, code FROM incidents ORDER BY id")
        return [(r["severity"], r["code"]) for r in rows]

    async def held(self) -> dict[str, int]:
        return {p.symbol: p.qty for p in await self.sim.positions()}

    async def working_stops(self, symbol: str = "XYZ") -> list[tuple[int, Decimal | None, str]]:
        return [
            (o.qty - o.filled_qty, o.stop_price, o.status)
            for o in await self.sim.open_orders()
            if o.symbol == symbol and o.side == "sell" and o.type == "stop"
        ]
