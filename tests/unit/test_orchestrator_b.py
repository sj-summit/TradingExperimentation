"""Book B in the day (P4.8; D-068, D-069): its jobs at their times, its worker parking a trigger
whose verdict is pending, the boot record, and Book A untouched.

Whole synthetic days on a SimClock (``DayRun``), the model scripted. P4.9 adds the full set of
A+B scenarios and their golden day.
"""

import asyncio
import contextlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.dayrun import DayRun
from tests.fixtures.days import DAY, standard_day
from tests.fixtures.exec_harness import RISK, SETTINGS
from trader.books.base import BookDecision, Defer
from trader.books.verdicts import VerdictStore
from trader.core.clock import ET
from trader.core.models import Signal, TriggerEvent
from trader.data.recorder import Recorder, read_jsonl
from trader.llm.epoch import Fingerprint
from trader.llm.fake import Reply, Seen, answer_for, envelope
from trader.llm.replay import LLM_FILE
from trader.orchestrator.day import DayOrchestrator, _Book
from trader.orchestrator.llm_jobs import BookBJobs
from trader.orchestrator.replay import comparable, replay_day
from trader.orchestrator.simdrive import drive
from trader.reports.daily import build_daily

D = Decimal
# Whole days, two books each, some recorded and replayed: about 10 to 40 s apiece alone, more
# beside the rest of the suite.
pytestmark = pytest.mark.timeout(300)


def at(hhmmss: str) -> datetime:
    return datetime.combine(DAY, time.fromisoformat(hhmmss), ET)


async def ab_day(tmp_path: Path, **kw: Any) -> DayRun:
    return await DayRun(tmp_path, standard_day(), start=time(8, 55), books=("A", "B"), **kw).open()


async def test_book_b_trades_beside_book_a_on_a_neutral_day(tmp_path: Path) -> None:
    run = await ab_day(tmp_path)
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "DONE"
    a, b = await run.decisions("A"), await run.decisions("B")
    assert b == a  # every verdict neutral: Book B decides as Book A does
    assert a["WIN"] == "taken"
    assert a["CAP"] == "capacity_entries"
    calls = await run.rows("SELECT role, started_at, deadline, ok FROM llm_calls ORDER BY id")
    assert [(r["role"], r["started_at"][11:19], r["deadline"][11:19], r["ok"]) for r in calls] == [
        ("premarket", "09:05:00", "09:28:00", 1),
        ("vet", calls[1]["started_at"][11:19], "15:30:00", 1),
    ]
    assert "09:35:03" <= calls[1]["started_at"][11:19] < "09:35:30"  # at arming
    vetted = await run.rows("SELECT symbol, source, signal_id FROM verdicts ORDER BY symbol")
    assert [(r["symbol"], r["source"]) for r in vetted] == [
        ("CAP", "vet"), ("EXP", "vet"), ("GAPPER", "vet"), ("LOSE", "vet"), ("WIN", "vet"),
    ]  # fmt: skip
    assert all(r["signal_id"] == f"20261013-{r['symbol']}-orb5" for r in vetted)
    modes = await run.rows("SELECT book, mode, source, multiplier FROM day_modes")
    assert [tuple(r) for r in modes] == [("B", "normal", "premarket", "1")]
    taken = await run.rows(
        "SELECT s.symbol, d.llm_call_id FROM final_decisions d JOIN signals s ON s.id = "
        "d.signal_id WHERE d.book = 'B' AND d.reason_code = 'taken'"
    )
    assert {r["symbol"]: r["llm_call_id"] for r in taken} == {
        "WIN": "20261013-vet-01-1", "LOSE": "20261013-vet-01-1",
    }  # fmt: skip
    assert (await run.trades("B")).keys() == (await run.trades("A")).keys() == {"WIN", "LOSE"}
    assert [i for i in await run.incidents() if i[0] in ("P1", "P2")] == []
    boot = json.loads((tmp_path / "recordings" / "2026-10-13" / "boot.json").read_text("utf-8"))
    assert boot["llm"] == {"book_b": "on", "fingerprint": None, "transport": "scripted"}
    await run.close()


