import asyncio
import json
import random
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tests.fixtures.fake_trading_ws import FakeTradingServer
from tests.helpers import set_yaml
from trader.config import load_config
from trader.core.clock import ET, SimClock
from trader.core.models import OrderIntent
from trader.data.alpaca_rest import TRADING_PAPER_URL
from trader.data.ratelimit import RateLimiter
from trader.execution.alpaca_broker import AlpacaBroker
from trader.execution.broker import (
    BrokerAuthError,
    BrokerDataError,
    BrokerError,
    DuplicateClientOrderId,
    OrderNotCancelable,
    OrderNotFound,
    OrderRejected,
    StreamResync,
    SubmitUncertain,
    TradeUpdate,
    stop_is_working,
)

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=ET)
ORDERS = f"{TRADING_PAPER_URL}/v2/orders"
D = Decimal


async def no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


@pytest.fixture
async def broker() -> AsyncIterator[AlpacaBroker]:
    server = FakeTradingServer()
    client = AlpacaBroker(
        "A",
        "PKTESTKEY",
        "test-secret",
        clock=SimClock(NOW),
        limiter=RateLimiter(100_000, capacity=1_000),
        connector=server.connect,
        sleep=no_sleep,
        rng=random.Random(0),
    )
    client.server = server  # type: ignore[attr-defined]
    yield client
    await client.aclose()


def order_json(**changes: Any) -> dict[str, Any]:
    """An order as Alpaca's REST and stream send it: numbers as strings."""
    base: dict[str, Any] = {
        "id": "61e69015-8549-4bfd-b9c3-01e75843f47d",
        "client_order_id": "A-20261009-F-entry-1",
        "created_at": "2026-10-09T13:45:00.123456789Z",
        "updated_at": "2026-10-09T13:45:00.2Z",
        "submitted_at": "2026-10-09T13:45:00.1Z",
        "filled_at": None,
        "symbol": "F",
        "asset_class": "us_equity",
        "qty": "40",
        "filled_qty": "0",
        "filled_avg_price": None,
        "order_class": "oto",
        "order_type": "limit",
        "type": "limit",
        "side": "buy",
        "position_intent": "buy_to_open",
        "time_in_force": "day",
        "limit_price": "12.35",
        "stop_price": None,
        "status": "new",
        "extended_hours": False,
        "legs": None,
        "group_id": "grp-1",
        "group_role": "entry",
    }
    return {**base, **changes}


def leg_json(**changes: Any) -> dict[str, Any]:
    return order_json(
        id="leg-1",
        client_order_id="0f7cd7b7-auto",
        type="stop",
        order_type="stop",
        side="sell",
        limit_price=None,
        stop_price="12.21",
        status="held",
        group_role="stop_loss",
        position_intent="sell_to_close",
        **changes,
    )


def intent() -> OrderIntent:
    return OrderIntent(
        book="A",
        signal_id="20261009-F-orb5",
        symbol="F",
        qty=40,
        entry_limit=D("12.35"),
        stop_price=D("12.21"),
        client_order_id="A-20261009-F-entry-1",
        day_size_multiplier=D("1.0"),
    )


# ---------------------------------------------------------------- reads


async def test_account_parses_strings_exactly(
    broker: AlpacaBroker, respx_mock: respx.MockRouter
) -> None:
    body = {
        "id": "x",
        "status": "ACTIVE",
        "cash": "1000.05",
        "equity": "1000.05",
        "buying_power": "1000.05",
        "multiplier": "1",
        "trading_blocked": False,
        "account_blocked": False,
        "shorting_enabled": False,
        "user_configurations": None,  # undocumented extras are ignored
    }
    respx_mock.get(f"{TRADING_PAPER_URL}/v2/account").mock(
        return_value=httpx.Response(200, json=body)
    )
    account = await broker.account()
    assert (account.cash, account.multiplier, account.is_paper) == (D("1000.05"), D(1), True)


async def test_positions_are_whole_shares(
    broker: AlpacaBroker, respx_mock: respx.MockRouter
) -> None:
    row = {"symbol": "F", "qty": "40", "qty_available": "0", "avg_entry_price": "12.3412",
           "current_price": "12.40", "side": "long"}  # fmt: skip
    respx_mock.get(f"{TRADING_PAPER_URL}/v2/positions").mock(
        return_value=httpx.Response(200, json=[row])
    )
    (position,) = await broker.positions()
    assert (position.qty, position.qty_available) == (40, 0)  # the stop holds every share
    assert position.avg_entry_price == D("12.3412")


