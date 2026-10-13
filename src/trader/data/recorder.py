"""Daily recordings for replay (ARCHITECTURE.md §4.2) under ``<recordings_dir>/YYYY-MM-DD/``.

| File | Contents |
|---|---|
| ``stream.jsonl.gz`` | every market event with its receive time ``rx``, one JSON object per line |
| ``rest.jsonl.gz`` | every market-data REST call of the day: arguments, result, start/end ``rx`` |
| ``broker-A.jsonl.gz`` | Book A's broker calls and trade updates (``broker-B`` likewise) |
| ``boot.json`` | each book's ledger, halts and state at boot (``boot-2.json`` after a restart) |
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
from datetime import date, datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Self

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel

from trader.core.clock import fmt_iso
from trader.core.models import NewsItem, Quote, Trade
from trader.data.stream import MarketEvent

STREAM_FILE = "stream.jsonl.gz"
REST_FILE = "rest.jsonl.gz"
BOOT_FILE = "boot.json"
NEWS_FILE = "news.jsonl.gz"
UNIVERSE_FILE = "universe.parquet"
OR_SCAN_FILE = "or_scan.parquet"
_GZIP_MAGIC = b"\x1f\x8b\x08"  # the start of a gzip member (deflate)
_CHUNK = 1 << 16  # a damaged member is decoded in pieces, keeping what precedes the damage


def broker_file(book: str) -> str:
    return f"broker-{book}.jsonl.gz"


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

    def record_event(self, event: MarketEvent, rx: datetime | None = None) -> None:
        """One hub event. ``rx`` is when the engine received it, so replay can order events
        as the engine saw them (DECISIONS D-047)."""
        kind = (
            "trade" if isinstance(event, Trade) else "quote" if isinstance(event, Quote) else "bar"
        )
        record = {"kind": kind, **event.model_dump(mode="json")}
        if rx is not None:
            record["rx"] = fmt_iso(rx)
        self._append(STREAM_FILE, record)

    def append(self, name: str, record: dict[str, Any]) -> None:
        """One JSON line to ``name`` in the day's folder (REST and broker recordings)."""
        self._append(name, record)

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
    """Decompress concatenated gzip members, keeping every complete line it can.

    ``gzip.open`` can't do this: on a member without its trailer its buffered reader raises
    EOFError and drops output it had already decompressed. A member cut short by a crash may
    be followed by the next run's members (append mode): it is read again only up to the next
    gzip header, its torn last line is dropped, and reading resumes there (D-051).
    """
    out: list[bytes] = []
    pos = 0
    while pos < len(data):
        member = zlib.decompressobj(wbits=31)  # 31: gzip header and trailer
        try:
            whole = member.decompress(data[pos:]) + member.flush()
        except zlib.error:
            whole = None
        if whole is not None and member.eof:  # the trailer's CRC checked out
            out.append(whole)
            pos = len(data) - len(member.unused_data)  # the next member, if any
            continue
        nxt = data.find(_GZIP_MAGIC, pos + 1)
        out.append(_whole_lines(data[pos : len(data) if nxt < 0 else nxt]))
        if nxt < 0:
            break
        pos = nxt
    return b"".join(out)


def _whole_lines(member: bytes) -> bytes:
    """What a damaged member decodes to before the damage, cut to its last complete line."""
    reader = zlib.decompressobj(wbits=31)
    parts: list[bytes] = []
    for start in range(0, len(member), _CHUNK):
        try:
            parts.append(reader.decompress(member[start : start + _CHUNK]))
        except zlib.error:
            break
    text = b"".join(parts)
    return text[: text.rfind(b"\n") + 1]


def read_table(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
    return rows