async def test_a_trigger_waits_for_a_slow_vet_then_enters(tmp_path: Path) -> None:
    run = await ab_day(tmp_path)
    run.llm_delay["vet"] = 55.0  # WIN triggers at 09:35:50, before the vet answers
    await run.run()
    b = await run.decisions("B")
    assert b["WIN"] == "entered_after_wait"
    assert (await run.decisions("A"))["WIN"] == "taken"
    rows = await run.rows(
        "SELECT d.decided_at FROM decisions d JOIN signals s ON s.id = d.signal_id "
        "WHERE d.book = 'B' AND s.symbol = 'WIN'"
    )
    vet = await run.rows("SELECT ended_at FROM llm_calls WHERE role = 'vet'")
    assert [r["decided_at"] for r in rows] == [vet[0]["ended_at"]]  # asked again at the answer
    assert "WIN" in await run.trades("B")
    await run.close()


def slow_repair(seen: Seen) -> Reply:
    """The vet's first try answers nonsense at once; its repair takes longer than any wait."""
    pack = seen.pack()
    if pack["role"] != "vet":
        return Reply(envelope=envelope(answer_for(pack)), delay_s=1.0)
    if seen.is_repair:
        return Reply(envelope=envelope(answer_for(pack)), delay_s=70.0)
    return Reply(stdout=b"not json", delay_s=5.0)


async def test_a_trigger_still_pending_at_its_deadline_is_vet_timeout(tmp_path: Path) -> None:
    """WIN triggers at 09:35:50 while the repair runs: at the call's start + 60 s it is
    ``vet_timeout``. The repair then times out, so LOSE (09:41:50) has no verdict to be had."""
    run = await ab_day(tmp_path, llm_script=slow_repair)
    await run.run()
    b = await run.decisions("B")
    assert (b["WIN"], b["LOSE"]) == ("vet_timeout", "llm_unavailable")
    assert (await run.decisions("A"))["WIN"] == "taken"
    vet = await run.rows("SELECT started_at, failure FROM llm_calls WHERE role = 'vet' "
                         "ORDER BY id")  # fmt: skip
    assert [r["failure"] for r in vet] == ["invalid_json", "timeout"]
    deadline = datetime.fromisoformat(vet[0]["started_at"]) + timedelta(seconds=60)
    win = await run.rows(
        "SELECT d.decided_at, d.detail, d.llm_call_id FROM decisions d JOIN signals s "
        "ON s.id = d.signal_id WHERE d.book = 'B' AND s.symbol IN ('WIN', 'LOSE') ORDER BY s.symbol"
    )
    lose, won = win
    assert datetime.fromisoformat(won["decided_at"]) == deadline
    assert f"no verdict by {deadline:%H:%M:%S}" in won["detail"]
    assert won["llm_call_id"] is None
    assert "the vet failed: timeout" in lose["detail"]
    assert lose["llm_call_id"] == "20261013-vet-01-2"
    assert [i for i in await run.incidents() if i[1].startswith("llm")] == [
        ("P3", "llm_call_failed"), ("P3", "llm_call_failed"),
    ]  # fmt: skip
    await run.close()


async def test_a_recorded_ab_day_replays_the_same(tmp_path: Path) -> None:
    """Decision 10: the replay serves the recorded packs and answers, each at its recorded
    time, so a trigger that waited for the vet waits as long, with no news, EDGAR or claude."""
    run = await ab_day(tmp_path, recording=True)
    run.verdicts = {"LOSE": "veto"}
    run.llm_delay["vet"] = 55.0  # WIN waits for its verdict
    await run.run()
    b = await run.decisions("B")
    assert (b["WIN"], b["LOSE"]) == ("entered_after_wait", "vetoed")
    await run.close()
    report = await replay_day(
        recordings=tmp_path / "recordings",
        day=DAY,
        out=tmp_path / "replay",
        settings=SETTINGS,
        risk=RISK,
        recorded_db=tmp_path / "trader.db",
    )
    assert (report.state, report.divergences, report.differences) == ("DONE", [], [])
    assert report.notes == []

    def compared(db: Path) -> dict[str, list[str]]:
        with closing(sqlite3.connect(db)) as conn:
            return comparable(conn, DAY)

    live = compared(tmp_path / "trader.db")
    assert {"day_mode", "verdict", "llm_call"} <= set(live)  # Book B's rows were compared
    assert compared(tmp_path / "replay" / "trader.db") == live
    assert (tmp_path / "replay" / "llm" / "2026-10-13" / "20261013-vet-01-1.output.json").exists()


