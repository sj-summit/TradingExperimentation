"""Book B's verdicts (S§5.2-5.4; the Phase 4 plan's decision 8; DECISIONS D-068).

The store holds what Book B decides with:

- the day mode;
- each symbol's verdict;
- which vet job covers which symbol;
- the day's failed attempts.

**Who judges what.** The brief (09:05) sets the day mode and judges the pre-market candidates.
The vet (09:35:05) judges every armed long without a pre-market verdict, or every armed long
when ``revet_premarket`` is on; ``vet_targets`` picks them.

**What ``standing(signal)`` returns** for Book B to decide with:

- **A verdict.** A pre-market one stands unless ``revet_premarket``; otherwise the vet's.
  Under ``revet_premarket``, a re-vet that fails, leaves the symbol out or runs past the deadline
  leaves the brief's verdict standing (``fallback``).
- **Pending:** a vet job covers the symbol and hasn't ended.
  - The deadline is max(armed_at, the call's start) + ``max_wait_s``, and never later than the
    last entry. Until the call starts, the time the vet was expected stands in for its start.
- **Unavailable:** the covering vet failed, left the symbol out, or had no room for it in its
  pack; or no call covers it.

**The journal first, then memory.** ``persist_brief`` and ``persist_vet`` are the runner's
``persist`` callbacks: they store an answer in the transaction that completes its attempt
(D-064). Memory follows when ``brief_done`` or ``vet_done`` gets the call's result. A default day
mode is stored before memory takes it. So memory is never ahead of the journal.

**A vet job** runs ``expect_vet`` (which returns the job), ``vet_started``, the runner's call, and
then ``vet_done``, or ``vet_abandoned`` if the call is cancelled. Each touches only its own job's
symbols.

**After a restart,** ``load`` rebuilds memory from ``day_modes``, ``verdicts``, ``llm_calls``,
the day's signals and Book B's decisions. Every armed long that no call judged, and that Book B
hasn't decided, is pending again from the restart, and ``load`` returns those symbols to be
vetted again. That covers:

- a call that a crash abandoned (its final attempt ``abandoned``, or its last attempt never
  final, D-064), which judged nothing;
- a vet the crash cut short before its first attempt.

The journal can't tell those apart from a vet refused before any attempt, or a symbol its pack
had no room for, so those are vetted again too.

**Degraded** at ``llm_failures_degrade_after`` failed attempts in the day (RISK.md §2). Book B
re-reads the count before each decision (``refresh_failures``); a call that ends does too.

Every change bumps ``version`` and wakes ``wait_for_change``, so P4.8's worker asks a parked
trigger again; otherwise it asks at the trigger's deadline.
"""

import asyncio
import json
import sqlite3
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel

from trader.config import BookName
from trader.core.clock import Clock, parse_et_iso
from trader.core.models import Signal
from trader.llm.budget import ABANDONED, failed_attempts
from trader.llm.client import LLMResult
from trader.llm.schemas import (
    DAY_MODE_MULTIPLIERS,
    DayMode,
    PremarketBrief,
    SymbolAssessment,
    Verdict,
    VetBatch,
)
from trader.memory.journal import Journal

VerdictSource = Literal["premarket", "vet"]
ModeSource = Literal["premarket", "default"]
CallState = Literal["ok", "failed", "abandoned"]
# Why a finished vet has no verdict for a symbol it covered, besides the call's own failure.
LEFT_OUT = "left_out"  # the answer didn't assess it
NOT_SENT = "not_sent"  # the pack had no room for it


@dataclass(frozen=True)
class DayModeState:
    mode: DayMode
    source: ModeSource
    multiplier: Decimal
    llm_call_id: str | None = None  # the brief's attempt whose result stands


DEFAULT_MODE = DayModeState("normal", "default", DAY_MODE_MULTIPLIERS["normal"])


@dataclass(frozen=True)
class StoredVerdict:
    symbol: str
    source: VerdictSource
    verdict: Verdict
    confidence: float
    llm_call_id: str  # the attempt whose answer it is


@dataclass(frozen=True)
class Standing:
    """What Book B can know about a signal now."""

    kind: Literal["verdict", "pending", "unavailable"]
    verdict: StoredVerdict | None = None
    deadline: datetime | None = None  # pending
    # pending under revet_premarket: the brief's verdict, should the re-vet not answer in time
    fallback: StoredVerdict | None = None
    llm_call_id: str | None = None  # the verdict's attempt, or the attempt that failed
    detail: str = ""


