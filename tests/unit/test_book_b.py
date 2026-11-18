"""Book B (books/llm_filtered.py; S§5; the Phase 4 plan's decision 8; DECISIONS D-068).

The verdicts come from the real runner on a scripted transport (``Desk``, from
test_verdicts.py); the execution side is test_books' scripted service.

**The numbers.** The signals' prices are trigger 14.81, stop 14.71 and chase cap 14.85. At the
last price 14.82, the limit is min(max(ceil(14.82 x 1.001), 14.81 + 0.02), 14.85) = 14.84, and a
$1,000 book buys floor(500 / 14.84) = 33 shares, the notional cap binding. A reduced day halves
the cap: floor(250 / 14.84) = 16. At 14.83 the limit is 14.85.

The triggers keep their own price, 14.82, throughout. The feed's (``Inputs.last``) is what the
book must use, and it is set apart from the trigger's where that matters.
"""

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.exec_harness import RISK, SETTINGS, TIMES
from tests.unit.test_books import Service
from tests.unit.test_llm_context import VET_AT
from tests.unit.test_verdicts import (
    AAA,
    BBB,
    CCC,
    SHORT,
    Desk,
    abandon,
    at,
    bad_answer,
    crash,
    long,
)
from trader.books.base import BookDecision, Defer, PreparedInputs
from trader.books.llm_filtered import LlmFilteredBook
from trader.core.models import Asset, Signal, TriggerEvent
from trader.execution.service import Exposure, GateStateBuilder
from trader.memory.journal import Journal
from trader.risk.gate import GateState
from trader.strategy.orb_sip import save_signals

D = Decimal
DEADLINE = at(9, 36, 5)  # the vet starts 09:35:05, after arming: + 60 s


def asset(symbol: str) -> Asset:
    return Asset(symbol=symbol, name=symbol, exchange="NASDAQ", asset_class="us_equity",
                 status="active", tradable=True)  # fmt: skip


class Inputs:
    """The gate's inputs: the feed's price and the halts in force. ``waited`` records how the
    book asked."""

    def __init__(self, last: str | None = "14.82", halts: tuple[str, ...] = ()) -> None:
        self.last = D(last) if last else None
        self.halts = halts
        self.waited: list[bool] = []

    async def prepare(
        self, book: str, sig: Signal, ev: TriggerEvent, *, waited: bool = False
    ) -> PreparedInputs:
        self.waited.append(waited)
        return PreparedInputs(last_price=self.last, quote_age_s=0.4, broker_equity=D(1000),
                              asset=asset(sig.symbol), halts=self.halts)  # fmt: skip

    def builder(self, book: str, sig: Signal, prepared: PreparedInputs) -> GateStateBuilder:
        async def build(exposure: Exposure) -> GateState:
            return GateState(mode="paper", now=VET_AT, day=TIMES,
                             available_cash=exposure.available_cash,
                             equity_sod=exposure.equity_sod, ledger_equity=exposure.ledger_equity,
                             broker_equity=prepared.broker_equity, asset=prepared.asset,
                             quote_age_s=prepared.quote_age_s)  # fmt: skip

        return build


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    await save_signals(opened, [AAA, BBB, CCC, SHORT])
    yield opened
    await opened.aclose()


@pytest.fixture
def desk(tmp_path: Path, journal: Journal) -> Desk:
    return Desk(tmp_path, journal)


def book_b(
    desk: Desk, service: Service | None = None, inputs: Inputs | None = None
) -> tuple[LlmFilteredBook, Service, Inputs]:
    service, inputs = service or Service(), inputs or Inputs()
    book = LlmFilteredBook(service=service, inputs=inputs, journal=desk.journal,  # type: ignore[arg-type]
                           clock=desk.clock, strategy=SETTINGS.strategy, risk=RISK,
                           store=desk.store)  # fmt: skip
    return book, service, inputs


