"""The watchdog (RISK.md §6; the Phase 6 plan's decisions 11-14; DECISIONS D-082).

``trader watchdog`` is one short process, which Task Scheduler starts every minute from 09:25 to
16:05 ET on weekdays (the human's decision of 2026-11-23). It is independent of the engine.

- **The window:** a trading day from 5 minutes before its open to 5 minutes after its close,
  early closes included. Outside it, or on a holiday, a run exits at once.
- **The heartbeat** (``orchestrator/heartbeat.py``) is fresh while younger than 60 s. One that is
  absent, unreadable or stale is read again 25 s later before anything is done: a beat can be
  skipped, and a read can collide with the engine's replace.
- **Each book with keys** is read: its positions and open orders. Its shares are covered by the
  engine's own R-020 rule (``service.stop_works``, ``exit_at_work``). After the close, only a
  working GTC sell stop covers them (``service.overnight_stop``).
- **The rules,** by the heartbeat and the time of day:
  - **Stale, before the open:** a P2 ``engine_down``; shares with no working stop get GTC stops,
    with a P1 ``watchdog_unprotected``.
  - **Stale, in the session:** each book with anything open is flattened, with a P1
    ``engine_down_flattened``. With nothing open, a P2 ``engine_down``.
  - **Stale, after the close:** shares without a GTC stop get one, with a P1
    ``watchdog_overnight``; else a P2 ``engine_down``, since the day's EOD waits.
  - **Fresh, before the close - 2 min:** shares with no working stop are flattened (before the
    open: GTC stops placed), with a P1 ``watchdog_unprotected``. A book the engine runs first gets
    ``partial_fill_cancel_max_s`` + 10 s to cover them, and none of this happens within 40 s of
    its flatten time, while the engine says it is flattening the book, or under someone's
    ``flattening`` marker. A book the engine doesn't run (missing from the heartbeat) gets no
    wait.
  - **From close - 2 min to the close, either way:** every book holding shares is flattened, with
    a P1 ``watchdog_late`` unless ``not_flat`` was raised for it today.
  - **Fresh, after the close:** a book the engine runs is left to its after-close pass; a book
    it doesn't run gets GTC stops for shares without one, with a P1 ``watchdog_overnight``.
  - **The KILL file with the heartbeat stale, in the session:** every book with anything open is
    flattened under a ``kill_switch`` halt, with a P2 ``watchdog_kill_file``, as the engine would.
- **A flatten** goes through ``execution/flatten.py``: the halt (``p1_incident``, or
  ``kill_switch``), the ``flattening`` marker, and ``flatten_account`` with purpose ``incident``.
  Outside the session that places GTC stops instead of selling.
- **Alerts:** each incident (component ``watchdog``) carries a ``key`` in ``detail_json``. One
  whose key was already journaled today isn't journaled again; a P1 for an engine-down episode
  stops its P2. The watchdog pumps alerts only while the heartbeat is stale, so it never races a
  live engine's pump. A broker that can't be read is one P2 ``watchdog_blind`` per book per day.
- **runtime/watchdog.json** records each run, for the RUNBOOK and the engine (P6.2b).
"""

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import structlog

from trader.config import BookName, RiskConfig, Settings
from trader.core.calendar import (
    CalendarRangeError,
    NotATradingDayError,
    Session,
    TradingCalendar,
    day_times,
)
from trader.core.clock import Clock, fmt_iso
from trader.execution.broker import BrokerAdapter, Order, Position
from trader.execution.flatten import flatten_books
from trader.execution.service import FlattenResult, exit_at_work, overnight_stop, stop_works
from trader.memory.incidents import Severity, record_incident
from trader.memory.journal import Journal
from trader.notify import Notifier, pump_alerts
from trader.orchestrator.heartbeat import Beat, read_heartbeat
from trader.risk.breakers import HaltReason, Halts

log = structlog.get_logger(__name__)

Phase = Literal["before_open", "regular", "late", "after_close"]
ActionKind = Literal["flatten", "recheck", "alert"]
MARGIN = timedelta(minutes=5)  # the window: open - 5 min to close + 5 min
LATE_CHECK = timedelta(minutes=2)  # RISK.md §6: from close - 2 min, any position is flattened
REREAD = timedelta(seconds=25)  # a heartbeat that isn't fresh is read again before acting
STOP_CHECK_MARGIN = timedelta(seconds=40)  # no stop check this close to the engine's flatten
GRACE_EXTRA_S = 10  # the stop check waits partial_fill_cancel_max_s + this for the engine
CONFIRM = timedelta(seconds=2)  # a book no engine runs is read again this soon (D-083)
STATUS_NAME = "watchdog.json"
SOURCE = "trader watchdog"