@dataclass(frozen=True)
class VetJob:
    """One vet call, from ``expect_vet`` to ``vet_done``: the symbols it is to judge."""

    number: int
    symbols: tuple[str, ...]


@dataclass(frozen=True)
class Rebuilt:
    """What ``load`` found that needs the orchestrator."""

    brief: Literal["none", "ok", "failed", "abandoned"]
    revet: tuple[str, ...]  # pending again: symbols to vet again


@dataclass(frozen=True)
class _Cover:
    """A vet covering one symbol."""

    since: datetime  # the call's start, or when the vet was expected
    job: int | None = None  # the job it waits on; None: to be vetted again
    started: bool = False
    done: bool = False
    call_id: str | None = None  # once done: the attempt whose result stands
    failure: str | None = None  # done without a verdict: the call's failure, LEFT_OUT, NOT_SENT


class VerdictStore:
    def __init__(
        self,
        journal: Journal,
        clock: Clock,
        *,
        day: date,
        max_wait_s: int,
        revet_premarket: bool,
        degrade_after: int,
        last_entry: datetime | None = None,
        book: BookName = "B",
    ) -> None:
        self._journal, self._clock, self._day, self._book = journal, clock, day, book
        self._max_wait = timedelta(seconds=max_wait_s)
        self._revet = revet_premarket
        self._last_entry = last_entry
        self.degrade_after = degrade_after
        self.failures = 0
        self.version = 0
        self._changed = asyncio.Event()
        self._jobs = 0
        self._mode: DayModeState | None = None
        self._pre: dict[str, StoredVerdict] = {}
        self._vet: dict[str, StoredVerdict] = {}
        self._covers: dict[str, _Cover] = {}
        # symbol -> today's signal id, for the vet's rows; replaced whole, never changed, as
        # persist_vet reads it on the journal's thread
        self._signal_ids: Mapping[str, str] = MappingProxyType({})

    # ------------------------------------------------------------ what Book B reads

    @property
    def degraded(self) -> bool:
        return self.failures >= self.degrade_after

    def day_mode(self) -> DayModeState:
        """The brief's day mode; normal by default, until or unless the brief sets one."""
        return self._mode or DEFAULT_MODE

    def standing(self, sig: Signal) -> Standing:
        symbol = sig.symbol
        pre, vet = self._pre.get(symbol), self._vet.get(symbol)
        cover = self._covers.get(symbol)
        if pre is not None and not self._revet:
            return _verdict(pre)
        if vet is not None:
            return _verdict(vet)
        if cover is not None and not cover.done:
            armed = sig.armed_at or cover.since
            deadline = max(armed, cover.since) + self._max_wait
            if self._last_entry is not None:
                deadline = min(deadline, self._last_entry)
            what = "under way" if cover.started else "expected"
            return Standing("pending", deadline=deadline, fallback=pre,
                            detail=f"the vet is {what} since {cover.since:%H:%M:%S}")  # fmt: skip
        if pre is not None:  # revet_premarket: the re-vet failed or left it out
            return _verdict(pre)
        if cover is not None:
            return Standing("unavailable", llm_call_id=cover.call_id, detail=_why(cover))
        return Standing("unavailable", detail="no LLM call covers it")

    def vet_targets(self, signals: Iterable[Signal]) -> list[Signal]:
        """The armed longs the vet should judge (S§5.3): those with no verdict, or with only
        the brief's when ``revet_premarket``, and no vet done or under way."""
        out = []
        for s in signals:
            if s.direction != "long" or s.status not in ("armed", "triggered"):
                continue
            if s.symbol in self._vet or (s.symbol in self._pre and not self._revet):
                continue
            cover = self._covers.get(s.symbol)
            if cover is not None and (cover.done or cover.started):
                continue
            out.append(s)
        return out

    async def wait_for_change(self, version: int) -> int:
        """Return once ``version`` is out of date, with the current one."""
        while self.version == version:
            await self._changed.wait()
        return self.version

    async def refresh_failures(self) -> int:
        """Re-read the day's failed attempts. RISK.md §2 counts attempts, so a call's first try
        can degrade Book B while its repair still runs. Book B calls this before each decision."""
        failures = await failed_attempts(self._journal, self._day)
        if failures != self.failures:
            self.failures = failures
            self._bump()
        return failures

    # ------------------------------------------------------------ the brief

    def persist_brief(self, conn: sqlite3.Connection, output: BaseModel, attempt_id: str) -> None:
        """The brief's ``persist``: its day mode and verdicts, with its attempt's row."""
        if not isinstance(output, PremarketBrief):
            raise TypeError(f"the brief answered with a {type(output).__name__}")
        epoch = _epoch(conn)
        conn.execute(
            "INSERT OR REPLACE INTO day_modes (date, book, mode, reason, llm_call_id, source, "
            "multiplier, eval_epoch) VALUES (?, ?, ?, ?, ?, 'premarket', ?, ?)",
            (self._day.isoformat(), self._book, output.day_mode, output.day_mode_reason,
             attempt_id, str(DAY_MODE_MULTIPLIERS[output.day_mode]), epoch),
        )  # fmt: skip
        for a in output.assessments:
            _insert_verdict(conn, self._day, a, "premarket", attempt_id, None, epoch)

    async def brief_done(self, result: LLMResult[Any]) -> None:
        """The brief's result, into memory; a failure stores the default day mode first."""
        if result.ok:
            output, call = result.output, result.call_id
            if not isinstance(output, PremarketBrief) or call is None:
                raise TypeError("a good brief must carry its answer and its attempt")
            self._mode = DayModeState(output.day_mode, "premarket",
                                      DAY_MODE_MULTIPLIERS[output.day_mode], call)  # fmt: skip
            self._pre.update({a.symbol: _stored(a, "premarket", call) for a in output.assessments})
        else:
            why = f"the brief failed: {result.failure or 'unknown'}"
            self._mode = await self._store_default(result.call_id, why)
        self._bump()
        await self.refresh_failures()

    async def brief_missed(self) -> None:
        """No brief ran today (a late start): the default day mode, stored first."""
        self._mode = await self._store_default(None, "no brief ran")
        self._bump()
        await self.refresh_failures()

    # ------------------------------------------------------------ the vet

    def expect_vet(self, signals: Iterable[Signal], *, at: datetime) -> VetJob:
        """A vet will judge these: pending from ``at`` until its job ends. Call it before their
        triggers can arrive."""
        picked = list(signals)
        self._jobs += 1
        job = VetJob(self._jobs, tuple(s.symbol for s in picked))
        ids = dict(self._signal_ids)
        for s in picked:
            self._covers[s.symbol] = _Cover(since=at, job=job.number)
            ids[s.symbol] = s.id
        self._signal_ids = MappingProxyType(ids)
        self._bump()
        return job

    def vet_started(self, job: VetJob, *, at: datetime) -> None:
        """The job's call is starting: its deadlines count from now (plan decision 8)."""
        for symbol, cover in self._open(job):
            if not cover.started:
                self._covers[symbol] = replace(cover, since=at, started=True)
        self._bump()

    def vet_abandoned(self, job: VetJob, *, at: datetime) -> None:
        """The job's call was cancelled before it ended: its symbols are to be vetted again,
        pending from ``at``."""
        for symbol, _ in self._open(job):
            self._covers[symbol] = _Cover(since=at)
        self._bump()

    def persist_vet(self, conn: sqlite3.Connection, output: BaseModel, attempt_id: str) -> None:
        """The vet's ``persist``: its verdicts, each naming its signal, with its attempt's row.
        A signal id the journal doesn't know is stored as NULL, rather than failing every other
        verdict of the answer with it."""
        if not isinstance(output, VetBatch):
            raise TypeError(f"the vet answered with a {type(output).__name__}")
        epoch = _epoch(conn)
        ids = self._signal_ids
        for a in output.assessments:
            _insert_verdict(conn, self._day, a, "vet", attempt_id, ids.get(a.symbol), epoch)

    async def vet_done(self, job: VetJob, result: LLMResult[Any], *, sent: Collection[str]) -> None:
        """The job's result, for each symbol it was to judge. ``sent``: the pack's kept
        candidates (``EncodedPack.kept``)."""
        verdicts: dict[str, StoredVerdict] = {}
        if result.ok:
            output, call = result.output, result.call_id
            if not isinstance(output, VetBatch) or call is None:
                raise TypeError("a good vet must carry its answer and its attempt")
            verdicts = {a.symbol: _stored(a, "vet", call) for a in output.assessments}
        self._vet.update(verdicts)
        for symbol, cover in self._open(job):
            if symbol in verdicts:
                failure = None
            elif symbol not in sent:
                failure = NOT_SENT
            elif result.ok:
                failure = LEFT_OUT
            else:
                failure = result.failure or "failed"
            call_id = None if failure == NOT_SENT else result.call_id
            self._covers[symbol] = replace(cover, done=True, call_id=call_id, failure=failure)
        self._bump()
        await self.refresh_failures()

    def _open(self, job: VetJob) -> list[tuple[str, _Cover]]:
        """The job's symbols whose cover is still its own and not done."""
        out = []
        for symbol in job.symbols:
            cover = self._covers.get(symbol)
            if cover is not None and cover.job == job.number and not cover.done:
                out.append((symbol, cover))
        return out

    # ------------------------------------------------------------ after a restart

    async def load(self) -> Rebuilt:
        """Rebuild memory from the journal: at boot, after ``close_abandoned``, before any
        other call (the module's docstring)."""
        if self.version:
            raise RuntimeError("load() rebuilds a fresh store only")
        mode_row, verdict_rows, call_rows, signal_rows, decided = await self._journal.read(
            self._read
        )
        now = self._clock.now()
        pre: dict[str, StoredVerdict] = {}
        vet: dict[str, StoredVerdict] = {}
        for r in verdict_rows:
            stored = StoredVerdict(r["symbol"], r["source"], r["verdict"], r["confidence"],
                                   r["llm_call_id"])  # fmt: skip
            (pre if stored.source == "premarket" else vet)[stored.symbol] = stored

        def settled(symbol: str) -> bool:
            """A verdict stands, or Book B has decided its signal: nothing left to vet."""
            return symbol in vet or (symbol in pre and not self._revet) or symbol in decided

        calls: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in call_rows:
            calls[r["logical_id"]].append(r)
        brief: Literal["none", "ok", "failed", "abandoned"] = "none"
        brief_last: sqlite3.Row | None = None
        covers: dict[str, _Cover] = {}
        for attempts in calls.values():  # in the journal's order: NN, then attempt
            first, last = attempts[0], attempts[-1]
            state = _state(last)
            if first["role"] == "premarket":
                brief, brief_last = state, last
                continue
            if state == "abandoned":
                continue  # it judged nothing: its signals fall to the armed longs' rule below
            start = parse_et_iso(first["started_at"])
            for symbol in json.loads(first["candidates_json"] or "[]"):
                failure = last["failure"] if state == "failed" else None
                if failure is None and symbol not in vet:
                    failure = LEFT_OUT
                covers[symbol] = _Cover(since=start, started=True, done=True,
                                        call_id=last["id"], failure=failure)  # fmt: skip
        ids: dict[str, str] = {}
        for r in signal_rows:  # armed longs, by rank
            ids[r["symbol"]] = r["id"]
            if not settled(r["symbol"]) and r["symbol"] not in covers:
                covers[r["symbol"]] = _Cover(since=now)  # no call judged it: vet it again
        if brief == "failed" and mode_row is None and brief_last is not None:
            # a crash came between the failed brief and its default day mode: record it now
            why = f"the brief failed: {brief_last['failure']}"
            mode: DayModeState | None = await self._store_default(brief_last["id"], why)
        else:
            mode = _mode(mode_row) if mode_row is not None else None
        failures = await failed_attempts(self._journal, self._day)
        if self.version:
            raise RuntimeError("the store changed while load() read the journal")
        self._mode, self._pre, self._vet, self._covers = mode, pre, vet, covers
        self._signal_ids = MappingProxyType(ids)
        self.failures = failures
        self._bump()
        return Rebuilt(brief=brief, revet=tuple(s for s, c in covers.items() if not c.done))

    def _read(
        self, conn: sqlite3.Connection
    ) -> tuple[
        sqlite3.Row | None, list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row], set[str]
    ]:
        day = self._day.isoformat()
        mode = conn.execute(
            "SELECT mode, source, multiplier, llm_call_id FROM day_modes "
            "WHERE date = ? AND book = ?",
            (day, self._book),
        ).fetchone()
        verdicts = conn.execute(
            "SELECT v.symbol, v.source, v.verdict, v.confidence, v.llm_call_id FROM verdicts v "
            "JOIN llm_calls c ON c.id = v.llm_call_id "
            "WHERE v.date = ? AND c.purpose = 'trading' ORDER BY v.id",
            (day,),
        ).fetchall()
        calls = conn.execute(
            "SELECT id, logical_id, role, final, ok, failure, started_at, candidates_json "
            "FROM llm_calls WHERE date = ? AND purpose = 'trading' "
            "AND role IN ('premarket', 'vet') ORDER BY logical_id, attempt",
            (day,),
        ).fetchall()
        signals = conn.execute(
            "SELECT id, symbol FROM signals WHERE date = ? AND direction = 'long' "
            "AND status IN ('armed', 'triggered') ORDER BY rel_vol_rank, symbol",
            (day,),
        ).fetchall()
        decided = {  # the symbols whose signal Book B has decided today
            r["symbol"]
            for r in conn.execute(
                "SELECT s.symbol FROM final_decisions d JOIN signals s ON s.id = d.signal_id "
                "WHERE s.date = ? AND d.book = ?",
                (day, self._book),
            )
        }
        return mode, verdicts, calls, signals, decided

    # ------------------------------------------------------------ helpers

    async def _store_default(self, call_id: str | None, reason: str) -> DayModeState:
        """Store the default day mode unless the day already has one; return the day's."""

        def write(conn: sqlite3.Connection) -> sqlite3.Row:
            day = self._day.isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO day_modes (date, book, mode, reason, llm_call_id, source, "
                "multiplier, eval_epoch) VALUES (?, ?, 'normal', ?, ?, 'default', ?, ?)",
                (day, self._book, reason, call_id, str(DEFAULT_MODE.multiplier), _epoch(conn)),
            )
            row: sqlite3.Row = conn.execute(
                "SELECT mode, source, multiplier, llm_call_id FROM day_modes "
                "WHERE date = ? AND book = ?",
                (day, self._book),
            ).fetchone()
            return row

        return _mode(await self._journal.write(write))

    def _bump(self) -> None:
        self.version += 1
        self._changed.set()
        self._changed = asyncio.Event()


