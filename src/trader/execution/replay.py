"""Replaying one book's recorded broker traffic (P3.8b; DECISIONS D-047, D-049).

``ReplayBroker`` answers a replayed day's calls from ``broker-<book>.jsonl.gz``, which
``RecordingBroker`` wrote:
- **Submits** match by client order id, and **cancels** by order id (repeats in order).
- **Reads** (account, positions, open orders, one order, an order by client id, orders since)
  get the recorded answer to the same call that started nearest to now, unused answers first.
  A live engine spends a few milliseconds between calls that a SimClock doesn't, so a replay
  runs slightly early; and calls made at one instant (a zero-latency recording) are told apart
  by their order. "The latest answer at or before now" got both wrong (D-049).
- **Trade updates** arrive at their receive times.

Each answer comes after its recorded duration (a zero duration doesn't even yield, as the
recorded call didn't), and a recorded error is raised again. A call the recording doesn't have
goes to the ``fallback`` SimBroker and counts as a divergence: the replay then no longer matches
the day, and says so.
"""

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn

from trader.core.clock import Clock, fmt_iso, parse_et_iso
from trader.core.models import OrderIntent
from trader.data.recorder import read_jsonl
from trader.execution.broker import (
    AccountSnapshot,
    BrokerAuthError,
    BrokerDataError,
    BrokerError,
    BrokerEvent,
    BrokerUnavailable,
    DuplicateClientOrderId,
    Order,
    OrderNotCancelable,
    OrderNotFound,
    OrderRejected,
    Position,
    StopTif,
    StreamResync,
    SubmitUncertain,
    TradeUpdate,
)
from trader.execution.sim_broker import SimBroker

_REJECTIONS: dict[str, type[OrderRejected]] = {
    "OrderRejected": OrderRejected,
    "DuplicateClientOrderId": DuplicateClientOrderId,
}
_FAILURES: dict[str, type[BrokerError]] = {
    "BrokerError": BrokerError,
    "BrokerAuthError": BrokerAuthError,
    "BrokerUnavailable": BrokerUnavailable,
    "BrokerDataError": BrokerDataError,
    "SubmitUncertain": SubmitUncertain,
    "OrderNotFound": OrderNotFound,
    "OrderNotCancelable": OrderNotCancelable,
}
_SUBMITS = ("submit_entry_oto", "submit_stop", "submit_market_sell")
_READ_KEYS: dict[str, str | None] = {
    "account": None,
    "positions": None,
    "open_orders": None,
    "get_order": "order_id",
    "order_by_client_id": "client_order_id",
    "orders_since": "after",
}


def decode_event(data: Mapping[str, Any]) -> BrokerEvent:
    if data.get("type") == "resync":
        return StreamResync(at=parse_et_iso(str(data["at"])))
    return TradeUpdate.model_validate({k: v for k, v in data.items() if k != "type"})


def _raise(error: Mapping[str, Any]) -> NoReturn:
    kind = str(error.get("type"))
    if kind in _REJECTIONS:
        reason = str(error.get("reason") or error.get("message") or "")
        raise _REJECTIONS[kind](
            reason, status=int(error.get("status") or 0), code=error.get("code")
        )
    raise _FAILURES.get(kind, BrokerError)(str(error.get("message") or kind))


