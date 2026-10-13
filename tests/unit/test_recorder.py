import asyncio
import contextlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trader.core.clock import ET
from trader.core.models import Bar, NewsItem, Quote, Trade
from trader.data.recorder import NEWS_FILE, STREAM_FILE, Recorder, read_jsonl, read_table
from trader.data.stream import BarEvent, MarketEvent
from trader.data.universe import UniverseRow

DAY = date(2026, 10, 9)
T0 = datetime(2026, 10, 9, 9, 35, tzinfo=ET)


def trade(i: int) -> Trade:
    return Trade(symbol="SPY", ts=T0 + timedelta(seconds=i), price=Decimal("571.12"), size=100)


def events() -> list[MarketEvent]:
    px = Decimal("571.10")
    bar = Bar(ts=T0, open=px, high=px, low=px, close=px, volume=1_200)
    return [
        trade(0),
        Quote(symbol="SPY", ts=T0, bid=px, bid_size=2, ask=Decimal("571.14"), ask_size=3),
        BarEvent(symbol="SPY", bar=bar, source="stream"),
    ]


def test_stream_events_round_trip_exactly(tmp_path: Path) -> None:
    with Recorder(tmp_path, DAY) as rec:
        for event in events():
            rec.record_event(event)
    path = tmp_path / "2026-10-09" / STREAM_FILE
    first, quote, bar = read_jsonl(path)
    assert first["kind"] == "trade"
    assert first["price"] == "571.12"  # Decimals stay exact as strings
    assert first["ts"] == "2026-10-09T09:35:00-04:00"
    assert (quote["kind"], quote["ask"]) == ("quote", "571.14")
    assert (bar["kind"], bar["source"], bar["bar"]["volume"]) == ("bar", "stream", 1_200)
    assert Trade.model_validate({k: v for k, v in first.items() if k != "kind"}) == trade(0)


def test_flushed_records_survive_without_close(tmp_path: Path) -> None:
    rec = Recorder(tmp_path, DAY, flush_every=2)
    for i in range(3):
        rec.record_event(trade(i))  # the third record is still buffered
    seen = list(read_jsonl(tmp_path / "2026-10-09" / STREAM_FILE))
    assert len(seen) == 2
    rec.close()
    assert len(list(read_jsonl(tmp_path / "2026-10-09" / STREAM_FILE))) == 3


def test_a_file_cut_short_still_reads(tmp_path: Path) -> None:
    rec = Recorder(tmp_path, DAY, flush_every=1)
    for i in range(50):
        rec.record_event(trade(i))
    rec.close()
    path = tmp_path / "2026-10-09" / STREAM_FILE
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - 40])  # lose the trailer and some compressed data
    survivors = list(read_jsonl(path))
    assert 40 <= len(survivors) < 50
    assert all(r["kind"] == "trade" for r in survivors)


def test_a_killed_run_and_its_rerun_both_read_back(tmp_path: Path) -> None:
    """A run killed mid-file leaves a member with no trailer; the rerun appends its own member
    after it. Every complete record of both reads back (D-051)."""
    rec = Recorder(tmp_path, DAY, flush_every=1)
    for i in range(50):
        rec.record_event(trade(i))
    rec.close()
    path = tmp_path / "2026-10-09" / STREAM_FILE
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - 40])  # killed: no trailer, the tail torn
    with Recorder(tmp_path, DAY) as rerun:
        for i in range(50, 53):
            rerun.record_event(trade(i))
    seen = [r["ts"] for r in read_jsonl(path)]
    first = seen[:-3]
    assert 40 <= len(first) < 50
    assert first == [(T0 + timedelta(seconds=i)).isoformat() for i in range(len(first))]
    assert seen[-3:] == [(T0 + timedelta(seconds=i)).isoformat() for i in range(50, 53)]


def test_reopening_appends_a_second_gzip_member(tmp_path: Path) -> None:
    with Recorder(tmp_path, DAY) as rec:
        rec.record_event(trade(0))
    with Recorder(tmp_path, DAY) as rec:  # e.g. after a crash restart
        rec.record_event(trade(1))
    assert len(list(read_jsonl(tmp_path / "2026-10-09" / STREAM_FILE))) == 2


def test_news_is_recorded(tmp_path: Path) -> None:
    item = NewsItem(id=7, headline="h", summary="s", author="a", source="benzinga", url=None,
                    symbols=("AAPL",), created_at=T0, updated_at=T0)  # fmt: skip
    with Recorder(tmp_path, DAY) as rec:
        rec.record_news(item)
    (record,) = read_jsonl(tmp_path / "2026-10-09" / NEWS_FILE)
    assert NewsItem.model_validate(record) == item


def test_universe_snapshot_is_parquet(tmp_path: Path) -> None:
    row = UniverseRow(date=DAY, symbol="GOOD", name="Good Inc", exchange="NASDAQ",
                      prev_close=Decimal("20.50"), atr14=Decimal("1.0000"), adv14=2_000_000,
                      or_vol_avg14=3_000.0, eligible=True, exclusion_reason=None)  # fmt: skip
    path = Recorder(tmp_path, DAY).write_universe([row])
    assert path == tmp_path / "2026-10-09" / "universe.parquet"
    (stored,) = read_table(path)
    assert stored["prev_close"] == "20.50"
    assert stored["eligible"] is True
    assert UniverseRow.model_validate(stored) == row
    assert not path.with_name("universe.parquet.partial").exists()


async def test_consume_records_a_hub_queue(tmp_path: Path) -> None:
    queue: asyncio.Queue[MarketEvent] = asyncio.Queue()
    for event in events():
        queue.put_nowait(event)
    rec = Recorder(tmp_path, DAY)
    task = asyncio.create_task(rec.consume(queue))
    await asyncio.wait_for(queue.join(), timeout=1.0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    rec.close()
    kinds = [r["kind"] for r in read_jsonl(tmp_path / "2026-10-09" / STREAM_FILE)]
    assert kinds == ["trade", "quote", "bar"]