async def test_a_replay_names_an_llm_answer_it_lacks(tmp_path: Path) -> None:
    run = await ab_day(tmp_path, recording=True)
    await run.run()
    await run.close()
    llm = tmp_path / "recordings" / "2026-10-13" / LLM_FILE
    kept = [r for r in read_jsonl(llm) if not (r["kind"] == "attempt" and r["role"] == "vet")]
    llm.unlink()
    recorder = Recorder(tmp_path / "recordings", DAY)
    for record in kept:
        recorder.append(LLM_FILE, record)
    recorder.close()
    report = await replay_day(recordings=tmp_path / "recordings", day=DAY,
                              out=tmp_path / "replay", settings=SETTINGS, risk=RISK,
                              recorded_db=tmp_path / "trader.db")  # fmt: skip
    # Book B then never traded, and the recorded broker answers it got no longer fit: the
    # replay diverges from there on, as any replay that lost a recorded answer does.
    assert "llm: a vet attempt the recording doesn't have" in report.divergences
    assert not report.matches
    assert "decision replayed but not recorded: 20261013-WIN-orb5 B 0 llm_unavailable" in (
        report.differences
    )


async def test_a_recorded_answer_the_replay_never_asked_for_is_named(tmp_path: Path) -> None:
    """On a day the engine ran once, an attempt the replay didn't make is one the live engine
    made and the replay lost: a divergence. After a restart (``boot-2.json``) the recording
    holds the restart's attempts too, and the replay covers the first run: only a note."""
    run = await ab_day(tmp_path, recording=True)
    await run.run()
    await run.close()
    folder = tmp_path / "recordings" / "2026-10-13"
    records = list(read_jsonl(folder / LLM_FILE))
    extra = next(r for r in reversed(records) if r["kind"] == "attempt")
    (folder / LLM_FILE).unlink()
    recorder = Recorder(tmp_path / "recordings", DAY)
    for record in [*records, extra]:
        recorder.append(LLM_FILE, record)
    recorder.close()
    left = "the replay left 1 recorded LLM attempts and 0 packs unused"

    async def replay(out: str) -> Any:
        return await replay_day(recordings=tmp_path / "recordings", day=DAY, out=tmp_path / out,
                                settings=SETTINGS, risk=RISK,
                                recorded_db=tmp_path / "trader.db")  # fmt: skip

    once = await replay("replay")
    assert f"llm: {left}" in once.divergences
    assert left not in once.notes
    (folder / "boot-2.json").write_bytes((folder / "boot.json").read_bytes())
    restarted = await replay("replay-2")
    assert left in restarted.notes
    assert f"llm: {left}" not in restarted.divergences


async def test_book_b_off_leaves_a_day_of_book_a_alone(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), books=("A",)).open()
    await run.run(run.deps(book_b_off="--no-llm"))
    assert await run.rows("SELECT * FROM llm_calls") == []
    boot = json.loads((tmp_path / "recordings" / "2026-10-13" / "boot.json").read_text("utf-8"))
    assert boot["llm"] == {"book_b": "off: --no-llm"}
    await run.close()


async def test_book_b_needs_its_llm(tmp_path: Path) -> None:
    run = await ab_day(tmp_path)
    with pytest.raises(ValueError, match="Book B needs its LLM runner"):
        await run.run(run.deps(llm=None))
    await run.close()


