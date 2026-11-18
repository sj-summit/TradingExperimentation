"""Book B's VerdictStore (books/verdicts.py; S§5.2-5.4; the Phase 4 plan's decision 8; D-068).

The calls go through the real runner (``LlmCalls``) on a scripted transport, so the rows, the
persist callbacks and the failure counts are the ones a trading day makes.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.exec_harness import SETTINGS
from tests.helpers import REPO
from tests.unit.test_llm_context import (
    BRIEF_AT,
    DAY,
    PREMARKET,
    VET,
    VET_AT,
    candidate,
    common,
    signal,
)
from trader.books.base import insert_decision
from trader.books.verdicts import (
    DEFAULT_MODE,
    DayModeState,
    Rebuilt,
    StoredVerdict,
    VerdictStore,
    VetJob,
)
from trader.core.clock import SimClock, et_datetime, fmt_iso
from trader.core.models import Signal
from trader.llm.budget import Budget
from trader.llm.client import LlmCalls, LLMResult
from trader.llm.context import EncodedPack, Evidence, VetCandidate, premarket_pack, vet_pack
from trader.llm.fake import Reply, ScriptedTransport, Seen, answer_for, envelope
from trader.llm.roles import role_specs
from trader.llm.schemas import PremarketBrief, SymbolAssessment, VetBatch
from trader.memory.journal import Journal
from trader.strategy.orb_sip import save_signals

ARMED = et_datetime(DAY, time(9, 35, 3))
BRIEF_DEADLINE = et_datetime(DAY, time(9, 28))


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return et_datetime(DAY, time(hour, minute, second))


def long(symbol: str, rank: int = 1, *, armed: datetime = ARMED) -> Signal:
    return signal(symbol, rank).model_copy(update={"armed_at": armed})


AAA, BBB, CCC = long("AAA", 1), long("BBB", 2), long("CCC", 3)
SHORT = signal("SSS", 4, direction="shadow_short")


class Desk:
    """A journal, a SimClock, the runner on a scripted transport and Book B's store.

    The scripted model answers each candidate with ``verdicts[symbol]`` (neutral by default)
    and the brief with ``day_mode``; ``leave_out`` drops candidates from its answers, and
    ``script`` replaces the whole reply. ``hang`` makes an attempt wait until it is cancelled,
    as a crash would leave it."""

    def __init__(self, root: Path, journal: Journal, *, revet: bool = False) -> None:
        self.root, self.journal = root, journal
        self.clock = SimClock(BRIEF_AT)
        self.revet = revet
        self.last_entry: datetime | None = None
        self.verdicts: dict[str, str] = {}
        self.day_mode = "normal"
        self.leave_out: set[str] = set()
        self.script: Callable[[Seen], Reply] | None = None
        self.hang = False
        self.started = asyncio.Event()
        self.transport = ScriptedTransport(self._reply, sleep=self._sleep)
        self.calls = LlmCalls(
            journal=journal,
            clock=self.clock,
            transport=self.transport,
            command=("claude",),
            roles=role_specs(SETTINGS),
            env={},
            prompts_dir=REPO / "prompts",
            sandbox_dir=root / "sandbox",
            artifacts_dir=root / "llm",
            budget=Budget(journal, max_calls_per_day=12, max_concurrent=1),
        )
        self.store = self.new_store()

    def new_store(self, *, day: date = DAY) -> VerdictStore:
        """A store on the same journal, as a restart builds it."""
        return VerdictStore(self.journal, self.clock, day=day, max_wait_s=60,
                            revet_premarket=self.revet, degrade_after=3,
                            last_entry=self.last_entry)  # fmt: skip

    async def _sleep(self, seconds: float) -> None:
        if self.hang:
            self.started.set()
            await asyncio.Event().wait()  # until cancelled
        await self.clock.advance(timedelta(seconds=seconds))

    def _reply(self, seen: Seen) -> Reply:
        if self.script is not None:
            return self.script(seen)
        out = answer_for(seen.pack())
        out["assessments"] = [
            {**a, "verdict": self.verdicts.get(a["symbol"], "neutral")}
            for a in out["assessments"]
            if a["symbol"] not in self.leave_out
        ]
        if "day_mode" in out:
            out["day_mode"] = self.day_mode
        return Reply(envelope=envelope(out), delay_s=2.0)

    async def at(self, when: datetime) -> None:
        await self.clock.advance(when - self.clock.now())

    def brief_pack(self, *symbols: str) -> EncodedPack:
        candidates = [candidate(s) for s in symbols]
        return self.calls.encode(
            premarket_pack(
                as_of=self.clock.now(),
                common=common(),
                candidates=candidates,
                evidence={},
                limits=PREMARKET,
            )
        )

    async def brief(self, *symbols: str) -> LLMResult[Any]:
        pack = self.brief_pack(*symbols)
        result = await self.calls.run(
            "premarket", pack, deadline=BRIEF_DEADLINE, persist=self.store.persist_brief
        )
        await self.store.brief_done(result)
        return result

    def vet_pack(self, targets: Sequence[Signal]) -> EncodedPack:
        picks = [VetCandidate(s, f"{s.symbol} Corp", "NYSE", Evidence(filings=())) for s in targets]
        return self.calls.encode(
            vet_pack(as_of=self.clock.now(), common=common(), candidates=picks, limits=VET)
        )

    async def vet(self, *signals: Signal) -> LLMResult[Any]:
        """The 09:35:05 vet as P4.8 will run it: its targets expected, then the call."""
        targets = self.store.vet_targets(signals)
        job = self.store.expect_vet(targets, at=self.clock.now())
        return await self.run_vet(job, targets)

    async def run_vet(
        self, job: VetJob, targets: Sequence[Signal], *, sent: Sequence[Signal] | None = None
    ) -> LLMResult[Any]:
        pack = self.vet_pack(targets if sent is None else sent)
        self.store.vet_started(job, at=self.clock.now())
        result = await self.calls.run(
            "vet", pack, deadline=self.clock.now() + timedelta(seconds=90),
            persist=self.store.persist_vet,
        )  # fmt: skip
        await self.store.vet_done(job, result, sent=pack.kept)
        return result

    async def failed_attempts(self, n: int) -> None:
        """``n`` failed trading attempts in the journal, as calls' first tries leave them."""
        stamp = fmt_iso(self.clock.now())

        def write(conn: Any) -> None:
            for i in range(n):
                conn.execute(
                    "INSERT INTO llm_calls (id, date, role, purpose, logical_id, attempt, final, "
                    "ok, failure, started_at, ended_at) VALUES (?, ?, 'vet', 'trading', ?, 1, 0, "
                    "0, 'schema_invalid', ?, ?)",
                    (f"x-{i}-1", DAY.isoformat(), f"x-{i}", stamp, stamp),
                )

        await self.journal.write(write)

    async def rows(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.journal.fetch_all(sql, params)]


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    await save_signals(opened, [AAA, BBB, CCC, SHORT])
    yield opened
    await opened.aclose()


