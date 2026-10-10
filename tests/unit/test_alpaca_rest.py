import asyncio
import json
import random
from collections.abc import AsyncIterator
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from tests.helpers import FAKE_SECRETS, write_env
from trader.config import load_config
from trader.core.clock import ET, SimClock
from trader.data.alpaca_rest import (
    DATA_URL,
    TRADING_PAPER_URL,
    AlpacaAuthError,
    AlpacaError,
    AlpacaHttp,
    AlpacaRest,
    AlpacaUncertainError,
    SipRecentError,
)
from trader.data.ratelimit import RateLimiter

NOW = datetime(2026, 10, 9, 8, 0, tzinfo=ET)
KEY, SECRET = "PKTESTKEY0000000001", "test-secret-value-000000000000000"
BARS = f"{DATA_URL}/v2/stocks/bars"


class FakeTime:
    """Monotonic seconds that only move when the client or a limiter sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
async def rest(fake_time: FakeTime) -> AsyncIterator[AlpacaRest]:
    def roomy() -> RateLimiter:
        return RateLimiter(
            100_000, capacity=1_000, monotonic=fake_time.monotonic, sleep=fake_time.sleep
        )

    client = AlpacaRest(
        KEY,
        SECRET,
        clock=SimClock(NOW),
        data_limiter=roomy(),
        trading_limiter=roomy(),
        batch_size=2,
        sleep=fake_time.sleep,
        rng=random.Random(0),
    )
    yield client
    await client.aclose()


def raw_bar(t: str, price: str = "10.10", volume: int = 1000) -> str:
    """A bar as Alpaca's JSON text, so prices stay decimal literals."""
    return (
        f'{{"t":"{t}","o":{price},"h":{price},"l":{price},"c":{price},'
        f'"v":{volume},"n":5,"vw":{price}}}'
    )


def bars_page(bars: dict[str, list[str]], token: str | None = None) -> httpx.Response:
    body = ",".join(f'"{s}":[{",".join(rows)}]' for s, rows in bars.items())
    next_token = json.dumps(token)
    return httpx.Response(200, content=f'{{"bars":{{{body}}},"next_page_token":{next_token}}}')


# ---------------------------------------------------------------- bars


async def test_bars_sends_auth_and_params(rest: AlpacaRest, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(BARS).mock(return_value=bars_page({"SPY": []}))
    start = datetime(2026, 9, 25, tzinfo=ET)
    await rest.bars(["SPY"], "1Day", start, feed="sip", adjustment="split")
    request = route.calls.last.request
    assert request.headers["APCA-API-KEY-ID"] == KEY
    assert request.headers["APCA-API-SECRET-KEY"] == SECRET
    params = request.url.params
    assert params["symbols"] == "SPY"
    assert params["timeframe"] == "1Day"
    assert params["feed"] == "sip"
    assert params["adjustment"] == "split"
    assert params["limit"] == "10000"
    assert params["start"] == "2026-09-25T04:00:00Z"
    assert "end" not in params  # free-plan SIP: let the server default to now - 15 min


async def test_bars_follow_pages_and_keep_order(
    rest: AlpacaRest, respx_mock: respx.MockRouter
) -> None:
    mon, tue, wed = "2026-10-06T04:00:00Z", "2026-10-07T04:00:00Z", "2026-10-08T04:00:00Z"
    first = bars_page({"AAPL": [raw_bar(mon), raw_bar(tue)]}, token="t1")
    second = bars_page({"AAPL": [raw_bar(wed)], "MSFT": [raw_bar(wed)]})
    route = respx_mock.get(BARS).mock(side_effect=[first, second])
    result = await rest.bars(["AAPL", "MSFT"], "1Day", datetime(2026, 10, 6, tzinfo=ET), feed="sip")
    days = [b.ts.date() for b in result["AAPL"]]
    assert days == [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)]
    assert len(result["MSFT"]) == 1
    assert route.calls[1].request.url.params["page_token"] == "t1"


async def test_bars_batch_symbols_without_duplicates(
    rest: AlpacaRest, respx_mock: respx.MockRouter
) -> None:
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        symbols = request.url.params["symbols"]
        seen.append(symbols)
        return bars_page({s: [raw_bar("2026-10-08T13:30:00Z")] for s in symbols.split(",")})

    respx_mock.get(BARS).mock(side_effect=answer)
    result = await rest.bars(["A", "B", "C", "A"], "5Min", NOW, feed="iex")
    assert sorted(seen) == ["A,B", "C"]  # batch_size=2
    assert sorted(result) == ["A", "B", "C"]