class ReplayBroker:
    """A ``BrokerAdapter`` over one book's recording."""

    def __init__(
        self, records: Iterable[Mapping[str, Any]], clock: Clock, *, fallback: SimBroker
    ) -> None:
        self._clock, self._fallback = clock, fallback
        self.name = f"replay:{fallback.name}"
        self._submits: dict[str, Mapping[str, Any]] = {}
        self._cancels: dict[str, deque[Mapping[str, Any]]] = defaultdict(deque)
        self._reads: dict[str, list[tuple[datetime, Mapping[str, Any]]]] = defaultdict(list)
        self._used: set[int] = set()  # id() of read records already served
        self._updates: list[tuple[datetime, BrokerEvent]] = []
        self.divergences: list[str] = []
        for record in records:
            method = str(record["method"])
            if method == "trade_update":
                self._updates.append((parse_et_iso(record["rx"]), decode_event(record["event"])))
            elif method in _SUBMITS:
                self._submits.setdefault(str(record["args"]["client_order_id"]), record)
            elif method == "cancel":
                self._cancels[str(record["args"]["order_id"])].append(record)
            elif method in _READ_KEYS:
                key = _read_key(method, record["args"])
                self._reads[key].append((parse_et_iso(record["start_rx"]), record))
        for found in self._reads.values():
            found.sort(key=lambda item: item[0])
        self._updates.sort(key=lambda item: item[0])

    @classmethod
    def from_file(cls, path: Path, clock: Clock, *, fallback: SimBroker) -> "ReplayBroker":
        return cls(read_jsonl(path) if path.exists() else [], clock, fallback=fallback)

    # ---------------------------------------------------------------- reads

    async def account(self) -> AccountSnapshot:
        result: AccountSnapshot = await self._read("account", {}, self._fallback.account)
        return result

    async def positions(self) -> list[Position]:
        result: list[Position] = await self._read("positions", {}, self._fallback.positions)
        return result

    async def open_orders(self) -> list[Order]:
        result: list[Order] = await self._read("open_orders", {}, self._fallback.open_orders)
        return result

    async def get_order(self, order_id: str) -> Order:
        result: Order = await self._read(
            "get_order", {"order_id": order_id}, lambda: self._fallback.get_order(order_id)
        )
        return result

    async def order_by_client_id(self, client_order_id: str) -> Order | None:
        result: Order | None = await self._read(
            "order_by_client_id",
            {"client_order_id": client_order_id},
            lambda: self._fallback.order_by_client_id(client_order_id),
        )
        return result

    async def orders_since(self, after: datetime) -> list[Order]:
        result: list[Order] = await self._read(
            "orders_since", {"after": fmt_iso(after)}, lambda: self._fallback.orders_since(after)
        )
        return result

    # ---------------------------------------------------------------- mutations

    async def submit_entry_oto(self, intent: OrderIntent) -> Order:
        return await self._submit(
            intent.client_order_id, lambda: self._fallback.submit_entry_oto(intent)
        )

    async def submit_stop(
        self,
        symbol: str,
        qty: int,
        stop_price: Decimal,
        client_order_id: str,
        *,
        tif: StopTif = "day",
    ) -> Order:
        return await self._submit(
            client_order_id,
            lambda: self._fallback.submit_stop(symbol, qty, stop_price, client_order_id, tif=tif),
        )

    async def submit_market_sell(self, symbol: str, qty: int, client_order_id: str) -> Order:
        return await self._submit(
            client_order_id,
            lambda: self._fallback.submit_market_sell(symbol, qty, client_order_id),
        )

    async def cancel(self, order_id: str) -> None:
        recorded = self._cancels.get(order_id)
        if not recorded:
            self.divergences.append(f"cancel {order_id}: not in the recording")
            await self._fallback.cancel(order_id)
            return
        await self._answer(recorded.popleft())

    async def trade_updates(self) -> AsyncIterator[BrokerEvent]:
        for rx, event in self._updates:
            await self._clock.sleep_until(rx)
            yield event
        await asyncio.Event().wait()  # the recording is over: nothing more arrives

    async def aclose(self) -> None:
        await self._fallback.aclose()

    # ---------------------------------------------------------------- plumbing

    async def _submit(self, cid: str, fallback: Callable[[], Awaitable[Order]]) -> Order:
        record = self._submits.pop(cid, None)
        if record is None:
            self.divergences.append(f"submit {cid}: not in the recording")
            return await fallback()
        result: Order = await self._answer(record)
        return result

    async def _read(
        self, method: str, args: Mapping[str, Any], fallback: Callable[[], Awaitable[Any]]
    ) -> Any:
        found = self._reads.get(_read_key(method, args))
        if not found:
            self.divergences.append(f"{method} {dict(args)}: not in the recording")
            return await fallback()
        now = self._clock.now()
        _, record = min(
            found,
            key=lambda item: (abs(item[0] - now), id(item[1]) in self._used, item[0]),
        )
        self._used.add(id(record))
        return await self._answer(record)

    async def _answer(self, record: Mapping[str, Any]) -> Any:
        took = parse_et_iso(record["end_rx"]) - parse_et_iso(record["start_rx"])
        if took > timedelta(0):
            await self._clock.sleep_until(self._clock.now() + took)
        if "error" in record:
            _raise(record["error"])
        return _decode(str(record["method"]), record.get("result"))


def _read_key(method: str, args: Mapping[str, Any]) -> str:
    name = _READ_KEYS[method]
    return method if name is None else f"{method}:{args[name]}"


def _decode(method: str, data: Any) -> Any:
    if method == "account":
        return AccountSnapshot.model_validate(data)
    if method == "positions":
        return [Position.model_validate(p) for p in data]
    if method in ("open_orders", "orders_since"):
        return [Order.model_validate(o) for o in data]
    if method == "order_by_client_id":
        return None if data is None else Order.model_validate(data)
    if method == "cancel":
        return None
    return Order.model_validate(data)