@pytest.fixture
def desk(tmp_path: Path, journal: Journal) -> Desk:
    return Desk(tmp_path, journal)


def bad_answer(seen: Seen) -> Reply:
    return Reply(envelope=envelope({"pack_id": seen.pack()["pack_id"]}))  # no members


def crash(seen: Seen) -> Reply:
    return Reply(stdout=b"Error: boom", exit_code=1)


def snapshot(store: VerdictStore, *signals: Signal) -> list[Any]:
    return [store.day_mode(), store.failures, *(store.standing(s) for s in signals)]


async def abandon(desk: Desk, run: Callable[[], Any]) -> None:
    """Start ``run`` and cancel it inside its attempt, as a crash or a shutdown would."""
    desk.hang = True
    task = asyncio.ensure_future(run())
    await asyncio.wait_for(desk.started.wait(), 5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    desk.hang = False
    desk.started.clear()


# ---------------------------------------------------------------- the brief


async def test_a_good_brief_is_journaled_before_memory_takes_it(desk: Desk) -> None:
    desk.verdicts, desk.day_mode = {"AAA": "veto", "BBB": "allow"}, "reduced"
    pack = desk.brief_pack("AAA", "BBB")
    result = await desk.calls.run(
        "premarket", pack, deadline=BRIEF_DEADLINE, persist=desk.store.persist_brief
    )
    call = result.call_id
    assert (result.ok, call) == (True, "20261103-premarket-01-1")
    (mode,) = await desk.rows("SELECT * FROM day_modes")
    assert {k: mode[k] for k in ("date", "book", "mode", "source", "multiplier", "llm_call_id",
                                 "eval_epoch")} == {"date": "2026-11-03", "book": "B",
                                 "mode": "reduced", "source": "premarket", "multiplier": "0.5",
                                 "llm_call_id": call, "eval_epoch": 0}  # fmt: skip
    rows = await desk.rows("SELECT symbol, source, verdict, signal_id, llm_call_id, "
                           "risk_flags_json, eval_epoch FROM verdicts ORDER BY id")  # fmt: skip
    assert rows == [
        {"symbol": "AAA", "source": "premarket", "verdict": "veto", "signal_id": None,
         "llm_call_id": call, "risk_flags_json": "[]", "eval_epoch": 0},
        {"symbol": "BBB", "source": "premarket", "verdict": "allow", "signal_id": None,
         "llm_call_id": call, "risk_flags_json": "[]", "eval_epoch": 0},
    ]  # fmt: skip
    # the journal first: memory hasn't moved yet
    assert (desk.store.day_mode(), desk.store.version) == (DEFAULT_MODE, 0)
    assert desk.store.standing(AAA).kind == "unavailable"
    await desk.store.brief_done(result)
    assert desk.store.day_mode() == DayModeState("reduced", "premarket", Decimal("0.5"), call)
    assert desk.store.standing(AAA).verdict == StoredVerdict("AAA", "premarket", "veto", 0.5, call)
    assert desk.store.version == 1


async def test_a_failed_brief_puts_the_default_day_mode_on_record(desk: Desk) -> None:
    desk.script = bad_answer
    result = await desk.brief("AAA")
    assert (result.ok, result.failure, result.call_id) == (
        False, "schema_invalid", "20261103-premarket-01-2",
    )  # fmt: skip
    (mode,) = await desk.rows("SELECT mode, source, multiplier, reason, llm_call_id FROM day_modes")
    assert mode == {"mode": "normal", "source": "default", "multiplier": "1",
                    "reason": "the brief failed: schema_invalid",
                    "llm_call_id": "20261103-premarket-01-2"}  # fmt: skip
    assert desk.store.day_mode() == DayModeState(
        "normal", "default", Decimal(1), "20261103-premarket-01-2"
    )
    assert desk.store.failures == 2  # the call and its repair
    assert await desk.rows("SELECT * FROM verdicts") == []


async def test_no_brief_at_all_is_the_default_day_mode(desk: Desk) -> None:
    await desk.store.brief_missed()
    (mode,) = await desk.rows("SELECT mode, source, reason, llm_call_id FROM day_modes")
    assert mode == {"mode": "normal", "source": "default", "reason": "no brief ran",
                    "llm_call_id": None}  # fmt: skip
    assert (desk.store.day_mode(), desk.store.version) == (DEFAULT_MODE, 1)


async def test_the_default_never_replaces_a_brief(desk: Desk) -> None:
    desk.day_mode = "no_trade"
    result = await desk.brief("AAA")
    await desk.store.brief_missed()
    (mode,) = await desk.rows("SELECT mode, source FROM day_modes")
    assert mode == {"mode": "no_trade", "source": "premarket"}
    assert desk.store.day_mode() == DayModeState("no_trade", "premarket", Decimal(0),
                                                 result.call_id)  # fmt: skip


async def test_an_answer_reaches_memory_before_the_count_is_read(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk.day_mode = "no_trade"
    pack = desk.brief_pack("AAA")
    result = await desk.calls.run(
        "premarket", pack, deadline=BRIEF_DEADLINE, persist=desk.store.persist_brief
    )

    async def unreadable(journal: Journal, day: date) -> int:
        raise OSError("the reader is gone")

    monkeypatch.setattr("trader.books.verdicts.failed_attempts", unreadable)
    with pytest.raises(OSError, match="the reader is gone"):
        await desk.store.brief_done(result)
    assert desk.store.day_mode().mode == "no_trade"  # as the journal says


async def test_persist_and_done_refuse_another_roles_answer(desk: Desk) -> None:
    vet = VetBatch(pack_id="x", assessments=[])
    brief = PremarketBrief(pack_id="x", day_mode="normal", day_mode_reason="",
                           market_notes="", assessments=[])  # fmt: skip
    job = desk.store.expect_vet([], at=desk.clock.now())
    with pytest.raises(TypeError, match="brief answered with a VetBatch"):
        desk.store.persist_brief(None, vet, "id")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="vet answered with a PremarketBrief"):
        desk.store.persist_vet(None, brief, "id")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="good brief"):
        await desk.store.brief_done(LLMResult(True, vet, "id", 1, None))
    with pytest.raises(TypeError, match="good vet"):
        await desk.store.vet_done(job, LLMResult(True, brief, "id", 1, None), sent=())
    with pytest.raises(TypeError, match="good vet"):
        await desk.store.vet_done(job, LLMResult(True, vet, None, 1, None), sent=())