async def test_the_news_starts_after_the_stock_stream_and_stops_with_the_day(
    tmp_path: Path,
) -> None:
    """Q2: the stock stream takes the first connection; and boot never waits for the news's
    backfill (D-069)."""
    run = await ab_day(tmp_path)
    hub_running: list[bool] = []
    run.news.on_start = lambda: hub_running.append(run.hub._task is not None)
    deps = run.deps()
    assert deps.llm is not None
    print_ = Fingerprint("f1ng3rpr1nt", {})
    await run.run(replace(deps, llm=replace(deps.llm, fingerprint=print_)))
    assert (run.news.started, hub_running, run.news.stopped) == ([timedelta(hours=24)], [True], 1)
    boot = json.loads((tmp_path / "recordings" / "2026-10-13" / "boot.json").read_text("utf-8"))
    assert boot["llm"]["fingerprint"] == "f1ng3rpr1nt"
    # Book B's packs carry its own start-of-day E
    vet = json.loads((tmp_path / "llm" / "2026-10-13" / "20261013-vet-01-1.input.json").read_text())
    assert vet["book_b"]["equity_usd"] == 1000.0
    await run.close()


async def test_a_restart_owes_book_b_the_trigger_it_was_waiting_on(tmp_path: Path) -> None:
    """D-069: Book A entered WIN while Book B waited for the vet, journaling nothing; then a
    crash. The restart re-serves WIN to Book B alone, which decides it, never restart_lost."""
    run = await ab_day(tmp_path)
    run.llm_delay["vet"] = 55.0
    assert await run.run(until="09:35:55") is None  # WIN triggered at 09:35:50: B waits
    assert await run.decisions("B") == {"CHASE": "missed_at_arm"}
    await run.wait_until("09:36:30")
    run.new_hub()
    await run.run()
    b = await run.decisions("B")
    assert b["WIN"] != "restart_lost"
    revet = await run.rows("SELECT id FROM llm_calls WHERE role = 'vet' ORDER BY id")
    assert [r["id"] for r in revet] == ["20261013-vet-01-1", "20261013-vet-02-1"]  # re-vetted
    a_win = await run.rows(
        "SELECT d.reason_code FROM decisions d JOIN signals s ON s.id = d.signal_id "
        "WHERE d.book = 'A' AND s.symbol = 'WIN' ORDER BY d.id"
    )
    assert [r["reason_code"] for r in a_win] == ["taken"]  # Book A's order stands: never again
    await run.close()


async def test_book_b_s_boot_failing_leaves_book_a_s_day_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await ab_day(tmp_path)

    async def broken(self: VerdictStore) -> None:
        raise sqlite3.DatabaseError("malformed")

    monkeypatch.setattr(VerdictStore, "load", broken)
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "DONE"
    assert (await run.decisions("A"))["WIN"] == "taken"
    assert set((await run.decisions("B")).values()) <= {"llm_degraded", "missed_at_arm"}
    assert await run.halts("B") == ["llm_degraded"]
    assert ("P2", "llm_job_failed") in await run.incidents()
    assert await run.rows("SELECT * FROM llm_calls") == []  # its jobs asked nothing
    stats = await run.rows("SELECT book FROM daily_stats ORDER BY book")
    assert [r["book"] for r in stats] == ["A", "B"]  # Book B's account still had its EOD
    await run.close()


async def test_the_review_runs_after_a_shadow_job_that_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await ab_day(tmp_path)
    reviewed: list[str] = []

    async def failing_shadow(self: DayOrchestrator) -> None:
        raise RuntimeError("no SIP bars")

    async def review(self: BookBJobs) -> None:
        reviewed.append(self._clock.now().strftime("%H:%M"))

    monkeypatch.setattr(DayOrchestrator, "_shadow", failing_shadow)
    monkeypatch.setattr(BookBJobs, "review", review)
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "REPORT"  # REVIEW failed: the day is left for a rerun
    assert reviewed == ["16:12"]  # Book B's review job still ran, after the shadow job
    await run.close()