def trigger(sig: Signal, *, source: str = "stream_trade") -> TriggerEvent:
    return TriggerEvent(signal_id=sig.id, symbol=sig.symbol, rank=sig.rel_vol_rank, ts=VET_AT,
                        received_at=VET_AT, price=D("14.82"), size=100, conditions=("@",),
                        source=source)  # type: ignore[arg-type]  # fmt: skip


async def decide(book: LlmFilteredBook, sig: Signal, *, waited: bool = False) -> BookDecision:
    decision = await book.on_trigger(trigger(sig), sig, waited=waited)
    assert isinstance(decision, BookDecision), decision
    return decision


async def park(book: LlmFilteredBook, sig: Signal, *, waited: bool = False) -> Defer:
    deferred = await book.on_trigger(trigger(sig), sig, waited=waited)
    assert isinstance(deferred, Defer), deferred
    return deferred


async def rows(journal: Journal) -> list[tuple[Any, ...]]:
    return [
        (r["signal_id"], r["take"], r["reason_code"], r["llm_call_id"])
        for r in await journal.fetch_all(
            "SELECT signal_id, take, reason_code, llm_call_id FROM decisions ORDER BY id"
        )
    ]


def outcome(d: BookDecision) -> tuple[bool, str, str | None]:
    return d.take, d.reason, d.llm_call_id


# ---------------------------------------------------------------- verdicts in hand


async def test_allow_and_neutral_are_taken_and_carry_their_call(
    desk: Desk, journal: Journal
) -> None:
    await desk.at(VET_AT)
    desk.verdicts = {"AAA": "allow"}  # BBB: neutral, treated as allow (S§5.1)
    vet = await desk.vet(AAA, BBB)
    book, service, _ = book_b(desk)
    first = await decide(book, AAA)
    assert outcome(first) == (True, "taken", vet.call_id)
    assert outcome(await decide(book, BBB)) == (True, "taken", vet.call_id)
    assert [(i.qty, i.entry_limit, i.day_size_multiplier) for i in service.intents] == [
        (33, D("14.84"), D(1)), (33, D("14.84"), D(1)),
    ]  # fmt: skip
    assert first.detail.endswith("; qty 33; vet allow 0.50 (20261103-vet-01-1)")
    assert await rows(journal) == [
        (AAA.id, 1, "taken", vet.call_id), (BBB.id, 1, "taken", vet.call_id),
    ]  # fmt: skip


async def test_a_veto_beats_the_chase_guard(desk: Desk) -> None:
    await desk.at(VET_AT)
    desk.verdicts = {"AAA": "veto"}
    vet = await desk.vet(AAA)
    book, service, _ = book_b(desk, inputs=Inputs("14.90"))  # past the chase cap
    decision = await decide(book, AAA)
    assert outcome(decision) == (False, "vetoed", vet.call_id)
    assert decision.detail.endswith("; vet veto 0.50 (20261103-vet-01-1)")
    assert service.intents == []


async def test_allow_past_the_chase_cap_is_chased(desk: Desk) -> None:
    await desk.at(VET_AT)
    desk.verdicts = {"AAA": "allow"}
    vet = await desk.vet(AAA)
    book, service, _ = book_b(desk, inputs=Inputs("14.90"))
    decision = await decide(book, AAA)
    assert outcome(decision) == (False, "chased", vet.call_id)
    assert "last 14.90 > max_chase 14.85; vet allow" in decision.detail
    assert service.intents == []


async def test_an_allowed_signal_that_cant_be_sized_keeps_its_call(desk: Desk) -> None:
    await desk.at(VET_AT)
    desk.verdicts = {"AAA": "allow"}
    vet = await desk.vet(AAA)
    book, service, _ = book_b(desk, service=Service(cash="5"))
    decision = await decide(book, AAA)
    assert outcome(decision) == (False, "insufficient_settled_cash", vet.call_id)
    assert decision.detail.endswith("; vet allow 0.50 (20261103-vet-01-1)")
    assert service.intents == []


