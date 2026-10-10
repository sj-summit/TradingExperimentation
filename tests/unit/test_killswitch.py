import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trader.core.clock import ET, SimClock
from trader.memory.journal import Journal
from trader.risk.breakers import Halts
from trader.risk.killswitch import KillSwitch

T0 = datetime(2026, 10, 9, 11, 0, tzinfo=ET)


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    yield opened
    await opened.aclose()


class Flattener:
    def __init__(self, *results: bool) -> None:
        self.results, self.calls = list(results), 0

    async def __call__(self) -> bool:
        self.calls += 1
        return self.results.pop(0) if self.results else True


def switch(tmp_path: Path, journal: Journal, clock: SimClock, flatten: Flattener) -> KillSwitch:
    return KillSwitch(
        tmp_path / "KILL",
        halts=Halts(journal, clock),
        clock=clock,
        books=("A", "B"),
        flatten=flatten,
    )


async def test_the_file_halts_every_book_and_flattens(tmp_path: Path, journal: Journal) -> None:
    clock, flatten = SimClock(T0), Flattener()
    kill = switch(tmp_path, journal, clock, flatten)
    assert not await kill.check()
    (tmp_path / "KILL").write_text("stop", encoding="utf-8")
    assert await kill.check()
    halts = Halts(journal, clock)
    for book in ("A", "B"):
        assert [h.reason for h in await halts.active(book)] == ["kill_switch"]
    assert await kill.check()
    assert flatten.calls == 1  # flat already: not flattened again


async def test_removing_the_file_does_not_resume(tmp_path: Path, journal: Journal) -> None:
    clock = SimClock(T0)
    kill = switch(tmp_path, journal, clock, Flattener())
    (tmp_path / "KILL").write_text("", encoding="utf-8")
    await kill.check()
    (tmp_path / "KILL").unlink()
    assert not await kill.check()
    assert [h.reason for h in await Halts(journal, clock).active("A")] == ["kill_switch"]


async def test_flatten_is_retried_while_not_flat(tmp_path: Path, journal: Journal) -> None:
    clock, flatten = SimClock(T0), Flattener(False, True)
    kill = switch(tmp_path, journal, clock, flatten)
    (tmp_path / "KILL").write_text("", encoding="utf-8")
    await kill.check()
    await kill.check()
    assert flatten.calls == 1  # the next try waits 30 s
    await clock.advance(timedelta(seconds=30))
    await kill.check()
    await clock.advance(timedelta(seconds=30))
    await kill.check()
    assert flatten.calls == 2  # flat now


async def test_watch_notices_the_file_within_a_second(tmp_path: Path, journal: Journal) -> None:
    clock, flatten = SimClock(T0), Flattener()
    kill = switch(tmp_path, journal, clock, flatten)
    task = asyncio.create_task(kill.watch())
    await asyncio.sleep(0.01)
    (tmp_path / "KILL").write_text("", encoding="utf-8")
    await clock.advance(timedelta(seconds=1))  # the next poll
    assert kill.triggered
    for _ in range(200):  # the halt rows are written on the journal's thread
        if flatten.calls:
            break
        await asyncio.sleep(0.005)
    assert flatten.calls == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
