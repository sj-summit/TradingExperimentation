"""`trader smoke paper` (P2.7, HUMAN GATE G2): one 1-share OTO in Book A's paper account.

It goes through the real risk gate and ExecutionService, so it tests the whole order path.
- **Default:** a limit about 5% under the bid, which won't fill. Confirm it is accepted with
  its stop leg ``held``, cancel it, and confirm the entry and the leg are cancelled.
- **--marketable:** a limit at the ask + 0.2%. Confirm the fill, record the stop leg's status
  after the fill (it decides how D-032 treats ``held``), then flatten and confirm flat.

Smoke entries carry ``smoke-`` signal ids and ``smoke`` client order ids, so they never count as
strategy entries (D-035). The journal rows of the run are printed at the end.
"""

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from trader.core.clock import Clock, fmt_iso
from trader.core.events import Event
from trader.core.ids import client_order_id
from trader.core.models import Asset, OrderIntent, Snapshot
from trader.core.prices import ceil_tick, floor_tick
from trader.execution.broker import BrokerAdapter
from trader.execution.records import OrderRow, Records
from trader.execution.service import ExecutionService, Exposure, TradingDay
from trader.memory.journal import Journal
from trader.risk.breakers import Halts
from trader.risk.gate import GateState
from trader.risk.ledger import LedgerError, SettlementLedger

MAX_PRICE = Decimal(50)  # "a liquid sub-$50 stock" (PLAN.md P2.7)
RESTING_DISCOUNT = Decimal("0.95")  # far enough not to fill, short of a price-away check
MARKETABLE_PREMIUM = Decimal("1.002")
STOP_FRACTION = Decimal("0.98")
POLL = timedelta(milliseconds=250)
CONFIRM_WAIT = timedelta(seconds=10)
LEG_SETTLE = timedelta(seconds=2)
ACCEPTED = ("new", "accepted", "pending_new")
Echo = Callable[[str], None]


class NoPrices:
    """The smoke test runs without the market-data hub, so the service has no marks."""

    def last_price(self, symbol: str) -> Decimal | None:
        return None


class SmokeMarket(Protocol):
    """The slice of AlpacaRest the smoke test reads."""

    async def snapshots(
        self, symbols: Sequence[str], *, feed: Literal["iex", "sip"]
    ) -> dict[str, Snapshot]: ...
    async def asset(self, symbol: str) -> Asset | None: ...


@dataclass(frozen=True)
class SmokeDeps:
    service: ExecutionService
    broker: BrokerAdapter  # reads only: every order goes through the service
    records: Records
    halts: Halts
    journal: Journal
    clock: Clock
    market: SmokeMarket
    day: TradingDay
    mode: Literal["paper", "live"]
    feed: Literal["iex", "sip"]
    events: tuple[Event, ...]
    kill_file: Path


async def ensure_seeded(ledger: SettlementLedger, broker: BrokerAdapter, echo: Echo) -> None:
    """Seed Book A's ledger from the broker's cash on first use, only from a flat account."""
    if await ledger.seeded():
        return
    account = await broker.account()
    positions, orders = await broker.positions(), await broker.open_orders()
    try:
        await ledger.seed(account.cash, positions=len(positions), open_orders=len(orders))
    except LedgerError as exc:
        raise LedgerError(f"{exc}: flatten the account first (`trader flatten --book A`)") from None
    echo(f"seeded Book {ledger.book}'s ledger with ${account.cash} (first use)")


async def run_smoke(d: SmokeDeps, *, symbol: str, marketable: bool, echo: Echo) -> bool:
    """Run one smoke round trip; returns True when every check passed."""
    started = d.clock.now()
    times = d.day.times
    if not times.entry_start <= started <= times.last_entry:
        echo(
            f"run it inside the entry window, {times.entry_start:%H:%M:%S}-"
            f"{times.last_entry:%H:%M} ET, on a trading day"
        )
        return False
    snapshot = (await d.market.snapshots([symbol], feed=d.feed)).get(symbol)
    if snapshot is None or snapshot.latest_quote is None:
        echo(f"no quote for {symbol} on the {d.feed} feed")
        return False
    quote = snapshot.latest_quote
    last = snapshot.latest_trade.price if snapshot.latest_trade is not None else quote.ask
    if not Decimal(0) < last < MAX_PRICE:
        echo(f"{symbol} trades at ${last}: pick a liquid symbol under ${MAX_PRICE}")
        return False
    limit = (
        ceil_tick(quote.ask * MARKETABLE_PREMIUM)
        if marketable
        else floor_tick(quote.bid * RESTING_DISCOUNT)
    )
    stop = floor_tick(limit * STOP_FRACTION)
    cid = await _fresh_client_order_id(d.records, started.date(), symbol)
    intent = OrderIntent(
        book="A",
        signal_id=f"smoke-{started:%Y%m%d}-{symbol}",
        symbol=symbol,
        qty=1,
        entry_limit=limit,
        stop_price=stop,
        client_order_id=cid,
        day_size_multiplier=Decimal(1),
    )
    seen = [t for t in (quote.ts, snapshot.latest_trade.ts if snapshot.latest_trade else None) if t]
    quote_age = (started - max(seen)).total_seconds()
    asset = await d.market.asset(symbol)
    broker_equity = (await d.broker.account()).equity

    async def build(exposure: Exposure) -> GateState:
        return GateState(
            mode=d.mode,
            now=d.clock.now(),
            day=times,
            halts=tuple(h.reason for h in await d.halts.active("A")),
            kill_switch=d.kill_file.exists(),
            exposure_symbols=exposure.symbols,
            entries_today=exposure.entries_today,
            symbols_entered_today=exposure.symbols_entered_today,
            available_cash=exposure.available_cash,
            equity_sod=exposure.equity_sod,
            realized_pnl_today=exposure.realized_pnl_today,
            open_risk=exposure.open_risk,
            ledger_equity=exposure.ledger_equity,
            broker_equity=broker_equity,
            asset=asset,
            quote_age_s=quote_age,
            client_order_id_used=exposure.client_order_id_used,
            open_order_for_signal=exposure.open_order_for_signal,
            unprotected_symbols=exposure.unprotected,
            events=d.events,
        )

    kind = "marketable" if marketable else "resting"
    echo(f"{kind} 1 x {symbol}: bid {quote.bid} ask {quote.ask}; limit {limit}, stop {stop}")
    result = await d.service.try_enter(intent, build)
    check = result.check
    if check is not None:
        verdict = "approved" if check.approved else f"rejected {', '.join(check.violations)}"
        echo(f"risk gate: {verdict}")
    if result.outcome != "submitted":
        echo(f"not submitted: {result.outcome} {result.reason}".rstrip())
        await _show_journal(d, started, echo)
        return False
    ok = await (_fill_and_flatten(d, cid, echo) if marketable else _rest_and_cancel(d, cid, echo))
    await _show_journal(d, started, echo)
    echo("SMOKE PASSED" if ok else "SMOKE FAILED")
    return ok