async def test_a_premarket_verdict_stands(desk: Desk) -> None:
    desk.verdicts = {"AAA": "veto"}
    brief = await desk.brief("AAA")
    await desk.at(VET_AT)
    await desk.vet(AAA, BBB)  # AAA isn't vetted again
    book, _, _ = book_b(desk)
    decision = await decide(book, AAA)
    assert outcome(decision) == (False, "vetoed", brief.call_id)
    assert decision.detail.endswith("premarket veto 0.50 (20261103-premarket-01-1)")


async def test_revet_premarket_waits_for_the_vets_verdict(tmp_path: Path, journal: Journal) -> None:
    desk = Desk(tmp_path, journal, revet=True)
    desk.verdicts = {"AAA": "veto"}
    await desk.brief("AAA")
    await desk.at(VET_AT)
    targets = desk.store.vet_targets([AAA])
    job = desk.store.expect_vet(targets, at=desk.clock.now())
    book, _, _ = book_b(desk)
    assert (await park(book, AAA)).until == DEADLINE
    desk.verdicts = {"AAA": "allow"}
    vet = await desk.run_vet(job, targets)
    assert outcome(await decide(book, AAA, waited=True)) == (True, "entered_after_wait",
                                                            vet.call_id)  # fmt: skip


@pytest.mark.parametrize(
    ("verdict", "reason", "take"),
    [("allow", "entered_after_wait", True), ("veto", "vetoed", False)],
)
async def test_a_re_vet_that_misses_the_deadline_leaves_the_briefs_verdict(
    tmp_path: Path, journal: Journal, verdict: str, reason: str, take: bool
) -> None:
    desk = Desk(tmp_path, journal, revet=True)
    desk.verdicts = {"AAA": verdict}
    brief = await desk.brief("AAA")
    await desk.at(VET_AT)
    job = desk.store.expect_vet(desk.store.vet_targets([AAA]), at=desk.clock.now())
    desk.store.vet_started(job, at=desk.clock.now())  # and it never answers in time
    book, _, _ = book_b(desk)
    await park(book, AAA)
    await desk.at(DEADLINE)
    decision = await decide(book, AAA, waited=True)
    assert outcome(decision) == (take, reason, brief.call_id)
    assert (
        f"premarket {verdict} 0.50 (20261103-premarket-01-1); no re-vet by the deadline"
        in decision.detail
    )


# ---------------------------------------------------------------- a verdict pending


@pytest.mark.parametrize(
    ("verdict", "feed", "reason", "take", "limit"),
    [
        ("allow", "14.82", "entered_after_wait", True, D("14.84")),
        ("neutral", "14.83", "entered_after_wait", True, D("14.85")),  # priced from the feed
        ("allow", "14.86", "chased_after_wait", False, None),  # it ran past the cap meanwhile
        ("veto", "14.82", "vetoed", False, None),
    ],
)
async def test_a_pending_trigger_parks_then_takes_its_verdict(
    desk: Desk,
    journal: Journal,
    verdict: str,
    feed: str,
    reason: str,
    take: bool,
    limit: Decimal | None,
) -> None:
    await desk.at(VET_AT)
    job = desk.store.expect_vet([AAA], at=desk.clock.now())
    book, service, inputs = book_b(desk)
    deferred = await park(book, AAA)
    assert deferred.until == DEADLINE
    assert deferred.detail.endswith("; the vet is expected since 09:35:05")
    assert await rows(journal) == []  # nothing is journaled while it waits
    desk.verdicts = {"AAA": verdict}
    vet = await desk.run_vet(job, [AAA])
    inputs.last = D(feed)  # the trigger keeps its own 14.82
    decision = await decide(book, AAA, waited=True)
    assert outcome(decision) == (take, reason, vet.call_id)
    assert inputs.waited == [False, True]
    assert await rows(journal) == [(AAA.id, int(take), reason, vet.call_id)]
    assert [i.entry_limit for i in service.intents] == ([limit] if limit else [])