async def test_a_fractional_position_is_an_anomaly(
    broker: AlpacaBroker, respx_mock: respx.MockRouter
) -> None:
    row = {"symbol": "F", "qty": "0.5", "qty_available": "0.5", "avg_entry_price": "12"}
    respx_mock.get(f"{TRADING_PAPER_URL}/v2/positions").mock(
        return_value=httpx.Response(200, json=[row])
    )
    with pytest.raises(BrokerDataError, match="fractional"):
        await broker.positions()


async def test_open_orders_list_legs_separately_in_eastern_time(
    broker: AlpacaBroker, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(ORDERS).mock(
        return_value=httpx.Response(200, json=[order_json(), leg_json()])
    )
    entry, leg = await broker.open_orders()
    params = route.calls.last.request.url.params
    # Not nested: a working leg must show up even once its entry has filled and closed.
    assert (params["status"], params["nested"], params["limit"]) == ("open", "false", "500")
    assert entry.submitted_at == datetime(2026, 10, 9, 9, 45, 0, 100000, tzinfo=ET)
    assert (leg.status, leg.stop_price, leg.group_id) == ("held", D("12.21"), entry.group_id)
    assert not stop_is_working(leg, entry)  # held until the entry fills completely
    filled = entry.model_copy(update={"status": "filled", "filled_qty": 40})
    assert stop_is_working(leg, filled)


async def test_an_order_can_carry_its_legs_nested(
    broker: AlpacaBroker, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{ORDERS}/o1").mock(
        return_value=httpx.Response(200, json=order_json(id="o1", legs=[leg_json()]))
    )
    entry = await broker.get_order("o1")
    assert entry.stop_leg is not None
    assert entry.stop_leg.group_role == "stop_loss"


async def test_orders_since_asks_for_every_status(
    broker: AlpacaBroker, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(ORDERS).mock(return_value=httpx.Response(200, json=[]))
    assert await broker.orders_since(NOW) == []
    params = route.calls.last.request.url.params
    assert (params["status"], params["after"], params["direction"]) == (
        "all",
        "2026-10-09T14:00:00Z",
        "asc",
    )


async def test_a_full_page_of_orders_is_refused(
    broker: AlpacaBroker, respx_mock: respx.MockRouter
) -> None:
    rows = [order_json(id=f"o{i}") for i in range(500)]
    respx_mock.get(ORDERS).mock(return_value=httpx.Response(200, json=rows))
    with pytest.raises(BrokerError, match="more than one page"):
        await broker.open_orders()


async def test_lookups_that_find_nothing(
    broker: AlpacaBroker, respx_mock: respx.MockRouter
) -> None:
    missing = httpx.Response(404, json={"code": 40410000, "message": "order not found"})
    route = respx_mock.get(f"{ORDERS}:by_client_order_id").mock(return_value=missing)
    assert await broker.order_by_client_id("A-20261009-F-entry-1") is None
    assert route.calls.last.request.url.params["client_order_id"] == "A-20261009-F-entry-1"
    respx_mock.get(f"{ORDERS}/nope").mock(return_value=missing)
    with pytest.raises(OrderNotFound):
        await broker.get_order("nope")


async def test_lookup_by_client_id_finds_the_order(
    broker: AlpacaBroker, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(f"{ORDERS}:by_client_order_id").mock(
        return_value=httpx.Response(200, json=order_json(status="filled", filled_qty="40",
                                                          filled_avg_price="12.3412"))
    )  # fmt: skip
    found = await broker.order_by_client_id("A-20261009-F-entry-1")
    assert found is not None
    assert (found.status, found.filled_qty, found.filled_avg_price) == ("filled", 40, D("12.3412"))


async def test_bad_keys_on_a_read(broker: AlpacaBroker, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{TRADING_PAPER_URL}/v2/account").mock(
        return_value=httpx.Response(401, text="<html>401 Authorization Required</html>")
    )
    with pytest.raises(BrokerAuthError):
        await broker.account()


# ---------------------------------------------------------------- submits


async def test_entry_is_sent_as_exact_strings(
    broker: AlpacaBroker, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(ORDERS).mock(
        return_value=httpx.Response(200, json=order_json(legs=[leg_json()]))
    )
    order = await broker.submit_entry_oto(intent())
    sent = json.loads(route.calls.last.request.content)
    assert sent["limit_price"] == "12.35"
    assert sent["stop_loss"] == {"stop_price": "12.21"}
    assert (sent["qty"], sent["order_class"], sent["extended_hours"]) == ("40", "oto", False)
    assert order.stop_leg is not None


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout("timed out"), httpx.Response(504, text="gateway timeout")]
)
async def test_an_unanswered_submit_is_uncertain_and_sent_once(
    broker: AlpacaBroker, respx_mock: respx.MockRouter, failure: httpx.Response | Exception
) -> None:
    route = respx_mock.post(ORDERS).mock(side_effect=[failure])
    with pytest.raises(SubmitUncertain):
        await broker.submit_entry_oto(intent())
    assert route.call_count == 1  # alpaca-py would have resent this; we never do (A§10)


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (422, {"code": 40010001, "message": "client_order_id must be unique"},
         DuplicateClientOrderId),
        (403, {"code": 40310000, "message": "insufficient buying power"}, OrderRejected),
        (403, {"message": "potential wash trade detected. use complex orders"}, OrderRejected),
        (422, {"code": 42210000, "message": "invalid limit_price 12.345. sub-penny increment"},
         OrderRejected),
    ],
)  # fmt: skip
async def test_definite_rejections_carry_alpacas_code(
    broker: AlpacaBroker,
    respx_mock: respx.MockRouter,
    status: int,
    body: dict[str, Any],
    error: type[OrderRejected],
) -> None:
    respx_mock.post(ORDERS).mock(return_value=httpx.Response(status, json=body))
    with pytest.raises(error) as info:
        await broker.submit_market_sell("F", 40, "A-20261009-F-exit-1")
    assert (info.value.status, info.value.code) == (status, body.get("code"))


