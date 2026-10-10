import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tests.fixtures.fake_ws import FakeAlpacaServer
from trader.core.clock import ET, SimClock
from trader.core.models import NewsItem
from trader.data.news import NewsHub
from trader.memory.journal import Journal

NOW = datetime(2026, 10, 9, 8, 0, tzinfo=ET)


def article(
    id_: int, headline: str = "Headline", symbols: Sequence[str] = ("AAPL",), age_min: int = 30
) -> NewsItem:
    created = NOW - timedelta(minutes=age_min)
    return NewsItem(
        id=id_,
        headline=headline,
        summary="summary",
        author="author",
        source="benzinga",
        url="https://example.invalid/a",
        symbols=tuple(symbols),
        created_at=created,
        updated_at=created,
    )


def stream_msg(id_: int, headline: str, symbols: list[str]) -> dict[str, Any]:
    stamp = NOW.isoformat()
    return {"T": "n", "id": id_, "headline": headline, "summary": "s", "author": "a",
            "created_at": stamp, "updated_at": stamp, "url": "https://example.invalid/b",
            "content": "", "symbols": symbols, "source": "benzinga"}  # fmt: skip


class FakeNewsRest:
    def __init__(self, items: Sequence[NewsItem] = ()) -> None:
        self.items = list(items)
        self.calls: list[datetime] = []

    async def news(
        self,
        *,
        start: datetime,
        end: datetime | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[NewsItem]:
        self.calls.append(start)
        return [i for i in self.items if i.created_at >= start]


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await asyncio.sleep(0)


async def until(condition: Callable[[], bool], timeout_s: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


def make_hub(
    journal: Journal,
    rest: FakeNewsRest,
    server: FakeAlpacaServer,
    sleeps: Sleeps,
    clock: SimClock | None = None,
) -> NewsHub:
    return NewsHub(
        rest=rest,
        journal=journal,
        clock=clock or SimClock(NOW),
        key_id="PKTEST",
        secret_key="secret-value",
        connector=server.connect,
        sleep=sleeps,
    )


async def test_backfill_then_stream(tmp_path: Path) -> None:
    server, rest, sleeps = FakeAlpacaServer(), FakeNewsRest([article(1, age_min=60)]), Sleeps()
    async with await Journal.open(tmp_path / "trader.db") as journal:
        hub = make_hub(journal, rest, server, sleeps)
        assert await hub.start(lookback=timedelta(hours=18)) == 1

        def subscribed() -> bool:
            return bool(server.connections) and "news" in server.connections[0].subscriptions

        await until(subscribed)
        server.connections[0].push(stream_msg(2, "Second", ["MSFT"]))
        await until(lambda: hub.stored == 2)
        assert hub.mode == "stream"
        await hub.stop()
        rows = await journal.fetch_all("SELECT id, headline, symbols_json FROM news ORDER BY id")
    assert [(r["id"], r["headline"], r["symbols_json"]) for r in rows] == [
        ("1", "Headline", '["AAPL"]'),
        ("2", "Second", '["MSFT"]'),
    ]
    assert server.connections[0].url == "wss://stream.data.alpaca.markets/v1beta1/news"
    assert server.connections[0].subscriptions == {"news": {"*"}}
    assert rest.calls == [NOW - timedelta(hours=18)]


async def test_an_updated_article_keeps_its_first_receipt_time(tmp_path: Path) -> None:
    clock = SimClock(NOW)
    rest = FakeNewsRest([article(7, "First take")])
    async with await Journal.open(tmp_path / "trader.db") as journal:
        hub = make_hub(journal, rest, FakeAlpacaServer(), Sleeps(), clock)
        await hub.backfill(NOW - timedelta(hours=1))
        await clock.advance(timedelta(minutes=10))
        rest.items = [article(7, "Corrected take")]
        await hub.backfill(NOW - timedelta(hours=1))
        (row,) = await journal.fetch_all("SELECT headline, received_at FROM news")
    assert row["headline"] == "Corrected take"
    assert row["received_at"] == NOW.isoformat()


async def test_recent_filters_by_symbol_and_time(tmp_path: Path) -> None:
    rest = FakeNewsRest(
        [
            article(1, "AAPL today", ("AAPL",), age_min=60),
            article(2, "MSFT today", ("MSFT",), age_min=30),
            article(3, "AAPL yesterday", ("AAPL", "MSFT"), age_min=600),
        ]
    )
    async with await Journal.open(tmp_path / "trader.db") as journal:
        hub = make_hub(journal, rest, FakeAlpacaServer(), Sleeps())
        await hub.backfill(NOW - timedelta(days=1))
        recent = await hub.recent(["aapl"], since=NOW - timedelta(hours=2))
    assert [item.headline for item in recent] == ["AAPL today"]


async def test_connection_limit_falls_back_to_polling(tmp_path: Path) -> None:
    limited = (406, "connection limit exceeded")
    server, rest, sleeps = (
        FakeAlpacaServer(auth_errors=[limited, limited]),
        FakeNewsRest(),
        Sleeps(),
    )
    async with await Journal.open(tmp_path / "trader.db") as journal:
        hub = make_hub(journal, rest, server, sleeps)
        await hub.start(lookback=timedelta(0))
        await until(lambda: hub.mode == "poll")
        rest.items.append(article(9, "Seen by polling", age_min=0))
        await until(lambda: hub.stored == 1)
        await hub.stop()
    assert len(server.connections) == 2  # the first try plus one retry
    assert 30.0 in sleeps.calls  # waited for Alpaca to clear stale sessions
