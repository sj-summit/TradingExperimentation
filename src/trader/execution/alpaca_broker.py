"""AlpacaBroker: one book's Alpaca paper account, over REST plus its trade-updates stream.

httpx through ``AlpacaHttp`` rather than alpaca-py (DECISIONS D-030): order submits are never
resent blindly, prices go out as exact strings and come back as Decimal, and errors are told
apart by Alpaca's body ``code``, because a 403 can be a business rejection or bad keys. Paper
only: the live host waits for the Phase 7 gate.
"""

import asyncio
import contextlib
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Self

import httpx
import structlog

from trader.config import AppConfig, BookName
from trader.core.clock import Clock, parse_et_iso
from trader.core.models import OrderIntent
from trader.data.alpaca_rest import (
    TRADING_PAPER_URL,
    AlpacaAuthError,
    AlpacaError,
    AlpacaHttp,
    AlpacaUncertainError,
    rfc3339,
)
from trader.data.ratelimit import RateLimiter
from trader.data.ws import WsConnector, websockets_connector
from trader.execution.broker import (
    AccountSnapshot,
    BrokerAuthError,
    BrokerDataError,
    BrokerError,
    BrokerEvent,
    BrokerUnavailable,
    DuplicateClientOrderId,
    FeeActivity,
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
from trader.execution.orders import entry_oto_body, market_sell_body, stop_body
from trader.execution.trade_stream import TRADE_STREAM_PAPER_URL, AlpacaTradeStream

log = structlog.get_logger(__name__)

RATE_LIMIT_PER_MIN = 180  # Alpaca allows 200 trading-API calls per minute per account
ORDER_RESERVE = 4  # tokens polls never take: they stay for submits, cancels and sells (D-056)
ERR_DUPLICATE_CLIENT_ORDER_ID = 40010001
MAX_ORDERS_PER_PAGE = 500
ACTIVITIES_PER_PAGE = 100  # Alpaca's maximum page_size for account activities
MAX_ACTIVITY_PAGES = 50


class AlpacaBroker:
    def __init__(
        self,
        book: BookName,
        key_id: str,
        secret_key: str,
        *,
        clock: Clock,
        limiter: RateLimiter,
        client: httpx.AsyncClient | None = None,
        connector: WsConnector = websockets_connector,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.name = f"alpaca-paper:{book}"
        self._clock = clock
        self._http = AlpacaHttp(
            key_id,
            secret_key,
            clock=clock,
            limiters={TRADING_PAPER_URL: limiter},
            client=client,
            sleep=sleep,
            rng=rng,
            timeout_s=timeout_s,
        )
        self._events: asyncio.Queue[BrokerEvent] = asyncio.Queue()
        self._stream = AlpacaTradeStream(
            url=TRADE_STREAM_PAPER_URL,
            key_id=key_id,
            secret_key=secret_key,
            clock=clock,
            on_update=self._on_update,
            on_connected=lambda: self._events.put_nowait(StreamResync(at=clock.now())),
            connector=connector,
            sleep=sleep,
        )
        self._stream_task: asyncio.Task[None] | None = None

    @classmethod
    def from_config(cls, cfg: AppConfig, book: BookName, clock: Clock) -> Self:
        if cfg.settings.mode != "paper":
            raise BrokerError("live trading needs the Phase 7 live gate (RISK.md section 7)")
        key, secret = cfg.alpaca_credentials(book)
        if key is None or secret is None:
            prefix = cfg.settings.books.get(book).account_env_prefix
            raise BrokerAuthError(f"no keys for Book {book} in .env ({prefix}_*)")
        return cls(
            book,
            key.get_secret_value(),
            secret.get_secret_value(),
            clock=clock,
            limiter=RateLimiter(RATE_LIMIT_PER_MIN, reserve=ORDER_RESERVE),
        )

    async def aclose(self) -> None:
        await self._stream.stop()
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- reads

    async def account(self) -> AccountSnapshot:
        return parse_account(await self._read("/v2/account"))

    async def positions(self) -> list[Position]:
        return [parse_position(row) for row in await self._read("/v2/positions")]

    async def open_orders(self) -> list[Order]:
        # Not nested: a working stop leg whose entry has filled must still be listed, and
        # without nesting "each order in a group is listed separately" (Alpaca docs).
        params = {"status": "open", "nested": "false", "limit": MAX_ORDERS_PER_PAGE}
        return self._order_list(await self._read("/v2/orders", params))

    async def orders_since(self, after: datetime) -> list[Order]:
        params = {
            "status": "all",
            "after": rfc3339(after),
            "nested": "true",
            "direction": "asc",
            "limit": MAX_ORDERS_PER_PAGE,
        }
        return self._order_list(await self._read("/v2/orders", params))

    async def fee_activities(self, after: datetime) -> list[FeeActivity]:
        """``GET /v2/account/activities/FEE``, oldest first. A page holds at most 100; the
        next page starts after the last id seen (``page_token``). ``after`` goes in whole
        seconds, the form Alpaca documents. A row this can't read is a ``BrokerDataError``,
        so the caller's "fees unavailable" path takes it (D-055)."""
        found: list[FeeActivity] = []
        params: dict[str, Any] = {
            "after": rfc3339(after.replace(microsecond=0)),
            "direction": "asc",
            "page_size": ACTIVITIES_PER_PAGE,
        }
        for _ in range(MAX_ACTIVITY_PAGES):
            try:
                rows = await self._read("/v2/account/activities/FEE", params)
            except _NotFound:
                raise BrokerUnavailable("account activities: not found") from None
            if not isinstance(rows, list):
                raise BrokerUnavailable("unexpected account activities payload")
            try:
                found += [parse_fee(row) for row in rows]
                last = str(rows[-1]["id"]) if rows else ""
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise BrokerDataError(
                    f"unreadable fee activity: {type(exc).__name__}: {exc}"
                ) from None
            if len(rows) < ACTIVITIES_PER_PAGE:
                return found
            params = params | {"page_token": last}
        raise BrokerUnavailable(f"over {MAX_ACTIVITY_PAGES} pages of fees: refusing to guess")

    async def get_order(self, order_id: str) -> Order:
        try:
            return parse_order(await self._read(f"/v2/orders/{order_id}", {"nested": "true"}))
        except _NotFound:
            raise OrderNotFound(order_id) from None

    async def order_by_client_id(self, client_order_id: str) -> Order | None:
        params = {"client_order_id": client_order_id}
        try:
            return parse_order(await self._read("/v2/orders:by_client_order_id", params))
        except _NotFound:
            return None

    # ---------------------------------------------------------------- order mutations

    async def submit_entry_oto(self, intent: OrderIntent) -> Order:
        return await self._submit(entry_oto_body(intent))

    async def submit_stop(
        self,
        symbol: str,
        qty: int,
        stop_price: Decimal,
        client_order_id: str,
        *,
        tif: StopTif = "day",
    ) -> Order:
        return await self._submit(stop_body(symbol, qty, stop_price, client_order_id, tif))

    async def submit_market_sell(self, symbol: str, qty: int, client_order_id: str) -> Order:
        return await self._submit(market_sell_body(symbol, qty, client_order_id))

    async def cancel(self, order_id: str) -> None:
        """204 means the cancel was requested; the final status arrives later."""
        path = f"/v2/orders/{order_id}"
        try:
            await self._http.delete(TRADING_PAPER_URL, path)
        except AlpacaAuthError as exc:
            raise BrokerAuthError(exc.message) from None
        except AlpacaError as exc:
            if exc.status == 404:
                raise OrderNotFound(order_id) from None
            if exc.status == 422:
                raise OrderNotCancelable(f"{order_id}: {exc.message}") from None
            raise BrokerUnavailable(str(exc)) from None

    # ---------------------------------------------------------------- the trade stream

    async def trade_updates(self) -> AsyncIterator[BrokerEvent]:
        """Events from the account's stream, which starts on first use. A ``StreamResync``
        follows every connection, because missed events are never replayed."""
        if self._stream_task is None:
            self._stream_task = asyncio.create_task(self._stream.run(), name=self.name)
        while True:
            yield await self._events.get()

    def _on_update(self, data: Mapping[str, Any]) -> None:
        try:
            update = parse_trade_update(data)
        except (BrokerDataError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
            # Never drop a fill silently: make the owner resync this order over REST.
            log.error("unreadable trade update", error=f"{type(exc).__name__}: {exc}")
            self._events.put_nowait(StreamResync(at=self._clock.now()))
            return
        self._events.put_nowait(update)

    # ---------------------------------------------------------------- plumbing

    async def _read(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            return await self._http.get(TRADING_PAPER_URL, path, params)
        except AlpacaAuthError as exc:
            raise BrokerAuthError(exc.message) from None
        except AlpacaError as exc:
            if exc.status == 404:
                raise _NotFound(path) from None
            raise BrokerUnavailable(str(exc)) from None

    async def _submit(self, body: Mapping[str, Any]) -> Order:
        cid = body["client_order_id"]
        try:
            raw = await self._http.post(TRADING_PAPER_URL, "/v2/orders", body)
        except AlpacaUncertainError as exc:
            raise SubmitUncertain(f"{cid}: {exc.message}") from None
        except AlpacaAuthError as exc:
            raise BrokerAuthError(exc.message) from None
        except AlpacaError as exc:
            if exc.status == 422 and exc.code == ERR_DUPLICATE_CLIENT_ORDER_ID:
                raise DuplicateClientOrderId(
                    f"{cid}: {exc.message}", status=exc.status, code=exc.code
                ) from None
            raise OrderRejected(exc.message, status=exc.status, code=exc.code) from None
        order = parse_order(raw)
        log.info("order submitted", client_order_id=cid, order_id=order.id, status=order.status)
        return order

    @staticmethod
    def _order_list(rows: Any) -> list[Order]:
        if not isinstance(rows, list):
            raise BrokerUnavailable("unexpected orders payload")
        if len(rows) >= MAX_ORDERS_PER_PAGE:
            raise BrokerUnavailable(f"{len(rows)} orders: more than one page, refusing to guess")
        return [parse_order(row) for row in rows]


class _NotFound(Exception):
    """A 404 from a read; each caller decides what it means."""


# ---------------------------------------------------------------- Alpaca JSON -> models


def parse_order(raw: Mapping[str, Any]) -> Order:
    return Order(
        id=str(raw["id"]),
        client_order_id=str(raw.get("client_order_id") or ""),
        symbol=str(raw["symbol"]),
        side=raw["side"],
        type=str(raw.get("type") or raw.get("order_type") or ""),
        order_class=str(raw.get("order_class") or ""),
        time_in_force=str(raw.get("time_in_force") or ""),
        qty=_shares(raw.get("qty")),
        filled_qty=_shares(raw.get("filled_qty")),
        filled_avg_price=_decimal(raw.get("filled_avg_price")),
        limit_price=_decimal(raw.get("limit_price")),
        stop_price=_decimal(raw.get("stop_price")),
        status=str(raw["status"]),
        submitted_at=_timestamp(raw.get("submitted_at")),
        updated_at=_timestamp(raw.get("updated_at")),
        filled_at=_timestamp(raw.get("filled_at")),
        group_id=raw.get("group_id") or None,
        group_role=raw.get("group_role") or None,
        legs=tuple(parse_order(leg) for leg in raw.get("legs") or ()),
        raw={key: value for key, value in raw.items() if key != "legs"},
    )


def parse_position(raw: Mapping[str, Any]) -> Position:
    return Position(
        symbol=str(raw["symbol"]),
        qty=_shares(raw.get("qty")),
        qty_available=_shares(raw.get("qty_available", raw.get("qty"))),
        avg_entry_price=Decimal(str(raw["avg_entry_price"])),
        current_price=_decimal(raw.get("current_price")),
    )


def parse_account(raw: Mapping[str, Any]) -> AccountSnapshot:
    return AccountSnapshot(
        status=str(raw.get("status") or ""),
        cash=Decimal(str(raw["cash"])),
        equity=Decimal(str(raw["equity"])),
        buying_power=Decimal(str(raw["buying_power"])),
        multiplier=_decimal(raw.get("multiplier")),
        trading_blocked=bool(raw.get("trading_blocked")),
        account_blocked=bool(raw.get("account_blocked")),
        trade_suspended_by_user=bool(raw.get("trade_suspended_by_user")),
        shorting_enabled=bool(raw.get("shorting_enabled")),
        is_paper=True,  # paper keys only authenticate against the paper host (D-020)
    )


def parse_fee(raw: Mapping[str, Any]) -> FeeActivity:
    """One non-trade activity of type FEE; its kind of fee is ``activity_sub_type``."""
    return FeeActivity(
        id=str(raw["id"]),
        date=date.fromisoformat(str(raw["date"])[:10]),
        sub_type=str(raw.get("activity_sub_type") or ""),
        net_amount=Decimal(str(raw["net_amount"])),
        status=str(raw.get("status") or "executed"),
        symbol=raw.get("symbol") or None,
        created_at=_timestamp(raw.get("created_at")),
    )


def parse_trade_update(data: Mapping[str, Any]) -> TradeUpdate:
    qty, position_qty = data.get("qty"), data.get("position_qty")
    return TradeUpdate(
        event=str(data["event"]),
        order=parse_order(data["order"]),
        at=_timestamp(data.get("timestamp")),
        execution_id=data.get("execution_id") or None,
        qty=None if qty is None else _shares(qty),
        price=_decimal(data.get("price")),
        position_qty=None if position_qty is None else _shares(position_qty),
    )


def _shares(value: object) -> int:
    """A whole-share quantity sent as a string ("5", "-5"); None counts as 0."""
    if value is None:
        return 0
    amount = Decimal(str(value))
    if amount != amount.to_integral_value():
        raise BrokerDataError(f"fractional quantity {value}: this bot trades whole shares")
    return int(amount)


def _decimal(value: object) -> Decimal | None:
    return None if value is None or value == "" else Decimal(str(value))


def _timestamp(value: object) -> datetime | None:
    return parse_et_iso(str(value)) if value else None