async def _rest_and_cancel(d: SmokeDeps, cid: str, echo: Echo) -> bool:
    row = await _wait_for(d, cid, lambda r: r.broker_order_id is not None and r.status in ACCEPTED)
    if row is None or row.broker_order_id is None:
        echo("FAIL: the entry was never accepted")
        return False
    order = await d.broker.get_order(row.broker_order_id)
    leg = order.stop_leg
    echo(f"accepted: entry {order.status}, stop leg {leg.status if leg else 'MISSING'}")
    accepted = leg is not None and leg.status == "held"
    await d.service.cancel_entry(cid)
    done = await _wait_for(d, cid, lambda r: r.status == "canceled")
    after = await d.broker.get_order(row.broker_order_id)
    leg_after = after.stop_leg
    leg_status = leg_after.status if leg_after else "gone"
    echo(f"cancelled: entry {after.status}, stop leg {leg_status}")
    return accepted and done is not None and leg_status in ("canceled", "gone")


async def _fill_and_flatten(d: SmokeDeps, cid: str, echo: Echo) -> bool:
    row = await _wait_for(
        d, cid, lambda r: r.status == "filled", CONFIRM_WAIT + timedelta(seconds=2)
    )
    if row is None or row.broker_order_id is None:
        echo("FAIL: no fill within the entry timeout (the service cancels it)")
        return False
    await d.clock.sleep_until(d.clock.now() + LEG_SETTLE)
    order = await d.broker.get_order(row.broker_order_id)
    leg = order.stop_leg
    leg_status = leg.status if leg else "MISSING"
    echo(f"filled at {order.filled_avg_price}; stop leg after the fill: {leg_status}")
    echo("  (record the leg status for DECISIONS: it settles how D-032 treats 'held')")
    result = await d.service.flatten(purpose="manual")
    held = ", ".join(f"{s} {q}" for s, q in sorted(result.remaining.items()))
    echo("flattened: flat" if result.flat else f"FAIL: still held after flatten: {held}")
    return result.flat and leg is not None and leg_status in ("new", "accepted", "held")


async def _wait_for(
    d: SmokeDeps,
    cid: str,
    done: Callable[[OrderRow], bool],
    within: timedelta = CONFIRM_WAIT,
) -> OrderRow | None:
    """Poll the journal (the service keeps it current) until ``done`` or ``within``."""
    deadline = d.clock.now() + within
    while True:
        row = await d.records.order(cid)
        if row is not None and done(row):
            return row
        if d.clock.now() >= deadline:
            return None
        await d.clock.sleep_until(d.clock.now() + POLL)


async def _fresh_client_order_id(records: Records, day: date, symbol: str) -> str:
    attempt = 1
    while await records.order(cid := client_order_id("A", day, symbol, "smoke", attempt)):
        attempt += 1
    return cid


async def _show_journal(d: SmokeDeps, since: datetime, echo: Echo) -> None:
    """The run's journal rows: orders, fills, risk checks and ledger entries."""
    after = fmt_iso(since)
    sections: list[tuple[str, Callable[[], Awaitable[list[str]]]]] = [
        ("orders", lambda: _rows(d.journal,
            "SELECT client_order_id, side, type, qty, limit_price, stop_price, status "
            "FROM orders WHERE book = 'A' AND submitted_at >= ? ORDER BY submitted_at", after)),
        ("fills", lambda: _rows(d.journal,
            "SELECT id, side, qty, price, filled_at FROM fills WHERE book = 'A' "
            "AND filled_at >= ? ORDER BY filled_at", after)),
        ("risk_checks", lambda: _rows(d.journal,
            "SELECT signal_id, approved, violations_json FROM risk_checks WHERE book = 'A' "
            "AND checked_at >= ? ORDER BY id", after)),
        ("ledger", lambda: _rows(d.journal,
            "SELECT kind, amount, trade_date, settle_date, ref FROM ledger_entries "
            "WHERE book = 'A' AND created_at >= ? ORDER BY id", after)),
    ]  # fmt: skip
    for title, read in sections:
        echo(f"-- {title}")
        for line in await read() or ["(none)"]:
            echo(f"   {line}")


async def _rows(journal: Journal, sql: str, after: str) -> list[str]:
    return [
        json.dumps(dict(zip(row.keys(), tuple(row), strict=True)), default=str)
        for row in await journal.fetch_all(sql, (after,))
    ]