# ---------------------------------------------------------------- the vet


async def test_the_vet_judges_the_armed_longs_without_a_premarket_verdict(desk: Desk) -> None:
    await desk.brief("AAA")
    expired = long("DDD", 5).model_copy(update={"status": "expired"})
    assert desk.store.vet_targets([AAA, BBB, SHORT, expired]) == [BBB]
    mislabeled = SHORT.model_copy(update={"status": "armed"})
    assert desk.store.vet_targets([mislabeled]) == []  # longs only, whatever the status says
    triggered = BBB.model_copy(update={"status": "triggered"})
    assert desk.store.vet_targets([triggered]) == [triggered]  # it waits for a verdict too
    desk.revet = True
    revet = desk.new_store()
    await revet.load()
    assert revet.vet_targets([AAA, BBB]) == [AAA, BBB]  # every armed long is judged again


async def test_a_vet_verdict_names_its_signal_and_stands(desk: Desk) -> None:
    await desk.at(VET_AT)
    desk.verdicts = {"BBB": "allow"}
    job = desk.store.expect_vet([BBB], at=desk.clock.now())
    assert job.symbols == ("BBB",)
    assert desk.store.standing(BBB).kind == "pending"
    version = desk.store.version
    result = await desk.run_vet(job, [BBB])
    call = result.call_id
    assert await desk.rows("SELECT symbol, source, verdict, signal_id, llm_call_id, eval_epoch "
                           "FROM verdicts") == [
        {"symbol": "BBB", "source": "vet", "verdict": "allow", "signal_id": BBB.id,
         "llm_call_id": call, "eval_epoch": 0},
    ]  # fmt: skip
    assert desk.store.standing(BBB).verdict == StoredVerdict("BBB", "vet", "allow", 0.5, call)
    assert desk.store.version == version + 2  # started, then done
    assert desk.store.vet_targets([BBB]) == []  # judged: never sent again


