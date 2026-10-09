import asyncio
from datetime import UTC, date, datetime, time, timedelta

import pytest

from trader.core.clock import (
    ET,
    NaiveDatetimeError,
    RealClock,
    SimClock,
    ensure_et,
    et_datetime,
    fmt_iso,
    parse_et_iso,
)

T0 = datetime(2026, 10, 9, 9, 30, tzinfo=ET)


def s(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


# ---------------------------------------------------------------- helpers


def test_ensure_et_rejects_naive() -> None:
    with pytest.raises(NaiveDatetimeError):
        ensure_et(datetime(2026, 10, 9, 9, 30))  # noqa: DTZ001 - deliberately naive


def test_ensure_et_converts_other_zones() -> None:
    converted = ensure_et(datetime(2026, 10, 9, 13, 30, tzinfo=UTC))
    assert converted == T0
    assert converted.tzinfo is ET


@pytest.mark.parametrize(
    ("day", "utc_hour"),
    [
        (date(2026, 3, 6), 14),  # EST, the Friday before the 2026-03-08 switch
        (date(2026, 3, 9), 13),  # EDT
        (date(2026, 10, 30), 13),  # EDT, the Friday before the 2026-11-01 switch
        (date(2026, 11, 2), 14),  # EST
    ],
)
def test_et_datetime_tracks_dst(day: date, utc_hour: int) -> None:
    open_utc = et_datetime(day, time(9, 30)).astimezone(UTC)
    assert (open_utc.hour, open_utc.minute) == (utc_hour, 30)


def test_et_datetime_rejects_wall_time_skipped_by_dst() -> None:
    with pytest.raises(ValueError, match="DST gap"):
        et_datetime(date(2026, 3, 8), time(2, 30))


def test_et_datetime_rejects_wall_time_repeated_by_dst() -> None:
    with pytest.raises(ValueError, match="DST overlap"):
        et_datetime(date(2026, 11, 1), time(1, 30))


def test_parse_et_iso_requires_an_offset() -> None:
    assert parse_et_iso("2026-10-09T13:30:00+00:00") == T0
    with pytest.raises(NaiveDatetimeError):
        parse_et_iso("2026-10-09T09:30:00")


def test_fmt_iso_round_trips() -> None:
    assert fmt_iso(T0) == "2026-10-09T09:30:00-04:00"
    assert parse_et_iso(fmt_iso(T0)) == T0


# ---------------------------------------------------------------- RealClock


def test_real_clock_is_aware_et() -> None:
    assert RealClock().now().tzinfo is ET


async def test_real_clock_sleep_until_past_returns_at_once() -> None:
    clock = RealClock()
    await asyncio.wait_for(clock.sleep_until(clock.now() - timedelta(seconds=1)), timeout=1)


# ---------------------------------------------------------------- SimClock


def test_sim_clock_rejects_naive_start() -> None:
    with pytest.raises(NaiveDatetimeError):
        SimClock(datetime(2026, 10, 9, 9, 30))  # noqa: DTZ001 - deliberately naive


async def test_sim_clock_wakes_sleepers_in_deadline_order() -> None:
    clock = SimClock(T0)
    woke: list[tuple[str, datetime]] = []

    async def sleeper(name: str, at: datetime) -> None:
        await clock.sleep_until(at)
        woke.append((name, clock.now()))

    tasks = [
        asyncio.create_task(sleeper("c", s(3))),
        asyncio.create_task(sleeper("a", s(1))),
        asyncio.create_task(sleeper("b", s(2))),
        asyncio.create_task(sleeper("b2", s(2))),  # tie: FIFO by registration
    ]
    await asyncio.sleep(0)
    assert clock.next_wakeup() == s(1)
    await clock.advance_to(s(5))
    await asyncio.gather(*tasks)
    assert woke == [("a", s(1)), ("b", s(2)), ("b2", s(2)), ("c", s(3))]
    assert clock.now() == s(5)


async def test_woken_task_can_sleep_again_within_one_advance() -> None:
    clock = SimClock(T0)
    marks: list[datetime] = []

    async def two_naps() -> None:
        await clock.sleep_until(s(1))
        marks.append(clock.now())
        await clock.sleep_until(s(2))
        marks.append(clock.now())

    task = asyncio.create_task(two_naps())
    await asyncio.sleep(0)
    await clock.advance_to(s(3))
    await task
    assert marks == [s(1), s(2)]


async def test_sleep_until_now_or_past_returns_at_once() -> None:
    clock = SimClock(T0)
    await asyncio.wait_for(clock.sleep_until(T0), timeout=1)
    await asyncio.wait_for(clock.sleep_until(s(-60)), timeout=1)


async def test_sim_clock_never_goes_backwards() -> None:
    clock = SimClock(T0)
    with pytest.raises(ValueError, match="backwards"):
        await clock.advance_to(s(-1))


async def test_sim_clock_sleep_until_rejects_naive() -> None:
    with pytest.raises(NaiveDatetimeError):
        await SimClock(T0).sleep_until(datetime(2026, 10, 9, 10, 0))  # noqa: DTZ001


async def test_cancelled_sleeper_is_skipped() -> None:
    clock = SimClock(T0)
    task = asyncio.create_task(clock.sleep_until(s(1)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clock.next_wakeup() is None
    await clock.advance(timedelta(seconds=2))
    assert clock.now() == s(2)