async def test_bad_keys_on_a_submit(broker: AlpacaBroker, respx_mock: respx.MockRouter) -> None:
    respx_mock.post(ORDERS).mock(return_value=httpx.Response(401, text="<html>401</html>"))
    with pytest.raises(BrokerAuthError):
        await broker.submit_stop("F", 40, D("12.21"), "A-20261009-F-stop-1")


# ---------------------------------------------------------------- cancels


async def test_cancel_is_a_request_and_retries_when_the_server_is_busy(
    broker: AlpacaBroker, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.delete(f"{ORDERS}/o1").mock(
        side_effect=[httpx.Response(500, text="busy"), httpx.Response(204)]
    )
    assert await broker.cancel("o1") is None
    assert route.call_count == 2


async def test_cancel_refusals(broker: AlpacaBroker, respx_mock: respx.MockRouter) -> None:
    respx_mock.delete(f"{ORDERS}/done").mock(
        return_value=httpx.Response(422, json={"message": "order is already in filled state"})
    )
    with pytest.raises(OrderNotCancelable):
        await broker.cancel("done")
    respx_mock.delete(f"{ORDERS}/gone").mock(return_value=httpx.Response(404, json={}))
    with pytest.raises(OrderNotFound):
        await broker.cancel("gone")


# ---------------------------------------------------------------- the trade stream


async def test_trade_updates_start_with_a_resync_and_parse_fills(broker: AlpacaBroker) -> None:
    server: FakeTradingServer = broker.server  # type: ignore[attr-defined]
    events = broker.trade_updates()
    first = await asyncio.wait_for(anext(events), 1.0)
    assert isinstance(first, StreamResync)  # also after the first connection
    fill = {
        "event": "partial_fill",
        "execution_id": "e-1",
        "timestamp": "2026-10-09T13:45:01.5Z",
        "price": "12.34",
        "qty": "15",
        "position_qty": "15",
        "order": order_json(status="partially_filled", filled_qty="15", filled_avg_price="12.34"),
    }
    server.latest.push_update(fill)
    update = await asyncio.wait_for(anext(events), 1.0)
    assert isinstance(update, TradeUpdate)
    assert (update.event, update.qty, update.price, update.position_qty) == (
        "partial_fill",
        15,
        D("12.34"),
        15,
    )
    assert (update.order.filled_qty, update.execution_id) == (15, "e-1")
    server.latest.push_update({"event": "fill"})  # no order: unreadable
    assert isinstance(await asyncio.wait_for(anext(events), 1.0), StreamResync)


# ---------------------------------------------------------------- configuration


async def test_from_config_paper_only(repo: Path) -> None:
    cfg = load_config(repo)
    broker = AlpacaBroker.from_config(cfg, "B", SimClock(NOW))
    assert broker.name == "alpaca-paper:B"
    await broker.aclose()
    set_yaml(repo / "config" / "settings.yaml", "mode", "live")
    with pytest.raises(BrokerError, match="live gate"):
        AlpacaBroker.from_config(load_config(repo), "A", SimClock(NOW))


async def test_from_config_without_keys(repo: Path) -> None:
    (repo / ".env").write_text('CLAUDE_TOKEN_CREATED="2026-10-01"\n', encoding="utf-8")
    with pytest.raises(BrokerAuthError, match="ALPACA_PAPER_A"):
        AlpacaBroker.from_config(load_config(repo), "A", SimClock(NOW))