async def test_a_day_stopped_during_the_shadow_job_never_starts_the_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shutdown (a crash, Ctrl-C) isn't a failed shadow job: it doesn't wait for the review,
    which would sleep to 16:15 and call the model while the engine stops (D-069)."""
    run = await ab_day(tmp_path)
    reviewed: list[str] = []

    async def slow_shadow(self: DayOrchestrator) -> None:
        await self._clock.sleep_until(at("16:14:00"))

    async def review(self: BookBJobs) -> None:
        reviewed.append(self._clock.now().strftime("%H:%M:%S"))

    monkeypatch.setattr(DayOrchestrator, "_shadow", slow_shadow)
    monkeypatch.setattr(BookBJobs, "review", review)
    assert await run.run(until="16:13:00") is None
    assert reviewed == []
    await run.close()


async def test_a_day_book_b_was_kept_off_says_why_in_its_report(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), books=("A",)).open()
    await run.run(run.deps(book_b_off="--no-llm"))
    md = (tmp_path / "reports" / "2026-10-13.md").read_text(encoding="utf-8")
    assert "- LLM calls: none (Book B off: --no-llm)" in md
    report = await build_daily(run.journal, DAY, books=("A",), mode="paper", session=None,
                               now=at("17:00:00"))  # regenerated: from the day's note  # fmt: skip
    assert report.book_b_off == "--no-llm"
    await run.close()


# ---------------------------------------------------------------- the worker and the stop


class Store:
    """The store's version and its wait, for the worker (a stand-in for ``VerdictStore``)."""

    def __init__(self) -> None:
        self.version = 0
        self._changed = asyncio.Event()

    def bump(self) -> None:
        self.version += 1
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait_for_change(self, version: int) -> int:
        while self.version == version:
            await self._changed.wait()
        return self.version


class Scripted:
    """A Book B stand-in: each signal's answers in turn (``defer``, a reason code, or an
    exception to raise), recording every ask."""

    name = "B"

    def __init__(self, answers: dict[str, list[Any]], until: datetime) -> None:
        self.answers, self.until = answers, until
        self.asked: list[tuple[str, bool]] = []

    async def on_trigger(
        self, ev: TriggerEvent, sig: Signal, *, waited: bool = False
    ) -> BookDecision | Defer:
        self.asked.append((sig.symbol, waited))
        answer = self.answers[sig.symbol].pop(0)
        if isinstance(answer, Exception):
            raise answer
        if answer == "defer":
            return Defer(until=self.until)
        return BookDecision(book="B", signal_id=sig.id, take=False, reason=answer)


class Worker:
    """Book B's worker (``DayOrchestrator._work``) on a scripted book, a queue and a store."""

    def __init__(self, run: DayRun, book: Scripted, *symbols: str) -> None:
        self.run, self.book = run, book
        self.orch = DayOrchestrator(run.deps())
        self.store = Store()
        self.queue: asyncio.Queue[TriggerEvent] = asyncio.Queue()
        self.b = _Book(name="B", broker=run.sims["A"], records=None, service=None,  # type: ignore[arg-type]
                       policy=book, equity_sod=D(1000), store=self.store)  # type: ignore[arg-type]  # fmt: skip
        self.sigs = {s: _signal(s, rank) for rank, s in enumerate(symbols, 1)}
        self.orch._signals = {s.id: s for s in self.sigs.values()}
        self.orch._books = {"B": self.b}

    def trigger(self, symbol: str) -> None:
        self.queue.put_nowait(_event(self.sigs[symbol]))

    async def at(self, hhmmss: str) -> None:
        await self.run.clock.sleep_until(at(hhmmss))


async def drive_worker(w: Worker, steps: Any) -> None:
    async def job() -> None:
        w.b.worker = asyncio.ensure_future(w.orch._work(w.b, w.queue))
        await steps(w)
        w.b.worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await w.b.worker

    await drive(w.run.clock, job(), watched=[w.run.journal])