async def test_bar_prices_are_exact_and_times_are_eastern(
    rest: AlpacaRest, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(BARS).mock(
        return_value=bars_page({"XYZ": [raw_bar("2026-10-08T13:30:00.123456789Z", "0.1")]})
    )
    (bar,) = (await rest.bars(["XYZ"], "5Min", NOW, feed="iex"))["XYZ"]
    assert bar.open == Decimal("0.1")
    assert bar.ts.tzinfo is ET
    assert bar.ts.time() == time(9, 30, 0, 123456)  # a bar is stamped with its start
    assert bar.volume == 1000


# ---------------------------------------------------------------- retries and errors


async def test_429_waits_for_the_rate_limit_reset(
    rest: AlpacaRest, fake_time: FakeTime, respx_mock: respx.MockRouter
) -> None:
    reset = {"X-RateLimit-Reset": str(int(NOW.timestamp()) + 7)}
    limited = httpx.Response(429, json={"message": "too many requests"}, headers=reset)
    ok = bars_page({"SPY": [raw_bar("2026-10-08T04:00:00Z")]})
    respx_mock.get(BARS).mock(side_effect=[limited, ok])
    result = await rest.bars(["SPY"], "1Day", NOW, feed="sip")
    assert len(result["SPY"]) == 1
    assert max(fake_time.sleeps) >= 7


async def test_server_errors_give_up_after_three_retries(
    rest: AlpacaRest, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(BARS).mock(return_value=httpx.Response(503, text="unavailable"))
    with pytest.raises(AlpacaError) as info:
        await rest.bars(["SPY"], "1Day", NOW, feed="sip")
    assert info.value.status == 503
    assert route.call_count == 4


async def test_network_errors_retry_then_fail(
    rest: AlpacaRest, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(BARS).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(AlpacaError, match="ConnectError") as info:
        await rest.bars(["SPY"], "1Day", NOW, feed="sip")
    assert info.value.status == 0
    assert route.call_count == 4


async def test_html_401_is_an_auth_error_without_secrets(
    rest: AlpacaRest, respx_mock: respx.MockRouter
) -> None:
    page = "<html><head><title>401 Authorization Required</title></head><body>nginx</body></html>"
    route = respx_mock.get(BARS).mock(return_value=httpx.Response(401, text=page))
    with pytest.raises(AlpacaAuthError) as info:
        await rest.bars(["SPY"], "1Day", NOW, feed="sip")
    assert "401 Authorization Required" in info.value.message
    assert KEY not in str(info.value)
    assert SECRET not in str(info.value)
    assert route.call_count == 1  # never retried


async def test_recent_sip_403_has_its_own_error(
    rest: AlpacaRest, respx_mock: respx.MockRouter
) -> None:
    body = {"code": 42210000, "message": "subscription does not permit querying recent SIP data"}
    respx_mock.get(BARS).mock(return_value=httpx.Response(403, json=body))
    with pytest.raises(SipRecentError):
        await rest.bars(["SPY"], "1Min", NOW, NOW, feed="sip")


async def test_non_json_success_is_an_error(rest: AlpacaRest, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(BARS).mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(AlpacaError, match="not JSON"):
        await rest.bars(["SPY"], "1Day", NOW, feed="sip")


# ---------------------------------------------------------------- AlpacaHttp: POST and DELETE

ORDERS = f"{TRADING_PAPER_URL}/v2/orders"


@pytest.fixture
def http(fake_time: FakeTime) -> AlpacaHttp:
    roomy = RateLimiter(
        100_000, capacity=1_000, monotonic=fake_time.monotonic, sleep=fake_time.sleep
    )
    return AlpacaHttp(
        KEY,
        SECRET,
        clock=SimClock(NOW),
        limiters={TRADING_PAPER_URL: roomy},
        sleep=fake_time.sleep,
        rng=random.Random(0),
    )


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("timed out"), httpx.ConnectError("reset"), httpx.Response(503, text="x")],
)
async def test_a_post_without_a_definite_answer_is_uncertain_and_never_resent(
    http: AlpacaHttp, respx_mock: respx.MockRouter, failure: httpx.Response | Exception
) -> None:
    route = respx_mock.post(ORDERS).mock(side_effect=[failure])
    with pytest.raises(AlpacaUncertainError, match="outcome unknown"):
        await http.post(TRADING_PAPER_URL, "/v2/orders", {"symbol": "F"})
    assert route.call_count == 1


async def test_a_post_is_resent_after_a_429_which_alpaca_answers_before_acting(
    http: AlpacaHttp, respx_mock: respx.MockRouter
) -> None:
    limited = httpx.Response(429, json={"message": "too many requests"})
    route = respx_mock.post(ORDERS).mock(
        side_effect=[limited, httpx.Response(200, json={"id": "o"})]
    )
    assert await http.post(TRADING_PAPER_URL, "/v2/orders", {"symbol": "F"}) == {"id": "o"}
    assert route.call_count == 2


async def test_a_rejected_post_carries_alpacas_error_code(
    http: AlpacaHttp, respx_mock: respx.MockRouter
) -> None:
    body = {"code": 40310000, "message": "insufficient buying power"}
    respx_mock.post(ORDERS).mock(return_value=httpx.Response(403, json=body))
    with pytest.raises(AlpacaError) as info:
        await http.post(TRADING_PAPER_URL, "/v2/orders", {"symbol": "F"})
    assert (info.value.status, info.value.code) == (403, 40310000)
    assert not isinstance(info.value, AlpacaUncertainError)


async def test_delete_retries_and_204_has_no_body(
    http: AlpacaHttp, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.delete(f"{ORDERS}/o1").mock(
        side_effect=[httpx.Response(503, text="busy"), httpx.Response(204)]
    )
    assert await http.delete(TRADING_PAPER_URL, "/v2/orders/o1") is None
    assert route.call_count == 2


# ---------------------------------------------------------------- other endpoints


async def test_movers(rest: AlpacaRest, respx_mock: respx.MockRouter) -> None:
    url = f"{DATA_URL}/v1beta1/screener/stocks/movers"
    gainers = {
        "gainers": [{"symbol": "XYZ", "percent_change": 145.56, "change": 1.5, "price": 2.53}],
        "losers": [],
        "market_type": "stocks",
        "last_updated": "2026-10-08T20:00:00.123456789Z",
    }
    respx_mock.get(url).mock(return_value=httpx.Response(200, json=gainers))
    (mover,) = await rest.movers(top=5) or []
    assert (mover.symbol, mover.percent_change, mover.price) == ("XYZ", 145.56, Decimal("2.53"))


@pytest.mark.parametrize("status", [403, 404])
async def test_movers_without_access_is_none(
    rest: AlpacaRest, respx_mock: respx.MockRouter, status: int
) -> None:
    url = f"{DATA_URL}/v1beta1/screener/stocks/movers"
    respx_mock.get(url).mock(return_value=httpx.Response(status, json={"message": "forbidden"}))
    assert await rest.movers() is None


async def test_movers_with_bad_keys_still_raises(
    rest: AlpacaRest, respx_mock: respx.MockRouter
) -> None:
    url = f"{DATA_URL}/v1beta1/screener/stocks/movers"
    respx_mock.get(url).mock(return_value=httpx.Response(401, text="<html>401</html>"))
    with pytest.raises(AlpacaAuthError):
        await rest.movers()


async def test_splits_parse_and_paginate(rest: AlpacaRest, respx_mock: respx.MockRouter) -> None:
    url = f"{DATA_URL}/v1/corporate-actions"
    forward = {
        "id": "1",
        "symbol": "SRE",
        "cusip": "x",
        "new_rate": 2,
        "old_rate": 1,
        "process_date": "2026-10-05",
        "ex_date": "2026-10-05",
    }
    reverse = {
        "id": "2",
        "symbol": "MNTS",
        "old_cusip": "a",
        "new_cusip": "b",
        "new_rate": 1,
        "old_rate": 50,
        "process_date": "2026-10-07",
        "ex_date": "2026-10-07",
    }
    page1 = {"corporate_actions": {"forward_splits": [forward]}, "next_page_token": "n"}
    page2 = {"corporate_actions": {"reverse_splits": [reverse]}, "next_page_token": None}
    route = respx_mock.get(url).mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    splits = await rest.splits(date(2026, 9, 1), date(2026, 10, 9))
    assert [(s.symbol, s.reverse, s.ex_date) for s in splits] == [
        ("SRE", False, date(2026, 10, 5)),
        ("MNTS", True, date(2026, 10, 7)),
    ]
    assert splits[1].old_rate == Decimal(50)
    params = route.calls[0].request.url.params
    assert params["types"] == "forward_split,reverse_split"
    assert route.calls[1].request.url.params["page_token"] == "n"


async def test_snapshots_skip_symbols_without_data(
    rest: AlpacaRest, respx_mock: respx.MockRouter
) -> None:
    url = f"{DATA_URL}/v2/stocks/snapshots"
    body = (
        '{"SPY":{"latestTrade":{"t":"2026-10-09T12:00:01Z","x":"V","p":571.12,"s":100,"c":["@"],'
        '"i":1,"z":"B"},"latestQuote":{"t":"2026-10-09T12:00:01Z","bx":"V","bp":571.1,"bs":2,'
        '"ax":"V","ap":571.14,"as":3,"c":["R"],"z":"B"},'
        f'"dailyBar":{raw_bar("2026-10-08T04:00:00Z", "570.5")},'
        f'"prevDailyBar":{raw_bar("2026-10-07T04:00:00Z", "565.25")}}},"DEAD":null}}'
    )
    respx_mock.get(url).mock(return_value=httpx.Response(200, content=body))
    snaps = await rest.snapshots(["SPY", "DEAD"], feed="iex")
    assert list(snaps) == ["SPY"]
    spy = snaps["SPY"]
    assert spy.latest_trade is not None
    assert spy.latest_trade.price == Decimal("571.12")
    assert spy.latest_quote is not None
    assert spy.latest_quote.ask == Decimal("571.14")
    assert spy.prev_daily_bar is not None
    assert spy.prev_daily_bar.close == Decimal("565.25")
    assert spy.minute_bar is None


async def test_calendar_and_assets_use_the_trading_host(
    rest: AlpacaRest, respx_mock: respx.MockRouter
) -> None:
    row = {
        "date": "2026-10-09",
        "open": "09:30",
        "close": "16:00",
        "session_open": "0400",
        "session_close": "2000",
        "settlement_date": "2026-10-13",
    }
    cal = respx_mock.get(f"{TRADING_PAPER_URL}/v2/calendar").mock(
        return_value=httpx.Response(200, json=[row])
    )
    asset = {
        "id": "u",
        "class": "us_equity",
        "exchange": "NASDAQ",
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "status": "active",
        "tradable": True,
        "marginable": True,
        "shortable": True,
        "fractionable": True,
        "attributes": ["has_options"],
    }
    assets = respx_mock.get(f"{TRADING_PAPER_URL}/v2/assets").mock(
        return_value=httpx.Response(200, json=[asset])
    )
    assert await rest.calendar(date(2026, 10, 9), date(2026, 10, 9)) == [row]
    assert cal.calls.last.request.url.params["start"] == "2026-10-09"
    (aapl,) = await rest.assets()
    assert (aapl.symbol, aapl.exchange, aapl.tradable) == ("AAPL", "NASDAQ", True)
    assert assets.calls.last.request.url.params["asset_class"] == "us_equity"


async def test_news_params_pages_and_parsing(
    rest: AlpacaRest, respx_mock: respx.MockRouter
) -> None:
    def article(id_: int, created: str) -> dict[str, object]:
        return {
            "id": id_,
            "headline": f"headline {id_}",
            "author": "a",
            "created_at": created,
            "updated_at": created,
            "summary": "s",
            "url": "https://example.invalid/n",
            "images": [],
            "symbols": ["AAPL"],
            "source": "benzinga",
        }

    first = {"news": [article(1, "2026-10-09T11:00:00Z")], "next_page_token": "p2"}
    second = {"news": [article(2, "2026-10-09T12:00:00Z")], "next_page_token": None}
    route = respx_mock.get(f"{DATA_URL}/v1beta1/news").mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    items = await rest.news(start=NOW, symbols=["AAPL"])
    assert [(i.id, i.created_at.hour) for i in items] == [(1, 7), (2, 8)]  # ET
    params = route.calls[0].request.url.params
    assert params["start"] == "2026-10-09T12:00:00Z"
    assert (params["limit"], params["sort"]) == ("50", "asc")
    assert (params["include_content"], params["symbols"]) == ("false", "AAPL")
    assert route.calls[1].request.url.params["page_token"] == "p2"


# ---------------------------------------------------------------- configuration


async def test_from_config_uses_book_b_keys_when_no_data_keys(
    repo: Path, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(f"{TRADING_PAPER_URL}/v2/calendar").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with AlpacaRest.from_config(load_config(repo), SimClock(NOW)) as client:
        await client.calendar(date(2026, 10, 9), date(2026, 10, 9))
    sent = route.calls.last.request.headers
    assert sent["APCA-API-KEY-ID"] == FAKE_SECRETS["ALPACA_PAPER_B_KEY_ID"]


async def test_from_config_without_keys_raises(repo: Path) -> None:
    write_env(repo, {"CLAUDE_TOKEN_CREATED": "2026-10-01"})
    with pytest.raises(AlpacaAuthError, match="no data keys"):
        AlpacaRest.from_config(load_config(repo), SimClock(NOW))