async def test_a_trigger_asked_again_while_pending_is_never_chased(desk: Desk) -> None:
    """Whether Book B trades mustn't hang on when it is asked: a spike past the cap while the
    verdict is pending ends nothing; the chase guard runs once the verdict is in (S§5.4)."""
    await desk.at(VET_AT)
    job = desk.store.expect_vet([AAA], at=desk.clock.now())
    book, service, inputs = book_b(desk)
    await park(book, AAA)
    inputs.last = D("14.90")
    assert (await park(book, AAA, waited=True)).until == DEADLINE  # woken by another change
    assert service.intents == []
    desk.verdicts = {"AAA": "allow"}
    vet = await desk.run_vet(job, [AAA])
    inputs.last = D("14.82")
    decision = await decide(book, AAA, waited=True)
    assert outcome(decision) == (True, "entered_after_wait", vet.call_id)


async def test_still_pending_at_its_deadline_is_vet_timeout_whatever_the_price(
    desk: Desk,
) -> None:
    await desk.at(VET_AT)
    job = desk.store.expect_vet([AAA], at=desk.clock.now())
    desk.store.vet_started(job, at=desk.clock.now())
    book, service, inputs = book_b(desk)
    assert (await park(book, AAA)).until == DEADLINE
    await desk.at(at(9, 35, 40))
    assert (await park(book, AAA, waited=True)).until == DEADLINE
    await desk.at(DEADLINE)
    inputs.last = D("14.90")  # past the cap, but no verdict ever came
    decision = await decide(book, AAA, waited=True)
    assert outcome(decision) == (False, "vet_timeout", None)
    assert "; no verdict by 09:36:05; the vet is under way since 09:35:05" in decision.detail
    assert service.intents == []


async def test_a_trigger_after_the_deadline_with_no_verdict_times_out_at_once(
    desk: Desk,
) -> None:
    await desk.at(VET_AT)
    desk.store.expect_vet([AAA], at=desk.clock.now())
    await desk.at(at(9, 37))
    book, _, _ = book_b(desk)
    assert (await decide(book, AAA)).reason == "vet_timeout"


async def test_pending_and_already_chased_on_the_first_look_is_chased(desk: Desk) -> None:
    await desk.at(VET_AT)
    desk.store.expect_vet([AAA], at=desk.clock.now())
    book, service, _ = book_b(desk, inputs=Inputs("14.86"))
    decision = await decide(book, AAA)
    assert outcome(decision) == (False, "chased", None)
    assert decision.detail.endswith("last 14.86 > max_chase 14.85; verdict pending")
    assert service.intents == []


async def test_the_wait_ends_at_the_last_entry(desk: Desk) -> None:
    desk.last_entry = at(9, 35, 30)
    desk.store = desk.new_store()
    await desk.at(VET_AT)
    desk.store.expect_vet([AAA], at=desk.clock.now())
    book, _, _ = book_b(desk)
    assert (await park(book, AAA)).until == at(9, 35, 30)
    await desk.at(at(9, 35, 30))
    assert (await decide(book, AAA, waited=True)).reason == "vet_timeout"


# ---------------------------------------------------------------- no verdict to be had


async def test_a_failed_vet_is_llm_unavailable(desk: Desk) -> None:
    await desk.at(VET_AT)
    desk.script = crash
    vet = await desk.vet(AAA)
    book, _, _ = book_b(desk)
    decision = await decide(book, AAA)
    assert outcome(decision) == (False, "llm_unavailable", vet.call_id)
    assert decision.detail.endswith("; the vet failed: nonzero_exit (20261103-vet-01-1)")


async def test_a_symbol_the_vet_left_out_is_llm_unavailable(desk: Desk) -> None:
    await desk.at(VET_AT)
    desk.leave_out = {"BBB"}
    vet = await desk.vet(AAA, BBB)
    book, _, _ = book_b(desk)
    assert outcome(await decide(book, BBB)) == (False, "llm_unavailable", vet.call_id)


