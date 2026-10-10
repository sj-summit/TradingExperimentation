"""Daily recordings for replay (ARCHITECTURE.md §4.2) under ``<recordings_dir>/YYYY-MM-DD/``.

| File | Contents |
|---|---|
| ``stream.jsonl.gz`` | every market event, one JSON object per line |
| ``news.jsonl.gz`` | every news item |
| ``or_scan.parquet`` | the 09:35 opening-range scan (written by P3.1) |
| ``universe.parquet`` | the day's universe snapshot |

LLM context packs and envelopes stay where ``llm_calls`` points. Decimals and timestamps are
recorded as strings, exactly as the models serialize them, so a replay rebuilds the same values.

JSON-lines files are appended and flushed every ``flush_every`` records and on close. A crash
loses at most the unflushed tail: ``read_jsonl`` returns every record up to the last flush.
"""

import asyncio
import gzip
import json
import zlib
from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Self

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel

from trader.core.models import NewsItem, Quote, Trade
from trader.data.stream import MarketEvent

STREAM_FILE = "stream.jsonl.gz"
NEWS_FILE = "news.jsonl.gz"
UNIVERSE_FILE = "universe.parquet"
OR_SCAN_FILE = "or_scan.parquet"


class Recorder:
    def __init__(self, recordings_dir: Path, day: date, *, flush_every: int = 100) -> None:
        self.dir = recordings_dir / day.isoformat()
        self._flush_every = flush_every
        self._open: dict[str, tuple[IO[str], int]] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ---------------------------------------------------------------- JSON lines

    def record_event(self, event: MarketEvent) -> None:
        kind = (
            "trade" if isinstance(event, Trade) else "quote" if isinstance(event, Quote) else "bar"
        )
        self._append(STREAM_FILE, {"kind": kind, **event.model_dump(mode="json")})

    def record_news(self, item: NewsItem) -> None:
        self._append(NEWS_FILE, item.model_dump(mode="json"))

    async def consume(self, events: "asyncio.Queue[MarketEvent]") -> None:
        """Record everything a hub consumer queue delivers, until cancelled."""
        while True:
            self.record_event(await events.get())
            events.task_done()

    def close(self) -> None:
        for handle, _ in self._open.values():
            handle.close()
        self._open.clear()

    def _append(self, name: str, record: dict[str, Any]) -> None:
        handle, pending = self._open.get(name) or (self._new(name), 0)
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        pending += 1
        if pending >= self._flush_every:
            handle.flush()  # a gzip sync point: everything so far is readable after a crash
            pending = 0
        self._open[name] = (handle, pending)

    def _new(self, name: str) -> IO[str]:
        self.dir.mkdir(parents=True, exist_ok=True)
        # Append mode adds a gzip member per open; readers handle concatenated members.
        return gzip.open(self.dir / name, "at", encoding="utf-8")

    # ---------------------------------------------------------------- tables

    def write_universe(self, rows: Sequence[BaseModel]) -> Path:
        return self.write_table(UNIVERSE_FILE, rows)

    def write_or_scan(self, rows: Sequence[BaseModel]) -> Path:
        return self.write_table(OR_SCAN_FILE, rows)

    def write_table(self, name: str, rows: Sequence[BaseModel]) -> Path:
        """Write ``rows`` as Parquet, replacing any earlier file atomically."""
        self.dir.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist([row.model_dump(mode="json") for row in rows])
        path = self.dir / name
        partial = path.with_name(path.name + ".partial")
        pq.write_table(table, partial)
        partial.replace(path)
        return path


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Every complete record in a recording, including one cut short by a crash."""
    for line in _gunzip_tolerant(path.read_bytes()).splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn final line


def _gunzip_tolerant(data: bytes) -> bytes:
    """Decompress concatenated gzip members, keeping everything before a truncation.

    ``gzip.open`` can't do this: on a member without its trailer its buffered reader raises
    EOFError and drops output it had already decompressed.
    """
    out: list[bytes] = []
    while data:
        member = zlib.decompressobj(wbits=31)  # 31: gzip header and trailer
        try:
            out.append(member.decompress(data))
            out.append(member.flush())
        except zlib.error:
            break  # corrupt bytes: keep what came before
        if not member.eof:
            break  # the last member was cut short: what was flushed is all there is
        data = member.unused_data  # the next member, if any
    return b"".join(out)


def read_table(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
    return rows