def phase(now: datetime, session: Session) -> Phase | None:
    """Where ``now`` falls in the watchdog's window around ``session``; None outside it."""
    if now < session.open - MARGIN or now > session.close + MARGIN:
        return None
    if now < session.open:
        return "before_open"
    if now < session.close - LATE_CHECK:
        return "regular"
    if now < session.close:
        return "late"
    return "after_close"


@dataclass(frozen=True)
class BookView:
    book: BookName
    held: Mapping[str, int]  # symbol -> long shares
    uncovered: Mapping[str, int]  # shares no working stop or exit at work covers (R-020)
    no_gtc: Mapping[str, int]  # shares no working GTC sell stop covers (after the close)
    orders: int  # open orders, legs included
    managed: bool = False  # the engine runs this book: it is in a fresh heartbeat
    flattening: bool = False  # the engine says it is flattening this book
    marker: bool = False  # a ``flattening`` marker is in force: someone else's flatten

    @property
    def open(self) -> bool:
        return bool(self.held or self.orders)


def coverage(
    positions: Sequence[Position], orders: Sequence[Order]
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Each long position's shares; those no working stop or exit at work covers, by the engine's
    own R-020 rule; and those no working GTC sell stop covers."""
    held = {p.symbol: p.qty for p in positions if p.qty > 0}  # a short is never ours (R-002)
    uncovered: dict[str, int] = {}
    no_gtc: dict[str, int] = {}
    for symbol, qty in sorted(held.items()):
        mine = [o for o in orders if o.symbol == symbol]
        covered = sum(
            o.qty - o.filled_qty for o in mine if stop_works(o, orders) or exit_at_work(o)
        )
        overnight = sum(o.qty - o.filled_qty for o in mine if overnight_stop(o))
        if covered < qty:
            uncovered[symbol] = qty - covered
        if overnight < qty:
            no_gtc[symbol] = qty - overnight
    return held, uncovered, no_gtc


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    book: BookName | None = None
    severity: Severity | None = None
    code: str = ""
    message: str = ""
    halt: HaltReason = "p1_incident"
    key: str = ""  # the alert's dedup key (journaled in detail_json)


def _shares(held: Mapping[str, int]) -> str:
    return ", ".join(f"{symbol} {qty}" for symbol, qty in sorted(held.items()))


def describe(beat: Beat) -> str:
    if beat.status == "stale" and beat.age_s is not None:
        return f"stale ({beat.age_s:.0f} s old)"
    return beat.status


def assess(
    ph: Phase,
    beat: Beat,
    views: Sequence[BookView],
    *,
    kill: bool,
    now: datetime,
    flatten_at: datetime,
    episode: str,
    rechecked: frozenset[BookName] = frozenset(),
    blind: Sequence[BookName] = (),
) -> list[Action]:
    """What this run does, by RISK.md §6's rules (above). ``blind``: books whose account
    couldn't be read this run."""
    if beat.fresh:
        return _engine_up(ph, views, now=now, flatten_at=flatten_at, rechecked=rechecked)
    return _engine_down(ph, describe(beat), views, kill=kill, episode=episode, blind=blind)


def _engine_down(
    ph: Phase,
    how: str,
    views: Sequence[BookView],
    *,
    kill: bool,
    episode: str,
    blind: Sequence[BookName] = (),
) -> list[Action]:
    out: list[Action] = []
    unread = f" (Book {', '.join(blind)} couldn't be read)" if blind else ""
    if ph in ("regular", "late") and kill:
        for v in views:
            if v.open:
                out.append(
                    Action(
                        "flatten",
                        v.book,
                        "P2",
                        "watchdog_kill_file",
                        f"the KILL file is there and the engine's heartbeat is {how}: Book "
                        f"{v.book} flattened ({_shares(v.held) or 'open orders only'})",
                        halt="kill_switch",
                        key=f"watchdog_kill_file:{v.book}:{episode}",
                    )
                )
        if out:
            return out
    if ph == "before_open":
        out.append(
            Action(
                "alert",
                None,
                "P2",
                "engine_down",
                f"the engine's heartbeat is {how} before the open: is run-day running?",
                key=f"engine_down:{episode}",
            )
        )
        out += [_unprotected(v, "before the open: GTC stops placed") for v in views if v.uncovered]
        return out
    if ph in ("regular", "late"):
        for v in views:
            if v.open:
                out.append(
                    Action(
                        "flatten",
                        v.book,
                        "P1",
                        "engine_down_flattened",
                        f"the engine's heartbeat is {how}: Book {v.book} flattened "
                        f"({_shares(v.held) or 'open orders only'})",
                        key=f"engine_down_flattened:{v.book}:{episode}",
                    )
                )
        if not out:
            out.append(
                Action(
                    "alert",
                    None,
                    "P2",
                    "engine_down",
                    f"the engine's heartbeat is {how}; nothing is open{unread}",
                    key=f"engine_down:{episode}",
                )
            )
        return out
    for v in views:  # after the close
        if v.no_gtc:
            out.append(
                Action(
                    "flatten",
                    v.book,
                    "P1",
                    "watchdog_overnight",
                    f"Book {v.book}: {_shares(v.no_gtc)} without a GTC stop after the close, "
                    f"and the engine's heartbeat is {how}: GTC stops placed",
                    key=f"watchdog_overnight:{v.book}",
                )
            )
    if not out:
        out.append(
            Action(
                "alert",
                None,
                "P2",
                "engine_down",
                f"the engine's heartbeat is {how} after the close: the day's EOD and report "
                "wait until run-day is started again",
                key=f"engine_down:{episode}",
            )
        )
    return out


def _unprotected(v: BookView, what: str) -> Action:
    whose = "a book the engine runs, after its wait" if v.managed else "a book no engine watches"
    return Action(
        "flatten",
        v.book,
        "P1",
        "watchdog_unprotected",
        f"Book {v.book}: {_shares(v.uncovered)} without a working stop ({whose}): {what}",
        key=f"watchdog_unprotected:{v.book}",
    )


def _engine_up(
    ph: Phase,
    views: Sequence[BookView],
    *,
    now: datetime,
    flatten_at: datetime,
    rechecked: frozenset[BookName],
) -> list[Action]:
    out: list[Action] = []
    if ph == "after_close":  # the engine's after-close pass protects its own books only
        for v in views:
            if v.no_gtc and not v.managed:
                out.append(
                    Action(
                        "flatten",
                        v.book,
                        "P1",
                        "watchdog_overnight",
                        f"Book {v.book}: {_shares(v.no_gtc)} without a GTC stop after the close, "
                        "in a book no engine watches: GTC stops placed",
                        key=f"watchdog_overnight:{v.book}",
                    )
                )
        return out
    if ph == "late":
        for v in views:
            if v.held:
                out.append(
                    Action(
                        "flatten",
                        v.book,
                        "P1",
                        "watchdog_late",
                        f"Book {v.book} still holds {_shares(v.held)} 2 minutes before the "
                        "close: flattened",
                        key=f"watchdog_late:{v.book}",
                    )
                )
        return out
    for v in views:
        if not v.uncovered or v.marker:
            continue
        if v.managed and (
            v.flattening or (ph == "regular" and now >= flatten_at - STOP_CHECK_MARGIN)
        ):
            continue  # the engine is flattening, or is about to
        if v.book not in rechecked:  # read twice: the engine's wait, or a race between reads
            out.append(Action("recheck", v.book))
            continue
        what = "before the open: GTC stops placed" if ph == "before_open" else "flattened"
        out.append(_unprotected(v, what))
    return out


# ---------------------------------------------------------------- one run


@dataclass(frozen=True)
class WatchdogDeps:
    clock: Clock
    journal: Journal
    calendar: TradingCalendar
    brokers: Mapping[BookName, BrokerAdapter]
    notifier: Notifier
    settings: Settings
    risk: RiskConfig
    heartbeat: Path
    status: Path
    kill_file: Path
    pause: Callable[[float], None] = time.sleep  # between the heartbeat's read attempts


@dataclass
class RunReport:
    at: datetime
    verdict: str
    phase: Phase | None = None
    beat: Beat | None = None
    views: list[BookView] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    results: dict[str, str] = field(default_factory=dict)  # book -> how its flatten ended

    def line(self) -> str:
        """The run in one line, for the scheduled log."""
        parts = [f"watchdog {fmt_iso(self.at)}: {self.verdict}"]
        if self.beat is not None:
            parts.append(f"heartbeat {describe(self.beat)}")
        for v in self.views:
            parts.append(
                f"Book {v.book}: {_shares(v.held) or 'flat'}"
                + (f", bare {_shares(v.uncovered)}" if v.uncovered else "")
            )
        parts += [f"Book {book}: {how}" for book, how in sorted(self.results.items())]
        return "; ".join(parts)


class Watchdog:
    def __init__(self, deps: WatchdogDeps) -> None:
        self._d = deps
        self._halts = Halts(deps.journal, deps.clock)
        self._flatten_at: datetime | None = None

    async def run(self) -> RunReport:
        d = self._d
        now = d.clock.now()
        try:
            session = d.calendar.session(now.date())
        except (CalendarRangeError, NotATradingDayError):
            return self._done(RunReport(now, "not a trading day"))
        if phase(now, session) is None:
            return self._done(RunReport(now, "outside the window"))
        beat = self._beat()
        if not beat.fresh:
            await d.clock.sleep_until(d.clock.now() + REREAD)
            beat = self._beat()
        views = await self._views(beat, set(d.brokers))
        now = d.clock.now()
        ph = phase(now, session)
        if ph is None:
            return self._done(RunReport(now, "outside the window", beat=beat, views=views))
        times = day_times(
            session,
            entry_window_start=d.risk.entry_window_start,
            last_entry=d.settings.schedule.last_entry,
            flatten=d.settings.schedule.flatten,
        )
        episode = f"{now.date()}:{beat.status}" if beat.ts is None else fmt_iso(beat.ts)
        self._flatten_at = times.flatten
        kill = d.kill_file.exists()
        blind = sorted(set(d.brokers) - {v.book for v in views})
        first = assess(ph, beat, views, kill=kill, now=now, flatten_at=times.flatten,
                       episode=episode, blind=blind)  # fmt: skip
        pending = {a.book for a in first if a.kind == "recheck" and a.book}
        actions = [a for a in first if a.kind != "recheck"]
        results = await self._act(actions, beat, episode)  # nothing waits on a recheck
        quick = frozenset(v.book for v in views if v.book in pending and not v.managed)
        slow = frozenset(pending - quick)
        grace = timedelta(seconds=d.risk.partial_fill_cancel_max_s + GRACE_EXTRA_S)
        started = d.clock.now()
        for group, wait in ((quick, CONFIRM), (slow, grace)):
            if not group:
                continue
            await d.clock.sleep_until(started + wait)
            again_beat = self._beat()
            again = await self._views(again_beat, group)
            now = d.clock.now()
            again_phase = phase(now, session)
            if again_beat.fresh != beat.fresh or again_phase is None:
                continue  # the engine came or went meanwhile: the next run decides
            more = [
                a
                for a in assess(again_phase, again_beat, again, kill=kill, now=now,
                                flatten_at=times.flatten, episode=episode, rechecked=group)
                if a.kind != "recheck"
            ]  # fmt: skip
            views = [v for v in views if v.book not in group] + again
            results |= await self._act(more, beat, episode)
            actions += more
        if not beat.fresh:
            await pump_alerts(d.journal, d.notifier, d.clock)
        verdict = "acted" if actions else "ok"
        return self._done(RunReport(d.clock.now(), verdict, ph, beat, views, actions, results))

    # ---------------------------------------------------------------- reading

    def _beat(self) -> Beat:
        return read_heartbeat(self._d.heartbeat, self._d.clock.now(), pause=self._d.pause)

    async def _views(
        self, beat: Beat, books: set[BookName] | frozenset[BookName]
    ) -> list[BookView]:
        views: list[BookView] = []
        for book in sorted(books):
            broker = self._d.brokers[book]
            try:
                positions = await broker.positions()
                orders = await broker.open_orders()  # after the positions, as the engine reads
            except Exception as exc:
                log.warning("broker unreadable", book=book, error=f"{type(exc).__name__}: {exc}")
                await self._incident(
                    Action(
                        "alert",
                        book,
                        "P2",
                        "watchdog_blind",
                        f"Book {book}'s account couldn't be read: {type(exc).__name__}",
                        key=f"watchdog_blind:{book}",
                    ),
                    None,
                )
                continue
            held, uncovered, no_gtc = coverage(positions, orders)
            state: Mapping[str, Any] = (beat.books.get(book) or {}) if beat.fresh else {}
            marker = any(h.reason == "flattening" for h in await self._halts.active(book))
            views.append(
                BookView(
                    book=book,
                    held=held,
                    uncovered=uncovered,
                    no_gtc=no_gtc,
                    orders=len(orders),
                    managed=beat.fresh and book in beat.books,
                    flattening=bool(state.get("flattening")),
                    marker=marker,
                )
            )
        return views

    # ---------------------------------------------------------------- acting

    async def _act(self, actions: Sequence[Action], beat: Beat, episode: str) -> dict[str, str]:
        d = self._d
        flattens = [a for a in actions if a.kind == "flatten" and a.book is not None]
        results: dict[BookName, FlattenResult | None] = {}
        for reason in sorted({a.halt for a in flattens}):
            books: dict[BookName, BrokerAdapter] = {}
            for a in flattens:
                if a.halt == reason and a.book is not None:
                    books[a.book] = d.brokers[a.book]
            results.update(
                await flatten_books(
                    books,
                    journal=d.journal,
                    calendar=d.calendar,
                    fees=d.settings.fees,
                    clock=d.clock,
                    halt_reason=reason,
                    purpose="kill_switch" if reason == "kill_switch" else "incident",
                    source=SOURCE,
                )
            )
        for action in actions:
            if action.severity is None:
                continue
            halt = await self._halt_id(action) if action.kind == "flatten" else None
            await self._incident(
                action,
                results.get(action.book) if action.book else None,
                flattened=action.book in results,
                episode=None if beat.fresh else episode,
                halt=halt,
            )
        return {book: _ended(result) for book, result in results.items()}

    async def _halt_id(self, action: Action) -> int | None:
        """The halt this flatten stands under: one alert per halt, so a new incident after a
        human resume alerts again (D-083)."""
        row = await self._d.journal.fetch_one(
            "SELECT id FROM halts WHERE book = ? AND reason = ? AND cleared_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (action.book, action.halt),
        )
        return None if row is None else int(row["id"])

    async def _incident(
        self,
        action: Action,
        result: FlattenResult | None,
        *,
        flattened: bool = False,
        episode: str | None = None,
        halt: int | None = None,
    ) -> None:
        d = self._d
        now = d.clock.now()
        today = await d.journal.fetch_all(
            "SELECT ts, code, message, detail_json FROM incidents WHERE ts LIKE ?",
            (f"{now.date().isoformat()}T%",),
        )
        details = [json.loads(r["detail_json"] or "{}") for r in today]
        key = action.key if halt is None else f"{action.key}:halt-{halt}"
        if any(detail.get("key") == key for detail in details):
            return
        if (
            action.code == "engine_down"
            and episode is not None
            and any(detail.get("episode") == episode for detail in details)
        ):
            return  # something already said more about this engine's death
        since = fmt_iso(self._flatten_at) if self._flatten_at is not None else ""
        if action.code == "watchdog_late" and any(
            r["code"] == "not_flat"
            and r["ts"] >= since
            and (r["message"] or "").startswith(f"Book {action.book}")
            for r in today
        ):
            return  # the engine's own flatten already raised it for this book
        message = action.message
        if flattened:
            message += f" ({_ended(result)})"
        detail: dict[str, Any] = {"key": key, "book": action.book}
        if episode is not None:
            detail["episode"] = episode
        await record_incident(
            d.journal,
            at=now,
            severity=action.severity or "P2",
            component="watchdog",
            code=action.code,
            message=message,
            detail=detail,
        )

    # ---------------------------------------------------------------- the record

    def _done(self, report: RunReport) -> RunReport:
        record = {
            "ts": fmt_iso(report.at),
            "verdict": report.verdict,
            "phase": report.phase,
            "heartbeat": None
            if report.beat is None
            else {"status": report.beat.status, "age_s": report.beat.age_s, "pid": report.beat.pid},
            "books": {
                v.book: {"held": dict(v.held), "uncovered": dict(v.uncovered), "orders": v.orders}
                for v in report.views
            },
            "actions": [
                {"kind": a.kind, "book": a.book, "code": a.code, "severity": a.severity}
                for a in report.actions
            ],
            "results": report.results,
        }
        try:
            path = self._d.status
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + ".partial")
            partial.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
            os.replace(partial, path)  # the engine reads it (P6.2b)
        except OSError as exc:
            log.warning("watchdog status not written", error=str(exc))
        log.info("watchdog run", verdict=report.verdict, phase=report.phase)
        return report


def _ended(result: FlattenResult | None) -> str:
    if result is None:
        return "the flatten failed; see the watchdog's log"
    if result.flat:
        return "flat"
    held = _shares(result.remaining)
    if result.market_closed:
        bare = _shares(result.unprotected)
        return f"market closed; held {held} under GTC stops" + (
            f"; NO STOP on {bare}" if bare else ""
        )
    return f"not flat: {held}"