async def test_a_signal_no_call_covers_is_llm_unavailable(desk: Desk) -> None:
    book, _, _ = book_b(desk)
    decision = await decide(book, AAA)
    assert outcome(decision) == (False, "llm_unavailable", None)
    assert decision.detail.endswith("; no LLM call covers it")


# ---------------------------------------------------------------- halts, degradation, mode


async def test_a_degraded_day_takes_no_entry(desk: Desk) -> None:
    desk.script = bad_answer
    await desk.brief("AAA")  # two failed attempts
    await desk.at(VET_AT)
    desk.script = crash
    await desk.vet(AAA)  # the third
    book, service, _ = book_b(desk)
    decision = await decide(book, AAA)
    assert outcome(decision) == (False, "llm_degraded", None)
    assert decision.detail.endswith("; 3 failed LLM attempts today (llm_failures_degrade_after 3)")
    assert service.intents == []


async def test_a_failed_attempt_degrades_before_its_call_ends(desk: Desk) -> None:
    """RISK.md §2 counts attempts: a first try that failed while its repair runs counts."""
    await desk.at(VET_AT)
    desk.verdicts = {"AAA": "allow"}
    await desk.vet(AAA)
    await desk.failed_attempts(3)
    assert desk.store.failures == 0  # no call has ended since
    book, service, _ = book_b(desk)
    assert (await decide(book, AAA)).reason == "llm_degraded"
    assert service.intents == []


async def test_the_degraded_halt_alone_is_llm_degraded(desk: Desk) -> None:
    await desk.at(VET_AT)
    desk.verdicts = {"AAA": "allow"}
    await desk.vet(AAA)
    book, service, _ = book_b(desk, inputs=Inputs(halts=("llm_degraded",)))
    assert (await decide(book, AAA)).reason == "llm_degraded"
    assert service.intents == []


@pytest.mark.parametrize(
    ("halts", "why"),
    [
        (("daily_loss", "llm_degraded"), "halted: daily_loss"),
        (("kill_switch",), "halted: kill_switch"),
    ],
)
async def test_any_other_halt_is_book_halted(desk: Desk, halts: tuple[str, ...], why: str) -> None:
    desk.script = crash
    await desk.at(VET_AT)
    await desk.vet(AAA)  # failed: a halt comes first anyway
    book, _, _ = book_b(desk, inputs=Inputs(halts=halts))
    decision = await decide(book, AAA)
    assert (decision.reason, decision.llm_call_id) == ("book_halted", None)
    assert decision.detail.endswith(why)


async def test_a_book_that_flattened_today_is_halted_before_it_waits(desk: Desk) -> None:
    await desk.at(VET_AT)
    desk.store.expect_vet([AAA], at=desk.clock.now())
    service = Service()
    service.accepting = False
    book, _, _ = book_b(desk, service=service)
    decision = await decide(book, AAA)
    assert decision.reason == "book_halted"
    assert decision.detail.endswith("halted: the book flattened today")


async def test_no_trade_beats_every_verdict_and_none(desk: Desk, journal: Journal) -> None:
    desk.verdicts, desk.day_mode = {"AAA": "allow", "BBB": "veto"}, "no_trade"
    brief = await desk.brief("AAA", "BBB")
    await desk.at(VET_AT)
    desk.store.expect_vet([CCC], at=desk.clock.now())
    ddd = long("DDD", 4)
    await save_signals(journal, [ddd])
    assert [desk.store.standing(s).kind for s in (AAA, BBB, CCC, ddd)] == [
        "verdict", "verdict", "pending", "unavailable",
    ]  # fmt: skip
    book, service, _ = book_b(desk)
    for sig in (AAA, BBB, CCC, ddd):
        decision = await decide(book, sig)
        assert outcome(decision) == (False, "day_mode_no_trade", brief.call_id)
        assert decision.detail.endswith("; day mode no_trade (20261103-premarket-01-1)")
    assert service.intents == []


