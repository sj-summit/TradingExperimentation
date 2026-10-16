"""A recorded entry replays without divergence near its time-based cancels (D-056).

A live engine spends a few milliseconds a tick on work a SimClock replay never spends, so their
ticks drift apart. Reading a working entry only every 2 s would then leave a replay acting on a
2 s old answer where live acted on the stream's fill: a cancel the recording lacks. Where a cancel
can come due on time, the service reads every tick, so the recording holds a fresh answer next
to any decision the replay takes.
"""

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.exec_harness import CALENDAR, FEES, NEXT_OPEN, RISK, T0, TIMES, Harness
from trader.execution.broker import BrokerAdapter
from trader.execution.recording import RecordingBroker
from trader.execution.records import Records
from trader.execution.replay import ReplayBroker
from trader.execution.service import ExecutionService, Exposure, TradingDay
from trader.execution.sim_broker import SimBroker
from trader.memory.journal import Journal
from trader.orchestrator.simdrive import drive
from trader.risk.breakers import Halts
from trader.risk.gate import GateState
from trader.risk.ledger import SettlementLedger

D = Decimal
ENTRY = "A-20261009-XYZ-entry-1"
LIVE_TICK_COST = timedelta(milliseconds=3)  # G3's live ticks spent 2-18 ms off the REST calls


class Rig(Harness):
    """The harness, its service on a broker that ``make`` builds (recording or replaying)."""

    def __init__(self, tmp_path: Path, make: Callable[["Rig"], BrokerAdapter]) -> None:
        super().__init__(tmp_path)
        self.make = make
        self.broker: Any = None

    async def open(self, *, seed: bool = True) -> "Rig":
        self.journal = await Journal.open(self.path, inline=True)
        self.records = Records(
            self.journal, book="A", calendar=CALENDAR, fees=FEES, clock=self.clock
        )
        self.halts = Halts(self.journal, self.clock)
        await SettlementLedger(self.journal, "A", self.clock).seed(
            D(1000), positions=0, open_orders=0
        )
        self.broker = self.make(self)
        self.service = ExecutionService(
            book="A",
            broker=self.broker,
            journal=self.journal,
            records=self.records,
            halts=self.halts,
            gate=self.gate,
            prices=self.prices,
            clock=self.clock,
            risk=RISK,
            day=TradingDay(times=TIMES, next_open=NEXT_OPEN),
        )
        await self.service.start()
        return self


async def life(h: Rig, *, live: bool, fills: list[tuple[float, int | None]]) -> None:
    """One entry: sent at T0, then filled at each (seconds, shares or all) while live."""
    if live:
        h.quote("12.40", "12.50")  # above the 12.35 limit: the entry works unfilled
    intent = h.intent()

    async def build(exposure: Exposure) -> GateState:
        return await h.state(exposure, intent.symbol)

    await h.service.try_enter(intent, build)
    for at, shares in fills:
        await h.clock.sleep_until(T0 + timedelta(seconds=at))
        if live:
            if shares is not None:
                h.sim.partial_next(shares)
            h.quote("12.30", "12.32")
            h.quote("12.40", "12.50")
    await h.clock.sleep_until(T0 + timedelta(seconds=16))
    await h.service.stop()


async def record_then_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fills: list[tuple[float, int | None]]
) -> list[str]:
    """Record the entry's life with each live tick costing LIVE_TICK_COST of clock time, then
    replay it at no cost; returns the replay's divergences."""
    recorded: list[dict[str, Any]] = []
    with monkeypatch.context() as m:
        original = ExecutionService._stood_down

        async def slow(self: ExecutionService) -> bool:
            result = await original(self)
            await self._clock.sleep_until(self._clock.now() + LIVE_TICK_COST)
            return result

        m.setattr(ExecutionService, "_stood_down", slow)
        (tmp_path / "live").mkdir()
        live = await Rig(
            tmp_path / "live", lambda r: RecordingBroker(r.sim, r.clock, recorded.append)
        ).open()
        await drive(live.clock, life(live, live=True, fills=fills), watched=[live.journal])
        await live.journal.aclose()
    (tmp_path / "replay").mkdir()
    replay = await Rig(
        tmp_path / "replay",
        lambda r: ReplayBroker(recorded, r.clock, fallback=SimBroker(clock=r.clock)),
    ).open()
    await drive(
        replay.clock, life(replay, live=False, fills=fills), watched=[replay.journal, replay.sim]
    )
    await replay.journal.aclose()
    return list(replay.broker.divergences)


@pytest.mark.parametrize("fill_at", [10.0, 10.01, 10.02])
async def test_a_fill_at_the_timeout_replays_as_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fill_at: float
) -> None:
    """Accepted at once, the entry times out at 10 s; the fill lands just after."""
    assert await record_then_replay(tmp_path, monkeypatch, [(fill_at, None)]) == []


@pytest.mark.parametrize("rest_at", [3.505, 3.52])
async def test_a_partial_fill_at_its_grace_replays_as_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rest_at: float
) -> None:
    """15 shares at 2.5 s; the rest just after the 1 s grace would cancel them."""
    fills: list[tuple[float, int | None]] = [(2.5, 15), (rest_at, None)]
    assert await record_then_replay(tmp_path, monkeypatch, fills) == []
