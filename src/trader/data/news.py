"""NewsHub: Alpaca news (Benzinga) into the ``news`` table (ARCHITECTURE.md §4.2).

On start it backfills over REST, then streams every article. If the news WebSocket is refused
for lack of a connection slot (Q2), it polls REST instead (DECISIONS D-028). Text is stored raw;
sanitizing happens only when a context pack is built.
"""

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

import structlog

from trader.core.clock import Clock, fmt_iso, parse_et_iso
from trader.core.models import NewsItem
from trader.data.alpaca_rest import AlpacaError, news_item
from trader.data.ws import (
    CONNECTION_LIMIT,
    NEWS_STREAM_URL,
    AlpacaStream,
    WsConnector,
    websockets_connector,
)
from trader.memory.journal import Journal

log = structlog.get_logger(__name__)

POLL_INTERVAL_S = 60.0
# Re-read the end of the previous window; upserts make the overlap harmless.
POLL_OVERLAP = timedelta(minutes=5)
# One 30 s retry on 406, then polling takes over.
STREAM_CONNECTION_LIMIT_RETRIES = 1
# ISO strings with different UTC offsets don't sort chronologically across a DST change, so
# queries over-select by this margin and filter exactly in Python.
_QUERY_MARGIN = timedelta(hours=2)

_UPSERT = (
    "INSERT INTO news (id, created_at, updated_at, headline, summary, source, url, symbols_json, "
    "received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
    "updated_at = excluded.updated_at, headline = excluded.headline, "
    "summary = excluded.summary, source = excluded.source, url = excluded.url, "
    "symbols_json = excluded.symbols_json"
)


class NewsSource(Protocol):
    async def news(
        self,
        *,
        start: datetime,
        end: datetime | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[NewsItem]: ...


class NewsHub:
    def __init__(
        self,
        *,
        rest: NewsSource,
        journal: Journal,
        clock: Clock,
        key_id: str,
        secret_key: str,
        connector: WsConnector = websockets_connector,
        poll_interval_s: float = POLL_INTERVAL_S,
        on_item: Callable[[NewsItem], None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rest, self._journal, self._clock = rest, journal, clock
        self._poll_interval_s, self._on_item, self._sleep = poll_interval_s, on_item, sleep
        self._queue: asyncio.Queue[NewsItem] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._polled_until: datetime | None = None
        self.mode: Literal["idle", "stream", "poll", "failed"] = "idle"
        self.stored = 0
        self._stream = AlpacaStream(
            url=NEWS_STREAM_URL,
            key_id=key_id,
            secret_key=secret_key,
            channels=("news",),
            clock=clock,
            on_data=self._on_message,
            connector=connector,
            connection_limit_retries=STREAM_CONNECTION_LIMIT_RETRIES,
            sleep=sleep,
        )

    async def start(self, lookback: timedelta) -> int:
        """Backfill ``lookback`` over REST, then follow the stream. Returns articles backfilled."""
        backfilled = await self.backfill(self._clock.now() - lookback)
        self._polled_until = self._clock.now()
        await self._stream.update({"news": ["*"]})  # applied when the stream connects
        self._tasks = [
            asyncio.create_task(self._store_loop(), name="news-store"),
            asyncio.create_task(self._follow(), name="news-hub"),
        ]
        return backfilled

    async def stop(self) -> None:
        await self._stream.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        leftover = []
        while not self._queue.empty():
            leftover.append(self._queue.get_nowait())
        if leftover:
            await self._store(leftover)

    async def backfill(self, since: datetime) -> int:
        items = await self._rest.news(start=since)
        await self._store(items)
        return len(items)

    async def recent(self, symbols: Iterable[str], since: datetime) -> list[NewsItem]:
        """Stored articles tagged with any of ``symbols`` and created at or after ``since``."""
        wanted = {s.upper() for s in symbols}
        rows = await self._journal.fetch_all(
            "SELECT * FROM news WHERE created_at >= ? ORDER BY created_at",
            (fmt_iso(since - _QUERY_MARGIN),),
        )
        items = [_from_row(row) for row in rows]
        return [i for i in items if i.created_at >= since and wanted.intersection(i.symbols)]

    # ---------------------------------------------------------------- internals

    async def _follow(self) -> None:
        self.mode = "stream"
        await self._stream.run()  # returns on stop() or when the stream gives up
        failed = self._stream.failed
        if failed is not None and failed.code == CONNECTION_LIMIT:
            log.warning("news stream refused for lack of a connection slot; polling REST")
            self.mode = "poll"
            await self._poll()
        elif failed is not None:
            log.error("news stream failed", code=failed.code, msg=failed.message)
            self.mode = "failed"

    async def _poll(self) -> None:
        while True:
            now = self._clock.now()
            since = (self._polled_until or now) - POLL_OVERLAP
            try:
                items = await self._rest.news(start=since)
            except AlpacaError as exc:
                log.warning("news poll failed", error=str(exc))
            else:
                self._polled_until = now
                for item in items:
                    self._queue.put_nowait(item)
            await self._sleep(self._poll_interval_s)

    def _on_message(self, message: Mapping[str, Any]) -> None:
        if message.get("T") == "n":
            self._queue.put_nowait(news_item(message))

    async def _store_loop(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())
            await self._store(batch)

    async def _store(self, items: Sequence[NewsItem]) -> None:
        if not items:
            return
        received = fmt_iso(self._clock.now())
        rows = [
            (
                str(item.id),
                fmt_iso(item.created_at),
                fmt_iso(item.updated_at),
                item.headline,
                item.summary,
                item.source,
                item.url,
                json.dumps(list(item.symbols)),
                received,
            )
            for item in items
        ]
        await self._journal.write(lambda conn: conn.executemany(_UPSERT, rows))
        self.stored += len(items)
        if self._on_item is not None:
            for item in items:
                self._on_item(item)


def _from_row(row: sqlite3.Row) -> NewsItem:
    return NewsItem(
        id=int(row["id"]),
        headline=row["headline"] or "",
        summary=row["summary"] or "",
        author="",
        source=row["source"] or "",
        url=row["url"],
        symbols=tuple(json.loads(row["symbols_json"] or "[]")),
        created_at=parse_et_iso(row["created_at"]),
        updated_at=parse_et_iso(row["updated_at"]),
    )
