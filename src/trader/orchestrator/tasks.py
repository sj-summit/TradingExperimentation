"""The trading day's background work (A§3, A§11, RISK.md §5; DECISIONS D-048).

Every loop runs on the injected clock and outlives its own failures: an error is logged and the
loop goes on (resolution 12), so one bad response can't end the heartbeat, the alert pump, the
staleness watch or the kill-switch watch.
"""

import json
import math
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from trader.config import BookName
from trader.core.clock import Clock, fmt_iso
from trader.data.stream import TRADING_CHANNELS, Channel, SubscriptionLimitError
from trader.data.ws import StreamError
from trader.execution.service import ExecutionService
from trader.memory.incidents import record_incident
from trader.memory.journal import Journal
from trader.orchestrator.plan import DayPlan
from trader.orchestrator.ports import Hub
from trader.risk.breakers import Halts, staleness

log = structlog.get_logger(__name__)


async def every(
    clock: Clock, period_s: float, job: Callable[[], Awaitable[object]], *, name: str
) -> None:
    """Run ``job`` now and then every ``period_s`` until cancelled, whatever it raises."""
    period = timedelta(seconds=period_s)
    while True:
        try:
            await job()
        except Exception:
            log.exception("background job failed", job=name)
        await clock.sleep_until(clock.now() + period)


# ---------------------------------------------------------------- heartbeat (A§11)


@dataclass
class EquityTrack:
    """A book's marked equity through the day, for ``daily_stats.max_intraday_dd`` ($)."""

    peak: Decimal
    max_drawdown: Decimal = Decimal(0)

    def sample(self, marked: Decimal) -> None:
        self.peak = max(self.peak, marked)
        self.max_drawdown = max(self.max_drawdown, self.peak - marked)


class Heartbeat:
    """Writes ``heartbeat.json`` (``{ts, state, books: {A: {halted, halts, positions}},
    hub_age_s}``) for the watchdog, and samples each book's marked equity on every beat."""

    def __init__(
        self,
        path: Path,
        clock: Clock,
        *,
        services: Mapping[BookName, ExecutionService],
        halts: Halts,
        state: Callable[[], str],
        hub_age: Callable[[], float],
    ) -> None:
        self._path, self._clock, self._services, self._halts = path, clock, services, halts
        self._state, self._hub_age = state, hub_age
        self.tracks: dict[BookName, EquityTrack] = {}

    async def beat(self) -> dict[str, Any]:
        books: dict[str, Any] = {}
        for name in sorted(self._services):
            service = self._services[name]
            exposure = await service.exposure()
            track = self.tracks.setdefault(name, EquityTrack(peak=exposure.equity_sod))
            track.sample(exposure.ledger_equity)
            lasting = [h.reason for h in await self._halts.active(name) if h.scope != "marker"]
            books[name] = {"halted": bool(lasting), "halts": lasting, "positions": service.held}
        age = self._hub_age()
        record = {
            "ts": fmt_iso(self._clock.now()),
            "state": self._state(),
            "books": books,
            "hub_age_s": None if math.isinf(age) else round(age, 1),
        }
        self._write(record)
        return record

    def _write(self, record: Mapping[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            partial = self._path.with_name(self._path.name + ".partial")
            partial.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
            os.replace(partial, self._path)  # readers never see half a file
        except OSError as exc:  # e.g. the watchdog has it open on Windows: next beat
            log.warning("heartbeat not written", error=str(exc))


# ---------------------------------------------------------------- data staleness (R§5)


class StaleWatch:
    """No market event for ``warn_s`` during regular hours logs a warning; for ``halt_s`` it
    blocks new entries (``hub_stale``, R-016) and raises one P2 per episode. The clock starts
    at the open or at the first subscription, whichever is later."""

    def __init__(
        self,
        *,
        hub: Hub,
        clock: Clock,
        journal: Journal,
        plan: DayPlan,
        warn_s: float,
        halt_s: float,
    ) -> None:
        self._hub, self._clock, self._journal, self._plan = hub, clock, journal, plan
        self._warn_s, self._halt_s = warn_s, halt_s
        self._since: datetime | None = None
        self._warned = False
        self.stale = False

    def started(self) -> None:
        if self._since is None:
            self._since = self._clock.now()

    async def check(self) -> None:
        now, times = self._clock.now(), self._plan.times
        if self._since is None or not times.open <= now < times.close:
            self.stale = self._warned = False
            return
        quiet_for = (now - max(self._since, times.open)).total_seconds()
        age = min(self._hub.last_event_age_s(), quiet_for)
        level = staleness(age, self._warn_s, self._halt_s)
        if level == "halt":
            if not self.stale:
                self.stale = True
                await record_incident(
                    self._journal,
                    at=now,
                    severity="P2",
                    component="data",
                    code="data_stale",
                    message=f"no market data for {age:.0f} s: new entries blocked",
                )
            return
        if self.stale:
            log.info("market data back", age_s=round(age, 1))
        self.stale = False
        if level == "warn" and not self._warned:
            log.warning("market data quiet", age_s=round(age, 1))
        self._warned = level == "warn"


# ---------------------------------------------------------------- subscriptions (resolution 13)


class Subscriptions:
    """The stream's symbols: the regime symbols and every symbol a book holds or is entering
    (never dropped), then the armed signals by rank while there is room.

    A 405 from the server first drops quotes (trades only), then armed symbols from the lowest
    rank up. Either, or trimming for room, raises one P2 for the day."""

    def __init__(
        self, *, hub: Hub, journal: Journal, clock: Clock, limit: int, regime: Sequence[str]
    ) -> None:
        self._hub, self._journal, self._clock = hub, journal, clock
        self._limit, self._regime = limit, tuple(regime)
        self.channels: tuple[Channel, ...] = TRADING_CHANNELS
        self.armed: list[str] = []  # by rank
        self.dropped: list[str] = []
        self._alerted = False

    async def apply(self, held: Iterable[str]) -> list[str]:
        must = sorted({*self._regime, *(s.upper() for s in held)})
        optional = [s for s in dict.fromkeys(self.armed) if s not in must]
        room = max(self._limit - len(must), 0)
        wanted, dropped = [*must, *optional[:room]], optional[room:]
        refused: StreamError | None = None
        while True:
            try:
                await self._hub.set_subscriptions(wanted, channels=self.channels)
                break
            except SubscriptionLimitError as exc:  # the held symbols alone are too many
                await self._alert("subscription_refused", str(exc))
                return []
            except StreamError as exc:
                if exc.code != 405:
                    await self._alert("subscription_refused", str(exc))
                    return []
                refused = exc
                if "quotes" in self.channels:
                    self.channels = ("trades",)
                    continue
                extra = [s for s in wanted if s not in must]
                if not extra:
                    await self._alert("subscription_refused", str(exc))
                    return []
                wanted.remove(extra[-1])
                dropped.insert(0, extra[-1])
        self.dropped = dropped
        if refused is not None or dropped:
            channels = "+".join(self.channels)
            await self._alert(
                "subscription_limited",
                f"streaming {len(wanted)} symbols ({channels}); not streamed: "
                f"{', '.join(dropped) or 'none'}",
            )
        log.info("subscribed", symbols=len(wanted), channels=list(self.channels))
        return wanted

    async def _alert(self, code: str, message: str) -> None:
        if self._alerted:
            log.warning("stream subscription", code=code, message=message)
            return
        self._alerted = True
        await record_incident(
            self._journal,
            at=self._clock.now(),
            severity="P2",
            component="data",
            code=code,
            message=message,
        )
