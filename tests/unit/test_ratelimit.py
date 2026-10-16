import asyncio

import pytest

from trader.data.ratelimit import RateLimiter


class FakeTime:
    """Monotonic time that only moves when the limiter sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


def limiter(t: FakeTime, rate_per_min: float, capacity: int) -> RateLimiter:
    return RateLimiter(rate_per_min, capacity=capacity, monotonic=t.monotonic, sleep=t.sleep)


async def test_burst_then_steady_rate() -> None:
    t = FakeTime()
    gate = limiter(t, 60, capacity=3)  # one per second after a burst of three
    for _ in range(5):
        await gate.acquire()
    assert t.now == pytest.approx(2.0)


async def test_any_minute_stays_under_capacity_plus_rate() -> None:
    t = FakeTime()
    gate = limiter(t, 180, capacity=10)
    stamps = []
    for _ in range(400):
        await gate.acquire()
        stamps.append(t.now)
    busiest = max(sum(start <= s < start + 60 for s in stamps) for start in stamps)
    assert busiest <= 190  # Alpaca allows 200 per minute


async def test_waiters_are_served_in_arrival_order() -> None:
    t = FakeTime()
    gate = limiter(t, 60, capacity=1)
    order: list[int] = []

    async def take(i: int) -> None:
        await gate.acquire()
        order.append(i)

    await asyncio.gather(*(take(i) for i in range(5)))
    assert order == [0, 1, 2, 3, 4]


async def test_pause_holds_every_caller() -> None:
    t = FakeTime()
    gate = limiter(t, 600, capacity=5)
    gate.pause_for(30)
    gate.pause_for(10)  # never shortens an existing pause
    await gate.acquire()
    assert t.now == pytest.approx(30.0)


def test_nonsense_settings_are_rejected() -> None:
    with pytest.raises(ValueError, match="rate_per_min"):
        RateLimiter(0)
    with pytest.raises(ValueError, match="capacity"):
        RateLimiter(60, capacity=0)


async def test_an_urgent_caller_goes_ahead_of_waiting_polls() -> None:
    """D-056: an order submit, cancel or sell never queues behind routine reads."""
    t = FakeTime()
    gate = limiter(t, 60, capacity=1)
    await gate.acquire()  # the bucket is empty
    order: list[str] = []

    async def take(name: str, *, urgent: bool = False) -> None:
        await gate.acquire(urgent=urgent)
        order.append(name)

    polls = [asyncio.create_task(take(f"poll {i}")) for i in range(2)]
    await asyncio.sleep(0)  # the polls are waiting
    await take("cancel", urgent=True)
    await asyncio.gather(*polls)
    assert order == ["cancel", "poll 0", "poll 1"]  # polls keep their own order


async def test_a_poll_leaves_the_next_token_to_an_urgent_caller_waiting_for_it() -> None:
    """With no reserve (or once it is spent), a poll arriving while an urgent caller waits for
    the next token lets it go first (review RL-1)."""
    t = FakeTime()
    gate = limiter(t, 60, capacity=1)
    await gate.acquire()  # the bucket is empty
    order: list[str] = []

    async def take(name: str, *, urgent: bool = False) -> None:
        await gate.acquire(urgent=urgent)
        order.append(name)

    cancel = asyncio.create_task(take("cancel", urgent=True))
    await asyncio.sleep(0)  # the cancel waits for the next token
    await take("poll")
    await cancel
    assert order == ["cancel", "poll"]


async def test_polls_leave_the_reserve_to_urgent_callers() -> None:
    t = FakeTime()
    gate = RateLimiter(60, capacity=10, reserve=4, monotonic=t.monotonic, sleep=t.sleep)
    for _ in range(6):
        await gate.acquire()
    for _ in range(4):
        await gate.acquire(urgent=True)  # the reserve, at once
    assert t.now == 0
    await gate.acquire()  # a poll waits for the reserve to refill, then for its own token
    assert t.now == pytest.approx(5.0)


def test_a_reserve_must_leave_room_for_polls() -> None:
    with pytest.raises(ValueError, match="reserve"):
        RateLimiter(60, capacity=4, reserve=4)
    with pytest.raises(ValueError, match="reserve"):
        RateLimiter(60, reserve=-1)