async def test_pending_counts_max_wait_from_the_later_of_arming_and_the_calls_start(
    desk: Desk,
) -> None:
    await desk.at(ARMED)
    late = long("DDD", 4, armed=at(9, 36))
    job = desk.store.expect_vet([BBB, late], at=desk.clock.now())
    standing = desk.store.standing(BBB)
    assert (standing.kind, standing.deadline) == ("pending", at(9, 36, 3))
    assert standing.detail == "the vet is expected since 09:35:03"
    await desk.at(at(9, 35, 9))
    desk.store.vet_started(job, at=desk.clock.now())
    standing = desk.store.standing(BBB)
    assert standing.deadline == at(9, 36, 9)  # from the call's start
    assert standing.detail == "the vet is under way since 09:35:09"
    desk.store.vet_started(job, at=at(9, 35, 30))  # a second start moves nothing
    assert desk.store.standing(BBB).deadline == at(9, 36, 9)
    assert desk.store.standing(late).deadline == at(9, 37)  # from arming
    unarmed = BBB.model_copy(update={"armed_at": None})
    assert desk.store.standing(unarmed).deadline == at(9, 36, 9)


async def test_the_deadline_never_passes_the_last_entry(desk: Desk) -> None:
    desk.last_entry = at(9, 35, 30)
    desk.store = desk.new_store()
    await desk.at(VET_AT)
    desk.store.expect_vet([BBB], at=desk.clock.now())
    assert desk.store.standing(BBB).deadline == at(9, 35, 30)


async def test_a_job_ends_only_its_own_symbols(desk: Desk) -> None:
    await desk.at(VET_AT)
    first = desk.store.expect_vet([AAA, CCC], at=desk.clock.now())
    desk.store.vet_started(first, at=desk.clock.now())
    await desk.at(at(9, 35, 30))
    second = desk.store.expect_vet([BBB, CCC], at=desk.clock.now())  # CCC is the second's now
    desk.store.vet_started(second, at=desk.clock.now())
    assert desk.store.standing(AAA).deadline == at(9, 36, 5)  # the second's start moved nothing
    desk.verdicts = {"AAA": "allow"}
    await desk.run_vet(first, [AAA])
    assert desk.store.standing(AAA).kind == "verdict"
    for sig in (BBB, CCC):
        still = desk.store.standing(sig)
        assert (still.kind, still.deadline) == ("pending", at(9, 36, 30))


