"""Entry triggers (S§3.3; DECISIONS D-043): one-shot, on the live trade stream.

- **Which trades count:** only trades whose conditions would update a minute bar's high or low
  (Alpaca's market-data FAQ). Odd lots (``I``) and the other excluded conditions never trigger
  and never set the last price, so triggers agree with ``or_high`` and with the SIP-bar shadow
  outcomes. Unknown codes count: Alpaca doesn't document the IEX feed's conditions.
- **Window:** a trade stamped before the opening range ends (09:35:00) is a late print from
  inside the range and never triggers; nothing triggers once ``last_entry`` is reached.
- **Arming:** the armed symbols are subscribed first (so the stream buffers their trades), then
  one snapshot gives each its last price. At or above the trigger and inside the chase guard it
  triggers at once (``arm_check``); beyond the guard it is ``missed_at_arm``.
- **Dispatch:** every trigger goes to one queue per book, so a book that waits (Book B, for a
  verdict) never delays another. ``next_batch`` serves what is already waiting in rank order.
- **Restart (A§3):** signals still ``armed`` without orders are re-armed with a fresh arm check; a
  ``triggered`` signal that a book never decided is served again before ``last_entry``, and is
  ``restart_lost`` for that book after it.
"""

import asyncio
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import structlog

from trader.config import BookName
from trader.core.clock import Clock, fmt_iso
from trader.core.models import Signal, Snapshot, Trade, TriggerEvent
from trader.data.conditions import EXCLUDED_CONDITIONS, counts_for_price
from trader.data.stream import MarketEvent
from trader.memory.journal import Journal
from trader.strategy.pricing import is_chased

log = structlog.get_logger(__name__)

__all__ = ["EXCLUDED_CONDITIONS", "TriggerEngine", "counts_for_price", "next_batch"]


@dataclass(frozen=True)
class ArmResult:
    armed: tuple[Signal, ...]
    triggered: tuple[TriggerEvent, ...]  # already at or above the trigger: served at once
    missed: tuple[Signal, ...]  # already past the chase guard


@dataclass(frozen=True)
class RestartPlan:
    rearmed: ArmResult
    reserved: tuple[tuple[BookName, TriggerEvent], ...] = ()  # undecided triggers, served again
    lost: tuple[tuple[BookName, Signal], ...] = ()  # undecided triggers past last_entry


