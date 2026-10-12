"""Recording one book's broker traffic for replay (DECISIONS D-047).

``RecordingBroker`` wraps a ``BrokerAdapter``: every call is written to ``broker-<book>.jsonl.gz``
with its arguments, its result or error, and its start and end receive times; every trade update
is written with its receive time. Results are our parsed models without Alpaca's raw payloads,
which keeps account details out of the recording. ``ReplayBroker`` (P3.8b) reads them back.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from trader.core.clock import Clock, fmt_iso
from trader.core.models import OrderIntent
from trader.execution.broker import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerError,
    BrokerEvent,
    Order,
    Position,
    StopTif,
    TradeUpdate,
)

Sink = Callable[[dict[str, Any]], None]
NO_RAW: Any = {"raw": True, "legs": {"__all__": {"raw"}}}


def encode_order(order: Order) -> dict[str, Any]:
    dumped: dict[str, Any] = order.model_dump(mode="json", exclude=NO_RAW)
    return dumped


def encode_event(event: BrokerEvent) -> dict[str, Any]:
    if isinstance(event, TradeUpdate):
        return {"type": "update", **event.model_dump(mode="json", exclude={"order": NO_RAW})}
    return {"type": "resync", "at": fmt_iso(event.at)}


def encode_error(exc: BrokerError) -> dict[str, Any]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "reason": getattr(exc, "reason", None),
        "status": getattr(exc, "status", None),
        "code": getattr(exc, "code", None),
    }


def _encode(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, Order):
        return encode_order(result)
    if isinstance(result, (AccountSnapshot, Position)):
        return result.model_dump(mode="json")
    if isinstance(result, list):
        return [_encode(item) for item in result]
    return result


class RecordingBroker:
    """A BrokerAdapter that records everything it says and hears."""

    def __init__(self, inner: BrokerAdapter, clock: Clock, sink: Sink) -> None:
        self._inner, self._clock, self._sink = inner, clock, sink
        self.name = inner.name

    async def account(self) -> AccountSnapshot:
        return await self._call("account", {}, self._inner.account)

    async def positions(self) -> list[Position]:
        return await self._call("positions", {}, self._inner.positions)

    async def open_orders(self) -> list[Order]:
        return await self._call("open_orders", {}, self._inner.open_orders)

    async def get_order(self, order_id: str) -> Order:
        return await self._call("get_order", {"order_id": order_id},
                                lambda: self._inner.get_order(order_id))  # fmt: skip

    async def order_by_client_id(self, client_order_id: str) -> Order | None:
        return await self._call(
            "order_by_client_id",
            {"client_order_id": client_order_id},
            lambda: self._inner.order_by_client_id(client_order_id),
        )

    async def orders_since(self, after: datetime) -> list[Order]:
        return await self._call("orders_since", {"after": fmt_iso(after)},
                                lambda: self._inner.orders_since(after))  # fmt: skip

    async def submit_entry_oto(self, intent: OrderIntent) -> Order:
        return await self._call(
            "submit_entry_oto",
            {"client_order_id": intent.client_order_id, "intent": intent.model_dump(mode="json")},
            lambda: self._inner.submit_entry_oto(intent),
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
        args = {"client_order_id": client_order_id, "symbol": symbol, "qty": qty,
                "stop_price": str(stop_price), "tif": tif}  # fmt: skip
        return await self._call(
            "submit_stop",
            args,
            lambda: self._inner.submit_stop(symbol, qty, stop_price, client_order_id, tif=tif),
        )

    async def submit_market_sell(self, symbol: str, qty: int, client_order_id: str) -> Order:
        return await self._call(
            "submit_market_sell",
            {"client_order_id": client_order_id, "symbol": symbol, "qty": qty},
            lambda: self._inner.submit_market_sell(symbol, qty, client_order_id),
        )

    async def cancel(self, order_id: str) -> None:
        await self._call("cancel", {"order_id": order_id}, lambda: self._inner.cancel(order_id))

    async def trade_updates(self) -> AsyncIterator[BrokerEvent]:
        async for event in self._inner.trade_updates():
            self._sink({"method": "trade_update", "rx": fmt_iso(self._clock.now()),
                        "event": encode_event(event)})  # fmt: skip
            yield event

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def _call[T](
        self, method: str, args: Mapping[str, Any], call: Callable[[], Awaitable[T]]
    ) -> T:
        started = self._clock.now()
        record: dict[str, Any] = {"method": method, "args": dict(args),
                                  "start_rx": fmt_iso(started)}  # fmt: skip
        try:
            result = await call()
        except BrokerError as exc:
            record |= {"end_rx": fmt_iso(self._clock.now()), "error": encode_error(exc)}
            self._sink(record)
            raise
        record |= {"end_rx": fmt_iso(self._clock.now()), "result": _encode(result)}
        self._sink(record)
        return result
