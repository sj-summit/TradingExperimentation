"""``trader replay --date D`` (A§4.2, P3.8b; DECISIONS D-049): a recorded day, run again offline
through the real orchestrator, then compared with what the journal recorded.

The replay gets a fresh folder, ``runtime/replay/<date>/``, holding its own database, report,
heartbeat and kill file. It is seeded from the recording's ``boot.json`` (the calendar slice,
each book's ledger, halts and state, the regime tags) and ``universe.parquet``:
- **Market data** comes from ``rest.jsonl.gz`` and ``stream.jsonl.gz``.
- **Each book's broker** comes from ``broker-<book>.jsonl.gz``.
- **The clock** is a SimClock started where the live engine started, and jumps from one
  recorded instant to the next.

It never notifies, never records and never touches a real account. Afterwards it lists every
divergence (a call the recording lacks) and every difference between the replay's signals, final
decisions and trades and the recorded ones. The replay covers the first engine of the day: after
a restart (``boot-2.json``) it says so.
"""

import dataclasses
import hashlib
import json
import shutil
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader.config import BookName, RiskConfig, Settings
from trader.core.calendar import Session, TradingCalendar
from trader.core.clock import SimClock, fmt_iso, parse_et_iso
from trader.core.events import Event
from trader.core.models import Quote, Trade
from trader.data.recorder import (
    BOOT_FILE,
    REST_FILE,
    STREAM_FILE,
    UNIVERSE_FILE,
    broker_file,
    read_table,
)
from trader.data.replay import ReplayHub, ReplayMarketData, load_stream
from trader.data.stream import MarketEvent
from trader.execution.broker import BrokerAdapter
from trader.execution.replay import ReplayBroker
from trader.execution.sim_broker import SimBroker
from trader.memory.journal import Journal
from trader.memory.regime import RegimeTags, store_regime
from trader.notify import NullNotifier
from trader.orchestrator.day import Cadence, DayDeps, DayOrchestrator, DayPaths
from trader.orchestrator.simdrive import drive
from trader.risk.ledger import LedgerEntry, insert_entries

CONFIG_FILES = {
    "settings": "config/settings.yaml",
    "risk": "config/risk.yaml",
    "events": "config/events.yaml",
}


@dataclasses.dataclass(frozen=True)
class ReplayReport:
    day: date
    folder: Path
    state: str
    divergences: list[str]
    differences: list[str]
    notes: list[str]

    @property
    def matches(self) -> bool:
        return not self.divergences and not self.differences

    def lines(self) -> list[str]:
        out = [f"replay {self.day}: {self.state}; folder {self.folder}", *self.notes]
        out += [f"divergence: {d}" for d in self.divergences]
        out += [f"difference: {d}" for d in self.differences]
        out.append(
            "the replay matches the recorded day"
            if self.matches
            else f"{len(self.divergences)} divergences, {len(self.differences)} differences"
        )
        return out


async def replay_day(
    *,
    recordings: Path,
    day: date,
    out: Path,
    settings: Settings,
    risk: RiskConfig,
    events: Sequence[Event] = (),
    recorded_db: Path | None = None,
    root: Path | None = None,
    cadence: Cadence | None = None,
) -> ReplayReport:
    folder = recordings / day.isoformat()
    boot, universe, notes = _prepare(folder, out, root)
    clock = SimClock(parse_et_iso(boot["started_at"]))
    journal = await Journal.open(out / "trader.db", inline=True)
    try:
        calendar = await seed(journal, boot, universe)
        market = ReplayMarketData.from_file(folder / REST_FILE, clock)
        hub = ReplayHub(
            clock=clock,
            events=load_stream(folder / STREAM_FILE),
            symbol_limit=settings.data.stream_symbol_limit,
        )
        brokers: dict[BookName, ReplayBroker] = {}
        for book in sorted(boot["books"]):
            fallback = SimBroker(
                clock=clock, book=book, cash=Decimal(str(boot["books"][book]["broker_cash"]))
            )
            hub.add_tap(_feeder(fallback))
            brokers[book] = ReplayBroker.from_file(
                folder / broker_file(book), clock, fallback=fallback
            )
        adapters: dict[BookName, BrokerAdapter] = dict(brokers)
        deps = DayDeps(
            settings=settings,
            risk=risk,
            clock=clock,
            journal=journal,
            calendar=calendar,
            market=market,
            hub=hub,
            brokers=adapters,
            notifier=NullNotifier(),
            events=tuple(events),
            paths=DayPaths(
                kill_file=out / "KILL",
                heartbeat=out / "heartbeat.json",
                reports_dir=out / "reports",
                backups_dir=out / "backups",
            ),
            cadence=cadence or Cadence(),
        )
        outcome = await drive(clock, DayOrchestrator(deps).run(), watched=[journal])
        divergences = list(market.divergences) + [
            f"Book {name}: {d}" for name, b in sorted(brokers.items()) for d in b.divergences
        ]
        replayed = await journal.read(lambda conn: comparable(conn, day))
    finally:
        await journal.aclose()
    recorded = _recorded(recorded_db, day)
    differences = [] if recorded is None else diff(recorded, replayed)
    if recorded is None:
        notes.append("no recorded journal to compare with")
    return ReplayReport(
        day=day,
        folder=out,
        state=outcome.state,
        divergences=divergences,
        differences=differences,
        notes=notes,
    )