def _verdict(v: StoredVerdict) -> Standing:
    return Standing("verdict", verdict=v, llm_call_id=v.llm_call_id)


def _why(cover: _Cover) -> str:
    if cover.failure == NOT_SENT:
        return "the vet's pack had no room for it"
    if cover.failure == LEFT_OUT:
        return f"the vet's answer left it out ({cover.call_id})"
    call = f" ({cover.call_id})" if cover.call_id else ""
    return f"the vet failed: {cover.failure}{call}"


def _state(last: sqlite3.Row) -> CallState:
    """How a call ended, from its last attempt: abandoned if it never got a result (D-064).
    An attempt still open has ``final`` 0, as every attempt does until it completes."""
    if last["failure"] == ABANDONED or not last["final"]:
        return "abandoned"
    return "ok" if last["ok"] else "failed"


def _mode(row: sqlite3.Row) -> DayModeState:
    mode: DayMode = row["mode"]
    source: ModeSource = row["source"] or ("premarket" if row["llm_call_id"] else "default")
    multiplier = Decimal(row["multiplier"]) if row["multiplier"] else DAY_MODE_MULTIPLIERS[mode]
    return DayModeState(mode, source, multiplier, row["llm_call_id"])


def _stored(a: SymbolAssessment, source: VerdictSource, call_id: str) -> StoredVerdict:
    return StoredVerdict(a.symbol, source, a.verdict, a.confidence, call_id)


def _epoch(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'eval_epoch'").fetchone()
    return int(row[0]) if row else None


def _insert_verdict(
    conn: sqlite3.Connection,
    day: date,
    a: SymbolAssessment,
    source: VerdictSource,
    call_id: str,
    signal_id: str | None,
    epoch: int | None,
) -> None:
    conn.execute(
        "INSERT INTO verdicts (date, symbol, source, verdict, confidence, catalyst_type, "
        "catalyst_fresh, catalyst_confirmed, risk_flags_json, reason, llm_call_id, signal_id, "
        "eval_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "(SELECT id FROM signals WHERE id = ?), ?)",
        (day.isoformat(), a.symbol, source, a.verdict, a.confidence, a.catalyst_type,
         int(a.catalyst_fresh), int(a.catalyst_confirmed), json.dumps(list(a.risk_flags)),
         a.reason, call_id, signal_id, epoch),
    )  # fmt: skip