@dataclass
class TriggerEngine:
    """Arms the day's long signals and fires each at most once (S§3.3)."""

    clock: Clock
    journal: Journal
    books: Sequence[BookName]
    window_start: datetime  # the opening range's end (09:35:00): earlier prints never trigger
    last_entry: datetime
    queues: dict[BookName, asyncio.Queue[TriggerEvent]] = field(init=False)
    _armed: dict[str, Signal] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.queues = {book: asyncio.Queue() for book in self.books}

    @property
    def armed_symbols(self) -> list[str]:
        return sorted(self._armed)

    # ---------------------------------------------------------------- arming

    async def arm(self, signals: Sequence[Signal], snapshots: Mapping[str, Snapshot]) -> ArmResult:
        """Arm the ``armed`` longs, applying the arm check against each one's snapshot."""
        now = self.clock.now()
        armed: list[Signal] = []
        missed: list[Signal] = []
        fired: list[TriggerEvent] = []
        for sig in sorted(signals, key=lambda s: s.rel_vol_rank):
            if sig.direction != "long" or sig.status != "armed":
                continue
            if sig.trigger_price is None or sig.max_chase_price is None:
                continue
            last = _snapshot_trade(snapshots.get(sig.symbol), self.window_start)
            if last is not None and last.price >= sig.trigger_price:
                if is_chased(last.price, sig.max_chase_price):
                    missed.append(sig)
                    continue
                fired.append(_event(sig, last, now, "arm_check"))
                continue
            armed.append(sig)
        for sig in armed:
            self._armed[sig.symbol] = sig
        await self.journal.write(lambda conn: _arm_rows(conn, armed, fired, missed, now))
        for ev in fired:
            self._dispatch(ev)
        log.info("armed", armed=len(armed), immediate=len(fired), missed=len(missed))
        return ArmResult(armed=tuple(armed), triggered=tuple(fired), missed=tuple(missed))

    async def rearm(
        self,
        signals: Sequence[Signal],
        snapshots: Mapping[str, Snapshot],
        *,
        with_orders: frozenset[str],
        decided: Mapping[str, frozenset[BookName]],
    ) -> RestartPlan:
        """After a crash: re-arm what never triggered (and has no orders), and re-serve or close
        out triggers a book never decided. Never re-triggers a signal that has orders."""
        now = self.clock.now()
        waiting = [s for s in signals if s.status == "armed" and s.id not in with_orders]
        result = await self.arm(waiting, snapshots)
        reserved: list[tuple[BookName, TriggerEvent]] = []
        lost: list[tuple[BookName, Signal]] = []
        for sig in sorted(signals, key=lambda s: s.rel_vol_rank):
            if sig.status != "triggered" or sig.id in with_orders:
                continue
            for book in self.books:
                if book in decided.get(sig.id, frozenset()):
                    continue
                if now < self.last_entry and sig.trigger_price is not None:
                    ev = TriggerEvent(
                        signal_id=sig.id,
                        symbol=sig.symbol,
                        rank=sig.rel_vol_rank,
                        ts=sig.triggered_at or now,
                        received_at=now,
                        price=sig.trigger_price,
                        source="restart",
                    )
                    self.queues[book].put_nowait(ev)
                    reserved.append((book, ev))
                else:
                    lost.append((book, sig))
        return RestartPlan(rearmed=result, reserved=tuple(reserved), lost=tuple(lost))

    # ---------------------------------------------------------------- the stream

    async def run(self, events: "asyncio.Queue[MarketEvent]") -> None:
        """Fire triggers from a hub consumer queue until cancelled. One event that fails is
        logged and skipped, so the engine never dies mid-day."""
        while True:
            event = await events.get()
            try:
                if isinstance(event, Trade):
                    await self.on_trade(event)
            except Exception:
                log.exception("trigger check failed", symbol=getattr(event, "symbol", None))
            finally:
                events.task_done()

    async def on_trade(self, trade: Trade) -> TriggerEvent | None:
        sig = self._armed.get(trade.symbol)
        if sig is None or sig.trigger_price is None:
            return None
        now = self.clock.now()
        if now >= self.last_entry or trade.ts < self.window_start or not counts_for_price(trade):
            return None
        if trade.price < sig.trigger_price:
            return None
        del self._armed[trade.symbol]  # one-shot: before any await
        ev = _event(sig, trade, now, "stream_trade")
        try:
            await self.journal.write(lambda conn: _mark_triggered(conn, sig.id, now))
        except Exception:
            self._armed[trade.symbol] = sig  # not journaled, not dispatched: the next trade retries
            raise
        self._dispatch(ev)
        log.info("triggered", signal=sig.id, price=str(trade.price), rank=sig.rel_vol_rank)
        return ev

    async def expire(self) -> list[Signal]:
        """At last_entry: armed signals that never triggered become ``expired``."""
        gone = [self._armed[s] for s in sorted(self._armed)]
        self._armed.clear()
        ids = [s.id for s in gone]
        await self.journal.write(
            lambda conn: conn.executemany(
                "UPDATE signals SET status = 'expired' WHERE id = ? AND status = 'armed'",
                [(i,) for i in ids],
            )
        )
        return gone

    def _dispatch(self, ev: TriggerEvent) -> None:
        for queue in self.queues.values():
            queue.put_nowait(ev)


async def next_batch(queue: "asyncio.Queue[TriggerEvent]") -> list[TriggerEvent]:
    """Wait for a trigger, then take every other one already waiting; serve them by rank."""
    batch = [await queue.get()]
    while True:
        try:
            batch.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return sorted(batch, key=lambda ev: (ev.rank, ev.signal_id))


def _snapshot_trade(snapshot: Snapshot | None, window_start: datetime) -> Trade | None:
    trade = snapshot.latest_trade if snapshot is not None else None
    if trade is None or trade.ts < window_start or not counts_for_price(trade):
        return None
    return trade


def _event(
    sig: Signal,
    trade: Trade,
    now: datetime,
    source: Literal["stream_trade", "arm_check"],
) -> TriggerEvent:
    return TriggerEvent(
        signal_id=sig.id,
        symbol=sig.symbol,
        rank=sig.rel_vol_rank,
        ts=trade.ts,
        received_at=now,
        price=trade.price,
        size=trade.size,
        conditions=trade.conditions,
        source=source,
    )


def _arm_rows(
    conn: sqlite3.Connection,
    armed: Sequence[Signal],
    fired: Sequence[TriggerEvent],
    missed: Sequence[Signal],
    now: datetime,
) -> None:
    at = fmt_iso(now)
    # COALESCE: re-arming after a restart keeps the original arming time.
    conn.executemany(
        "UPDATE signals SET armed_at = COALESCE(armed_at, ?) WHERE id = ?",
        [(at, s.id) for s in armed],
    )
    conn.executemany(
        "UPDATE signals SET armed_at = COALESCE(armed_at, ?), status = 'triggered', "
        "triggered_at = ? WHERE id = ?",
        [(at, at, ev.signal_id) for ev in fired],
    )
    conn.executemany(
        "UPDATE signals SET armed_at = COALESCE(armed_at, ?), status = 'missed_at_arm' "
        "WHERE id = ?",
        [(at, s.id) for s in missed],
    )


def _mark_triggered(conn: sqlite3.Connection, signal_id: str, now: datetime) -> None:
    conn.execute(
        "UPDATE signals SET status = 'triggered', triggered_at = ? WHERE id = ? "
        "AND status = 'armed'",
        (fmt_iso(now), signal_id),
    )