async def test_the_worker_asks_parked_and_new_triggers_together_in_rank_order(
    tmp_path: Path,
) -> None:
    run = await DayRun(tmp_path, standard_day(), start=time(10, 0)).open()
    book = Scripted({"AAA": ["defer", "vetoed"], "BBB": ["taken"]}, until=at("10:01:00"))
    w = Worker(run, book, "AAA", "BBB")

    async def steps(w: Worker) -> None:
        w.trigger("AAA")
        await w.at("10:00:05")
        w.trigger("BBB")  # a new trigger and a change in the store, together
        w.store.bump()
        await w.at("10:00:10")

    await drive_worker(w, steps)
    assert book.asked == [("AAA", False), ("AAA", True), ("BBB", False)]
    assert w.b.parked == {}
    await run.close()


async def test_a_parked_trigger_is_asked_again_at_its_deadline(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), start=time(10, 0)).open()
    book = Scripted({"AAA": ["defer", "vet_timeout"]}, until=at("10:01:00"))
    w = Worker(run, book, "AAA")

    async def steps(w: Worker) -> None:
        w.trigger("AAA")
        await w.at("10:00:02")
        w.trigger("AAA")  # the same signal again while it waits: never asked as new
        await w.at("10:00:30")
        assert book.asked == [("AAA", False)]
        await w.at("10:01:05")

    await drive_worker(w, steps)
    assert book.asked == [("AAA", False), ("AAA", True)]  # at 10:01:00, its deadline
    await run.close()


async def test_a_trigger_whose_ask_raised_is_not_parked_any_more(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), start=time(10, 0)).open()
    book = Scripted({"AAA": ["defer", RuntimeError("a bug"), "taken"]}, until=at("10:05:00"))
    w = Worker(run, book, "AAA")

    async def steps(w: Worker) -> None:
        w.trigger("AAA")
        await w.at("10:00:05")
        w.store.bump()  # asked again: it raises
        await w.at("10:00:10")
        w.store.bump()  # not parked any more: not asked
        await w.at("10:00:15")

    await drive_worker(w, steps)
    assert book.asked == [("AAA", False), ("AAA", True)]
    assert ("P2", "trigger_failed") in await run.incidents()
    await run.close()


async def test_the_stop_settles_every_parked_trigger_in_rank_order(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day()).open()
    book = Scripted({"AAA": ["vet_timeout"], "BBB": ["vet_timeout"], "CCC": ["defer"]},
                    until=at("15:30:00"))  # fmt: skip
    w = Worker(run, book, "AAA", "BBB", "CCC")
    for symbol in ("CCC", "BBB", "AAA"):  # parked out of rank order
        w.b.parked[w.sigs[symbol].id] = (_event(w.sigs[symbol]), at("15:30:00"))
    w.b.worker = asyncio.ensure_future(asyncio.sleep(3600))
    await w.orch._stop_trading()
    assert book.asked == [("AAA", True), ("BBB", True), ("CCC", True)]  # by rank, waited
    assert w.b.parked == {}  # CCC still deferred: cleared, left undecided for EOD
    await run.close()


def _signal(symbol: str, rank: int) -> Signal:
    return Signal(id=f"20261013-{symbol}-orb5", date=DAY, symbol=symbol, direction="long",
                  status="triggered", rel_vol=2.0, rel_vol_rank=rank, or_open=D(10),
                  or_high=D(11), or_low=D(9), or_close=D(10), or_volume=1, atr14=D(1),
                  adv14=1, prev_close=D(10), gap_pct=0.0, trigger_price=D("11.01"),
                  stop_price=D("10.91"), max_chase_price=D("11.03"))  # fmt: skip


def _event(sig: Signal) -> TriggerEvent:
    return TriggerEvent(signal_id=sig.id, symbol=sig.symbol, rank=sig.rel_vol_rank,
                        ts=at("09:40:00"), received_at=at("09:40:00"), price=D("11.01"),
                        source="stream_trade")  # fmt: skip