async def test_a_cancelled_job_is_vetted_again(desk: Desk) -> None:
    await desk.at(VET_AT)
    job = desk.store.expect_vet([BBB], at=desk.clock.now())
    desk.store.vet_started(job, at=desk.clock.now())
    assert desk.store.vet_targets([BBB]) == []
    await desk.at(at(9, 35, 20))
    desk.store.vet_abandoned(job, at=desk.clock.now())
    standing = desk.store.standing(BBB)
    assert (standing.kind, standing.deadline, standing.detail) == (
        "pending", at(9, 36, 20), "the vet is expected since 09:35:20",
    )  # fmt: skip
    assert desk.store.vet_targets([BBB]) == [BBB]


async def test_a_failed_vet_leaves_its_symbols_unavailable(desk: Desk) -> None:
    await desk.at(VET_AT)
    desk.script = crash
    result = await desk.vet(BBB, CCC)
    assert (result.failure, result.call_id) == ("nonzero_exit", "20261103-vet-01-1")
    standing = desk.store.standing(BBB)
    assert (standing.kind, standing.llm_call_id) == ("unavailable", "20261103-vet-01-1")
    assert standing.detail == "the vet failed: nonzero_exit (20261103-vet-01-1)"
    assert desk.store.failures == 1  # a process failure gets no repair
    assert desk.store.vet_targets([BBB, CCC]) == []  # no second chance


async def test_a_vet_refused_before_any_attempt_names_no_attempt(desk: Desk) -> None:
    await desk.at(VET_AT)
    job = desk.store.expect_vet([BBB], at=desk.clock.now())
    refused: LLMResult[Any] = LLMResult(False, None, None, 0, "refused: no_time",
                                        failure="no_time")  # fmt: skip
    await desk.store.vet_done(job, refused, sent=["BBB"])
    standing = desk.store.standing(BBB)
    assert (standing.kind, standing.llm_call_id, standing.detail) == (
        "unavailable", None, "the vet failed: no_time",
    )  # fmt: skip
    await desk.store.vet_done(job, LLMResult(False, None, None, 0, None), sent=["BBB"])
    assert desk.store.standing(BBB).detail == "the vet failed: no_time"  # already done


async def test_a_job_whose_symbol_moved_on_leaves_it_alone(desk: Desk) -> None:
    await desk.at(VET_AT)
    old = desk.store.expect_vet([BBB], at=desk.clock.now())
    desk.store.expect_vet([BBB], at=desk.clock.now())  # expected again, by a newer job
    await desk.store.vet_done(old, LLMResult(False, None, None, 0, None, failure="no_time"),
                              sent=["BBB"])  # fmt: skip
    assert desk.store.standing(BBB).kind == "pending"


async def test_a_symbol_left_out_or_never_sent_is_unavailable(desk: Desk) -> None:
    await desk.at(VET_AT)
    desk.leave_out = {"BBB"}
    job = desk.store.expect_vet([AAA, BBB, CCC], at=desk.clock.now())
    result = await desk.run_vet(job, [AAA, BBB, CCC], sent=[AAA, BBB])  # no room for CCC
    assert result.ok
    left = desk.store.standing(BBB)
    assert (left.kind, left.llm_call_id) == ("unavailable", result.call_id)
    assert left.detail == f"the vet's answer left it out ({result.call_id})"
    unsent = desk.store.standing(CCC)
    assert (unsent.kind, unsent.llm_call_id) == ("unavailable", None)
    assert unsent.detail == "the vet's pack had no room for it"
    assert desk.store.standing(AAA).kind == "verdict"


async def test_an_unknown_signal_costs_only_its_own_id(desk: Desk) -> None:
    await desk.at(VET_AT)
    stray = long("DDD", 4)  # never saved to the journal
    desk.verdicts = {"BBB": "veto", "DDD": "allow"}
    result = await desk.vet(BBB, stray)
    assert result.ok
    assert await desk.rows("SELECT symbol, signal_id FROM verdicts ORDER BY id") == [
        {"symbol": "BBB", "signal_id": BBB.id}, {"symbol": "DDD", "signal_id": None},
    ]  # fmt: skip
    verdict = desk.store.standing(stray).verdict
    assert verdict is not None
    assert verdict.verdict == "allow"


async def test_a_signal_no_call_covers_is_unavailable(desk: Desk) -> None:
    standing = desk.store.standing(BBB)
    assert (standing.kind, standing.detail) == ("unavailable", "no LLM call covers it")


