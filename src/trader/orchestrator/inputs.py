"""The gate's inputs for a trigger (resolution 4 of the Phase 3 plan; DECISIONS D-044, D-048).

``prepare`` runs before the book's lock and may wait on the network:
- **Last price:** the trigger's own price for an arm-check trigger, else the hub's.
- **Quote age (R-016):** from the exchange timestamps of the hub's latest trade and quote. When
  that is missing or older than ``symbol_quote_max_age_s``, one REST snapshot refreshes it.
- **Broker equity (R-019):** cached per book, refreshed at most every 10 s.
- **The asset (R-003):** from the asset list read at boot.

``builder`` returns the gate-state builder, which runs under the book's lock and only assembles
values. A missing input is None, which makes its rule reject; nothing here raises.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

import structlog

from trader.books.base import PreparedInputs
from trader.config import BookName, RiskConfig
from trader.core.calendar import DayTimes
from trader.core.clock import Clock
from trader.core.events import Event
from trader.core.models import Asset, Quote, Signal, Snapshot, Trade, TriggerEvent
from trader.data.alpaca_rest import Feed
from trader.execution.broker import BrokerAdapter
from trader.execution.service import Exposure, GateStateBuilder
from trader.risk.breakers import Halts
from trader.risk.gate import GateState

log = structlog.get_logger(__name__)

EQUITY_TTL = timedelta(seconds=10)


class PriceHub(Protocol):
    def last_price(self, symbol: str, max_age_s: float = ...) -> Decimal | None: ...
    def last_trade(self, symbol: str) -> Trade | None: ...
    def last_quote(self, symbol: str) -> Quote | None: ...


class SnapshotMarket(Protocol):
    async def snapshots(self, symbols: Sequence[str], *, feed: Feed) -> dict[str, Snapshot]: ...


class LiveInputs:
    """``EntryInputs`` for run-day, replay and simulation alike."""

    def __init__(
        self,
        *,
        hub: PriceHub,
        market: SnapshotMarket,
        brokers: Mapping[BookName, BrokerAdapter],
        halts: Halts,
        clock: Clock,
        times: DayTimes,
        mode: Literal["paper", "live"],
        risk: RiskConfig,
        feed: Feed,
        events: Sequence[Event],
        assets: Mapping[str, Asset],
        kill_file: Path,
        hub_stale: Callable[[], bool],
    ) -> None:
        self._hub, self._market, self._brokers = hub, market, brokers
        self._halts, self._clock, self._times, self._mode = halts, clock, times, mode
        self._risk, self._feed, self._events = risk, feed, tuple(events)
        self._assets, self._kill_file, self._hub_stale = assets, kill_file, hub_stale
        self._equity: dict[BookName, tuple[Decimal, datetime]] = {}

    async def prepare(self, book: BookName, sig: Signal, ev: TriggerEvent) -> PreparedInputs:
        symbol = sig.symbol
        last = ev.price if ev.source == "arm_check" else self._hub.last_price(symbol)
        latest = (self._hub.last_trade(symbol), self._hub.last_quote(symbol))
        seen = [x.ts for x in latest if x is not None]
        if ev.source == "arm_check":
            seen.append(ev.ts)
        age = self._age(seen)
        if age is None or age > self._risk.symbol_quote_max_age_s:
            fresher = await self._snapshot_age(symbol)
            if fresher is not None and (age is None or fresher < age):
                age = fresher
        halts = tuple(h.reason for h in await self._halts.active(book))
        if self._kill_file.exists() and "kill_switch" not in halts:
            halts = (*halts, "kill_switch")
        return PreparedInputs(
            last_price=last,
            quote_age_s=age,
            broker_equity=await self._broker_equity(book),
            asset=self._assets.get(symbol),
            hub_stale=self._hub_stale(),
            halts=halts,
        )

    def builder(self, book: BookName, sig: Signal, prepared: PreparedInputs) -> GateStateBuilder:
        async def build(exposure: Exposure) -> GateState:
            halts = tuple(h.reason for h in await self._halts.active(book))
            return GateState(
                mode=self._mode,
                now=self._clock.now(),
                day=self._times,
                halts=halts,
                kill_switch=self._kill_file.exists(),
                exposure_symbols=exposure.symbols,
                entries_today=exposure.entries_today,
                symbols_entered_today=exposure.symbols_entered_today,
                available_cash=exposure.available_cash,
                equity_sod=exposure.equity_sod,
                realized_pnl_today=exposure.realized_pnl_today,
                open_risk=exposure.open_risk,
                ledger_equity=exposure.ledger_equity,
                broker_equity=prepared.broker_equity,
                asset=prepared.asset,
                symbol_halted=prepared.symbol_halted,
                corporate_action_today=prepared.corporate_action,
                quote_age_s=prepared.quote_age_s,
                hub_stale=prepared.hub_stale,
                client_order_id_used=exposure.client_order_id_used,
                open_order_for_signal=exposure.open_order_for_signal,
                unprotected_symbols=exposure.unprotected,
                events=self._events,
            )

        return build

    def _age(self, seen: Sequence[datetime]) -> float | None:
        if not seen:
            return None
        return max((self._clock.now() - max(seen)).total_seconds(), 0.0)

    async def _snapshot_age(self, symbol: str) -> float | None:
        try:
            snap = (await self._market.snapshots([symbol], feed=self._feed)).get(symbol)
        except Exception as exc:  # fail closed: R-016 rejects without a fresh quote
            log.warning("quote refresh failed", symbol=symbol, error=f"{type(exc).__name__}")
            return None
        if snap is None:
            return None
        return self._age([x.ts for x in (snap.latest_trade, snap.latest_quote) if x is not None])

    async def _broker_equity(self, book: BookName) -> Decimal | None:
        now = self._clock.now()
        cached = self._equity.get(book)
        if cached is not None and now - cached[1] < EQUITY_TTL:
            return cached[0]
        try:
            equity = (await self._brokers[book].account()).equity
        except Exception as exc:  # fail closed: R-019 rejects an unknown equity
            log.warning("broker equity unavailable", book=book, error=f"{type(exc).__name__}")
            return None
        self._equity[book] = (equity, now)
        return equity