async def test_degraded_comes_before_the_day_mode(desk: Desk) -> None:
    desk.day_mode = "no_trade"
    await desk.brief("AAA")
    await desk.failed_attempts(3)
    book, _, _ = book_b(desk)
    assert (await decide(book, AAA)).reason == "llm_degraded"


async def test_a_reduced_day_halves_the_size(desk: Desk) -> None:
    desk.verdicts, desk.day_mode = {"AAA": "allow"}, "reduced"
    brief = await desk.brief("AAA")
    book, service, _ = book_b(desk)
    decision = await decide(book, AAA)
    assert outcome(decision) == (True, "taken", brief.call_id)
    (intent,) = service.intents
    assert (intent.qty, intent.day_size_multiplier) == (16, D("0.5"))
    assert decision.detail.endswith(
        "; qty 16; premarket allow 0.50 (20261103-premarket-01-1); day mode reduced x0.5"
    )


async def test_a_gate_rejection_after_a_wait_keeps_its_reason_and_its_call(desk: Desk) -> None:
    await desk.at(VET_AT)
    job = desk.store.expect_vet([AAA], at=desk.clock.now())
    book, _, _ = book_b(desk, service=Service("rejected", violations=("R-007",)))
    await park(book, AAA)
    desk.verdicts = {"AAA": "allow"}
    vet = await desk.run_vet(job, [AAA])
    decision = await decide(book, AAA, waited=True)
    assert outcome(decision) == (False, "capacity_entries", vet.call_id)


# ---------------------------------------------------------------- after a restart


async def test_a_rebuilt_store_decides_as_the_old_one_did(desk: Desk) -> None:
    desk.verdicts, desk.day_mode = {"AAA": "veto", "BBB": "allow"}, "reduced"
    await desk.brief("AAA")
    await desk.at(VET_AT)
    desk.leave_out = {"CCC"}
    await desk.vet(AAA, BBB, CCC)
    before, _, _ = book_b(desk)
    first = [outcome(await decide(before, s)) for s in (AAA, BBB, CCC)]
    desk.store = desk.new_store()
    await desk.store.load()
    after, service, _ = book_b(desk)
    assert [outcome(await decide(after, s)) for s in (AAA, BBB, CCC)] == first
    assert [r for _, r, _ in first] == ["vetoed", "taken", "llm_unavailable"]
    assert service.intents[0].qty == 16  # the reduced day survived the restart


async def test_the_deadline_after_a_restart_counts_from_the_new_vet(desk: Desk) -> None:
    await desk.at(VET_AT)
    await abandon(desk, lambda: desk.vet(AAA))
    await desk.at(at(9, 40))
    desk.store = desk.new_store()
    rebuilt = await desk.store.load()
    book, _, _ = book_b(desk)
    restarted = trigger(AAA, source="restart")  # re-served: triggered before the crash
    first = await book.on_trigger(restarted, AAA)
    assert isinstance(first, Defer)
    assert first.until == at(9, 41)
    await desk.at(at(9, 40, 7))
    targets = desk.store.vet_targets([AAA])
    assert [s.symbol for s in targets] == ["AAA"]
    assert "AAA" in rebuilt.revet
    job = desk.store.expect_vet(targets, at=desk.clock.now())
    desk.store.vet_started(job, at=desk.clock.now())
    second = await book.on_trigger(restarted, AAA, waited=True)
    assert isinstance(second, Defer)
    assert second.until == at(9, 41, 7)
    desk.verdicts = {"AAA": "allow"}
    vet = await desk.run_vet(job, targets)
    decision = await book.on_trigger(restarted, AAA, waited=True)
    assert isinstance(decision, BookDecision)
    assert outcome(decision) == (True, "entered_after_wait", vet.call_id)