async def test_a_premarket_verdict_stands_over_the_vets_unless_revet(desk: Desk) -> None:
    desk.verdicts = {"AAA": "veto"}
    brief = await desk.brief("AAA")
    await desk.at(VET_AT)
    desk.verdicts = {"AAA": "allow"}
    job = desk.store.expect_vet([AAA], at=desk.clock.now())  # as if vetted anyway
    await desk.run_vet(job, [AAA])
    verdict = desk.store.standing(AAA).verdict
    assert verdict is not None
    assert (verdict.source, verdict.verdict, verdict.llm_call_id) == (
        "premarket", "veto", brief.call_id,
    )  # fmt: skip


async def test_revet_waits_for_the_vet_and_falls_back_on_the_brief(
    tmp_path: Path, journal: Journal
) -> None:
    desk = Desk(tmp_path, journal, revet=True)
    desk.verdicts = {"AAA": "veto", "BBB": "veto"}
    brief = await desk.brief("AAA", "BBB")
    brief_aaa = StoredVerdict("AAA", "premarket", "veto", 0.5, brief.call_id or "")
    await desk.at(VET_AT)
    targets = desk.store.vet_targets([AAA, BBB])
    job = desk.store.expect_vet(targets, at=desk.clock.now())
    pending = desk.store.standing(AAA)  # the brief's veto waits for the vet
    assert (pending.kind, pending.fallback) == ("pending", brief_aaa)
    desk.verdicts = {"AAA": "allow"}
    desk.leave_out = {"BBB"}
    vet = await desk.run_vet(job, targets)
    assert desk.store.standing(AAA).verdict == StoredVerdict(
        "AAA", "vet", "allow", 0.5, vet.call_id or ""
    )
    fallback = desk.store.standing(BBB).verdict  # the re-vet left it out: the brief's stands
    assert fallback == StoredVerdict("BBB", "premarket", "veto", 0.5, brief.call_id or "")


async def test_degraded_at_the_third_failed_attempt(desk: Desk) -> None:
    desk.script = bad_answer
    await desk.brief("AAA")  # two failed attempts
    assert (desk.store.failures, desk.store.degraded) == (2, False)
    await desk.at(VET_AT)
    desk.script = crash
    await desk.vet(BBB)
    assert (desk.store.failures, desk.store.degraded) == (3, True)


async def test_failures_are_read_attempt_by_attempt(desk: Desk) -> None:
    assert await desk.store.refresh_failures() == 0
    version = desk.store.version
    await desk.failed_attempts(3)  # say, first tries whose repairs still run
    assert await desk.store.refresh_failures() == 3
    assert (desk.store.degraded, desk.store.version) == (True, version + 1)
    assert await desk.store.refresh_failures() == 3
    assert desk.store.version == version + 1  # no change, no bump


async def test_the_version_wakes_whoever_waits_for_a_change(desk: Desk) -> None:
    version = desk.store.version
    waiter = asyncio.create_task(desk.store.wait_for_change(version))
    await asyncio.sleep(0)
    assert not waiter.done()
    desk.store.expect_vet([BBB], at=desk.clock.now())
    assert await asyncio.wait_for(waiter, 1) == version + 1
    assert await desk.store.wait_for_change(version) == version + 1  # already out of date


async def test_vet_targets_keep_a_vet_that_is_only_expected(desk: Desk) -> None:
    await desk.at(VET_AT)
    job = desk.store.expect_vet([BBB], at=desk.clock.now())
    assert desk.store.vet_targets([BBB]) == [BBB]  # expected, not yet under way
    desk.store.vet_started(job, at=desk.clock.now())
    assert desk.store.vet_targets([BBB]) == []


# ---------------------------------------------------------------- after a restart


async def test_load_rebuilds_the_day_from_the_journal(desk: Desk) -> None:
    desk.verdicts, desk.day_mode = {"AAA": "veto"}, "reduced"
    await desk.brief("AAA")
    await desk.at(VET_AT)
    desk.verdicts, desk.leave_out = {"BBB": "allow"}, {"CCC"}
    await desk.vet(AAA, BBB, CCC)
    restarted = desk.new_store()
    assert await restarted.load() == Rebuilt(brief="ok", revet=())
    assert snapshot(restarted, AAA, BBB, CCC) == snapshot(desk.store, AAA, BBB, CCC)
    assert restarted.vet_targets([AAA, BBB, CCC]) == []