async def seed(
    journal: Journal, boot: Mapping[str, Any], universe: Sequence[Mapping[str, Any]]
) -> TradingCalendar:
    """Give a fresh database the state the recorded engine booted with."""
    sessions = [Session.model_validate(s) for s in boot["calendar"]]
    first, last = sessions[0].date, sessions[-1].date
    at = str(boot["started_at"])
    regime = boot.get("regime")

    def write(conn: sqlite3.Connection) -> None:
        conn.executemany(
            "INSERT INTO trading_days (date, open_ts, close_ts, is_early_close, settlement_date) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    s.date.isoformat(),
                    fmt_iso(s.open),
                    fmt_iso(s.close),
                    int(s.is_early_close),
                    s.settlement_date.isoformat() if s.settlement_date else None,
                )
                for s in sessions
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [("calendar_first", first.isoformat()), ("calendar_last", last.isoformat())],
        )
        conn.executemany(
            "INSERT INTO universe_daily (date, symbol, name, exchange, prev_close, atr14, adv14, "
            "or_vol_avg14, eligible, exclusion_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    str(r["date"]),
                    r["symbol"],
                    r["name"],
                    r["exchange"],
                    r["prev_close"],
                    r["atr14"],
                    r["adv14"],
                    r["or_vol_avg14"],
                    int(bool(r["eligible"])),
                    r["exclusion_reason"],
                )
                for r in universe
            ],
        )
        if regime:
            store_regime(conn, _regime(regime), parse_et_iso(at))
        for name, book in sorted(boot["books"].items()):
            entries = [LedgerEntry.model_validate(e) for e in book["ledger"]]
            insert_entries(conn, entries, parse_et_iso(at))
            conn.executemany(
                "INSERT INTO halts (book, reason, scope, detail, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (name, h["reason"], h["scope"], h["detail"], h["created_at"], h["expires_at"])
                    for h in book["halts"]
                ],
            )
            state = book.get("book_state")
            if state:
                columns = sorted(state)
                conn.execute(
                    f"INSERT INTO book_state ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    [state[c] for c in columns],
                )

    await journal.write(write)
    return TradingCalendar(sessions, first, last)


def comparable(conn: sqlite3.Connection, day: date) -> dict[str, list[str]]:
    """What a replay must reproduce: signals, each book's final decision, and trades."""
    d = day.isoformat()
    signals = conn.execute(
        "SELECT id, direction, status, trigger_price, stop_price FROM signals WHERE date = ? "
        "ORDER BY id",
        (d,),
    ).fetchall()
    decisions = conn.execute(
        "SELECT f.signal_id, f.book, f.take, f.reason_code FROM final_decisions f JOIN signals s "
        "ON s.id = f.signal_id WHERE s.date = ? ORDER BY f.signal_id, f.book",
        (d,),
    ).fetchall()
    trades = conn.execute(
        "SELECT id, qty, entry_price, exit_price, exit_reason FROM trades WHERE entry_ts LIKE ? "
        "ORDER BY id",
        (f"{d}T%",),
    ).fetchall()
    return {
        "signal": [" ".join(str(v) for v in tuple(r)) for r in signals],
        "decision": [" ".join(str(v) for v in tuple(r)) for r in decisions],
        "trade": [" ".join(str(v) for v in tuple(r)) for r in trades],
    }


def diff(recorded: Mapping[str, list[str]], replayed: Mapping[str, list[str]]) -> list[str]:
    out: list[str] = []
    for kind in sorted(set(recorded) | set(replayed)):
        was, now = set(recorded.get(kind, [])), set(replayed.get(kind, []))
        out += [f"{kind} recorded but not replayed: {row}" for row in sorted(was - now)]
        out += [f"{kind} replayed but not recorded: {row}" for row in sorted(now - was)]
    return out


def _prepare(
    folder: Path, out: Path, root: Path | None
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Read the recording's boot record and universe, and empty the replay's folder."""
    boot_path = folder / BOOT_FILE
    if not boot_path.exists():
        raise ValueError(f"no recording to replay: {boot_path} is missing")
    boot: dict[str, Any] = json.loads(boot_path.read_text(encoding="utf-8"))
    universe = read_table(folder / UNIVERSE_FILE) if (folder / UNIVERSE_FILE).exists() else []
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    return boot, universe, _notes(folder, boot, root)


def _recorded(recorded_db: Path | None, day: date) -> dict[str, list[str]] | None:
    if recorded_db is None or not recorded_db.exists():
        return None
    with closing(sqlite3.connect(f"file:{recorded_db.as_posix()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return comparable(conn, day)


def _notes(folder: Path, boot: Mapping[str, Any], root: Path | None) -> list[str]:
    notes: list[str] = []
    if (folder / "boot-2.json").exists():
        notes.append("the engine restarted that day: the replay covers its first run only")
    if root is not None:
        changed = [
            name
            for name, rel in CONFIG_FILES.items()
            if (root / rel).is_file()
            and boot.get(f"{name}_sha256")
            and hashlib.sha256((root / rel).read_bytes()).hexdigest() != boot[f"{name}_sha256"]
        ]
        if changed:
            notes.append(f"config changed since the recording: {', '.join(changed)}")
    return notes


def _regime(data: Mapping[str, Any]) -> RegimeTags:
    return RegimeTags(
        date=date.fromisoformat(str(data["date"])),
        spy_close=Decimal(str(data["spy_close"])),
        spy_sma20=Decimal(str(data["spy_sma20"])),
        spy_trend=data["spy_trend"],
        spy_atr_pct=float(data["spy_atr_pct"]),
        tercile_low=float(data["tercile_low"]),
        tercile_high=float(data["tercile_high"]),
        vol_bucket=data["vol_bucket"],
    )


def _feeder(sim: SimBroker) -> Any:
    def feed(event: MarketEvent) -> None:
        if isinstance(event, (Trade, Quote)):
            sim.feed(event)

    return feed


def started_at(boot: Mapping[str, Any]) -> datetime:
    return parse_et_iso(str(boot["started_at"]))