async def test_load_after_a_failed_brief_and_a_failed_vet(desk: Desk) -> None:
    desk.script = bad_answer
    await desk.brief("AAA")
    await desk.at(VET_AT)
    desk.script = crash
    await desk.vet(AAA, BBB)
    restarted = desk.new_store()
    # CCC was armed but never vetted: after a restart it is pending, as if its vet had crashed
    assert await restarted.load() == Rebuilt(brief="failed", revet=("CCC",))
    assert snapshot(restarted, AAA, BBB) == snapshot(desk.store, AAA, BBB)
    assert restarted.degraded


async def test_an_empty_day_loads_as_nothing_yet(desk: Desk) -> None:
    store = desk.new_store(day=DAY + timedelta(days=1))  # no signals, no calls
    assert await store.load() == Rebuilt(brief="none", revet=())
    assert (store.day_mode(), store.failures) == (DEFAULT_MODE, 0)


async def test_an_abandoned_vet_is_pending_again_from_the_restart(desk: Desk) -> None:
    await desk.at(VET_AT)
    await abandon(desk, lambda: desk.vet(AAA, BBB, CCC))
    (row,) = await desk.rows("SELECT failure, final FROM llm_calls")
    assert row == {"failure": "abandoned", "final": 1}
    await desk.at(at(9, 40))
    restarted = desk.new_store()
    assert await restarted.load() == Rebuilt(brief="none", revet=("AAA", "BBB", "CCC"))
    assert restarted.failures == 0  # an abandoned attempt never counts
    standing = restarted.standing(BBB)
    assert (standing.kind, standing.deadline) == ("pending", at(9, 41))
    targets = restarted.vet_targets([AAA, BBB, CCC])
    assert targets == [AAA, BBB, CCC]
    # The re-vet starts later still: its start sets the deadline (plan decision 8).
    await desk.at(at(9, 40, 7))
    desk.store = restarted
    job = restarted.expect_vet(targets, at=desk.clock.now())
    restarted.vet_started(job, at=desk.clock.now())
    assert restarted.standing(BBB).deadline == at(9, 41, 7)
    desk.verdicts = {"BBB": "allow"}
    vet = await desk.run_vet(job, targets)
    assert restarted.standing(BBB).verdict == StoredVerdict("BBB", "vet", "allow", 0.5,
                                                           vet.call_id or "")  # fmt: skip
    assert vet.call_id == "20261103-vet-02-1"


async def test_a_vet_cut_short_before_its_first_attempt_is_vetted_again(desk: Desk) -> None:
    await desk.at(VET_AT)
    desk.store.expect_vet([AAA, BBB, CCC], at=desk.clock.now())  # then a crash, before any row
    await desk.at(at(9, 36))
    restarted = desk.new_store()
    assert await restarted.load() == Rebuilt(brief="none", revet=("AAA", "BBB", "CCC"))
    standing = restarted.standing(AAA)
    assert (standing.kind, standing.deadline) == ("pending", at(9, 37))


async def test_a_signal_book_b_decided_is_not_vetted_again(desk: Desk) -> None:
    def decide(book: Any, signal_id: str, reason: str) -> Callable[[Any], bool]:
        return lambda conn: insert_decision(conn, book=book, signal_id=signal_id,
                                            take=reason == "taken", reason=reason, detail="",
                                            at=VET_AT)  # fmt: skip

    await desk.journal.write(decide("B", AAA.id, "llm_unavailable"))
    await desk.journal.write(decide("A", BBB.id, "taken"))  # Book A's say nothing of B
    restarted = desk.new_store()
    assert (await restarted.load()).revet == ("BBB", "CCC")


async def test_a_call_whose_repair_never_ran_is_abandoned_and_its_try_counts(
    desk: Desk,
) -> None:
    await desk.at(VET_AT)
    desk.script = bad_answer
    await desk.vet(AAA, BBB, CCC)
    # A crash before the repair's insert: only the first, repairable, attempt is left.
    await desk.journal.execute("DELETE FROM llm_calls WHERE attempt = 2")
    restarted = desk.new_store()
    assert await restarted.load() == Rebuilt(brief="none", revet=("AAA", "BBB", "CCC"))
    assert restarted.failures == 1  # its failure still counts (D-064)


async def test_an_attempt_a_crash_left_open_is_abandoned(desk: Desk) -> None:
    await desk.at(VET_AT)
    await desk.vet(AAA, BBB, CCC)
    await desk.journal.execute("UPDATE llm_calls SET ended_at = NULL, ok = NULL, final = 0")
    await desk.journal.execute("DELETE FROM verdicts")
    restarted = desk.new_store()
    assert (await restarted.load()).revet == ("AAA", "BBB", "CCC")


async def test_an_abandoned_vet_leaves_a_standing_verdict_alone(desk: Desk) -> None:
    desk.verdicts = {"AAA": "veto"}
    await desk.brief("AAA")
    await desk.at(VET_AT)
    job = desk.store.expect_vet([AAA, BBB, CCC], at=desk.clock.now())  # AAA too, anyway
    await abandon(desk, lambda: desk.run_vet(job, [AAA, BBB, CCC]))
    restarted = desk.new_store()
    assert (await restarted.load()).revet == ("BBB", "CCC")
    assert restarted.standing(AAA).kind == "verdict"


async def test_load_follows_the_calls_numbers_not_the_clock(desk: Desk) -> None:
    await abandon(desk, lambda: desk.brief("AAA"))  # premarket-01
    desk.day_mode = "reduced"
    ok = await desk.brief("AAA")
    assert ok.call_id == "20261103-premarket-02-1"
    # The clock stepped back between them, so the abandoned call reads as the later one.
    await desk.journal.execute(
        "UPDATE llm_calls SET started_at = ? WHERE logical_id = '20261103-premarket-01'",
        (fmt_iso(at(9, 30)),),
    )
    restarted = desk.new_store()
    assert (await restarted.load()).brief == "ok"
    assert restarted.day_mode().mode == "reduced"


async def test_load_records_the_default_a_crash_kept_off_the_record(desk: Desk) -> None:
    desk.script = bad_answer
    pack = desk.brief_pack("AAA")
    failed = await desk.calls.run(
        "premarket", pack, deadline=BRIEF_DEADLINE, persist=desk.store.persist_brief
    )  # then a crash, before brief_done
    assert await desk.rows("SELECT * FROM day_modes") == []
    restarted = desk.new_store()
    assert (await restarted.load()).brief == "failed"
    (mode,) = await desk.rows("SELECT mode, source, reason, llm_call_id FROM day_modes")
    assert mode == {"mode": "normal", "source": "default",
                    "reason": "the brief failed: schema_invalid",
                    "llm_call_id": failed.call_id}  # fmt: skip
    assert restarted.day_mode() == DayModeState("normal", "default", Decimal(1), failed.call_id)


async def test_load_reads_the_trading_calls_and_their_verdicts_only(desk: Desk) -> None:
    await desk.at(VET_AT)
    pack = desk.vet_pack([BBB])
    smoke = await desk.calls.run("vet", pack, deadline=desk.clock.now() + timedelta(seconds=90),
                                 purpose="smoke")  # fmt: skip
    await desk.journal.execute(
        "INSERT INTO verdicts (date, symbol, source, verdict, confidence, llm_call_id) "
        "VALUES ('2026-11-03', 'BBB', 'vet', 'veto', 0.9, ?)",
        (smoke.call_id,),
    )
    restarted = desk.new_store()
    await restarted.load()
    assert restarted.standing(BBB).kind == "pending"  # still to be vetted, for trading


async def test_load_restores_the_signal_ids_for_the_vets_rows(desk: Desk) -> None:
    await desk.at(VET_AT)
    vet = await desk.vet(AAA)  # an attempt the verdict's foreign key can name
    restarted = desk.new_store()
    await restarted.load()
    batch = VetBatch(pack_id="x", assessments=[SymbolAssessment(
        symbol="BBB", catalyst_type="other", catalyst_fresh=False, catalyst_confirmed=False,
        risk_flags=[], verdict="allow", confidence=0.5, reason="")])  # fmt: skip
    await desk.journal.write(lambda conn: restarted.persist_vet(conn, batch, vet.call_id or ""))
    assert await desk.rows("SELECT signal_id FROM verdicts WHERE symbol = 'BBB'") == [
        {"signal_id": BBB.id}
    ]


async def test_load_rebuilds_a_fresh_store_only(desk: Desk) -> None:
    store = desk.new_store()
    store.expect_vet([BBB], at=desk.clock.now())
    with pytest.raises(RuntimeError, match="fresh store"):
        await store.load()


async def test_a_day_mode_row_without_its_columns_still_loads(desk: Desk) -> None:
    await desk.journal.execute(
        "INSERT INTO day_modes (date, book, mode, reason, llm_call_id) "
        "VALUES ('2026-11-03', 'B', 'reduced', 'x', NULL)"
    )
    store = desk.new_store()
    await store.load()
    assert store.day_mode() == DayModeState("reduced", "default", Decimal("0.5"), None)
