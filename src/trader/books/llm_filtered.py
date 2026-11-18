"""Book B: the rules plus Claude's veto (S§5, A§4.4; the Phase 4 plan's decision 8; D-068).

Book A's entry path (books/entry.py), behind the verdicts in ``VerdictStore``. On a trigger,
the first of these that applies decides:

1. **A halt other than ``llm_degraded``**, or a book that flattened today: ``book_halted``.
2. **Degraded,** by the day's failed LLM attempts or by its halt: ``llm_degraded``.
3. **Day mode ``no_trade``:** ``day_mode_no_trade``.
4. **A ``veto``:** ``vetoed``, before the chase guard.
5. **No verdict to be had:** ``llm_unavailable``. The covering call failed or left the symbol
   out, or no call covers it.
6. **Pending:**
   - on the first look, already past the chase cap: ``chased``;
   - before the deadline: ``Defer`` until then;
   - at the deadline: ``vet_timeout``. Under ``revet_premarket``, the brief's verdict instead,
     as step 4 or 7.

   A trigger asked again while still pending is never chased: whether Book B trades mustn't
   depend on when it is asked. Its chase guard runs once the verdict is in (S§5.4).
7. **``allow`` or ``neutral``:** the entry path, with the day mode's multiplier (S§5.1). After a
   wait, its codes are ``entered_after_wait`` and ``chased_after_wait``.

A deferred trigger is asked again with ``waited=True`` when the verdicts change or at its
deadline (P4.8's worker), and its price then comes from the feed (``EntryInputs.prepare``).
Nothing is journaled while it waits. The day's failed attempts are re-read before each decision
(RISK.md §2 counts attempts, not calls). A decision carries the attempt whose answer it rests on
(``llm_call_id``): the verdict's, the brief's for ``day_mode_no_trade``, or the failed one's.
"""

import structlog

from trader.books.base import BookDecision, Defer, EntryInputs, record_decision
from trader.books.entry import EntryPath, priced, seen, skip
from trader.books.verdicts import StoredVerdict, VerdictStore
from trader.config import BookName, RiskConfig, StrategyConfig
from trader.core.clock import Clock
from trader.core.models import Signal, TriggerEvent
from trader.execution.service import ExecutionService
from trader.memory.journal import Journal
from trader.strategy.pricing import is_chased

log = structlog.get_logger(__name__)

LLM_HALT = "llm_degraded"


class LlmFilteredBook:
    def __init__(
        self,
        *,
        service: ExecutionService,
        inputs: EntryInputs,
        journal: Journal,
        clock: Clock,
        strategy: StrategyConfig,
        risk: RiskConfig,
        store: VerdictStore,
        name: BookName = "B",
    ) -> None:
        self.name: BookName = name
        self._service, self._inputs, self._journal, self._clock = service, inputs, journal, clock
        self._store = store
        self._path = EntryPath(name, service, inputs, strategy, risk)

    async def on_trigger(
        self, ev: TriggerEvent, sig: Signal, *, waited: bool = False
    ) -> BookDecision | Defer:
        outcome = await self._decide(ev, sig, waited=waited)
        if isinstance(outcome, Defer):
            log.info("trigger parked", book=self.name, signal=sig.id,
                     until=f"{outcome.until:%H:%M:%S}")  # fmt: skip
            return outcome
        await record_decision(self._journal, outcome, at=self._clock.now())
        return outcome

    async def _decide(self, ev: TriggerEvent, sig: Signal, *, waited: bool) -> BookDecision | Defer:
        _, _, chase = priced(sig)
        prepared = await self._inputs.prepare(self.name, sig, ev, waited=waited)
        last = prepared.last_price if prepared.last_price is not None else ev.price
        what = seen(ev, last)
        store = self._store
        others = tuple(h for h in prepared.halts if h != LLM_HALT)
        if others or not self._service.accepting:
            why = ", ".join(others) or "the book flattened today"
            return skip(self.name, sig, "book_halted", f"{what}; halted: {why}")
        await store.refresh_failures()
        if store.degraded or LLM_HALT in prepared.halts:
            return skip(self.name, sig, "llm_degraded",
                        f"{what}; {store.failures} failed LLM attempts today "
                        f"(llm_failures_degrade_after {store.degrade_after})")  # fmt: skip
        mode = store.day_mode()
        if mode.mode == "no_trade":
            return skip(self.name, sig, "day_mode_no_trade",
                        f"{what}; day mode no_trade ({mode.llm_call_id})",
                        llm_call_id=mode.llm_call_id)  # fmt: skip
        standing = store.standing(sig)
        verdict, note = standing.verdict, ""
        if standing.kind == "pending" and standing.deadline is not None:
            deadline = standing.deadline
            if not waited and is_chased(last, chase):
                return skip(self.name, sig, "chased",
                            f"{what} > max_chase {chase}; verdict pending")  # fmt: skip
            if self._clock.now() < deadline:
                return Defer(until=deadline, detail=f"{what}; {standing.detail}")
            if standing.fallback is None:
                late = f"no verdict by {deadline:%H:%M:%S}"
                return skip(self.name, sig, "vet_timeout", f"{what}; {late}; {standing.detail}")
            verdict, note = standing.fallback, "; no re-vet by the deadline"
        if verdict is None:
            return skip(self.name, sig, "llm_unavailable", f"{what}; {standing.detail}",
                        llm_call_id=standing.llm_call_id)  # fmt: skip
        note = f"{_note(verdict)}{note}"
        if verdict.verdict == "veto":
            return skip(self.name, sig, "vetoed", f"{what}; {note}",
                        llm_call_id=verdict.llm_call_id)  # fmt: skip
        if mode.mode != "normal":
            note = f"{note}; day mode {mode.mode} x{mode.multiplier}"
        return await self._path.enter(
            ev, sig, prepared, last=last, seen=what, multiplier=mode.multiplier,
            waited=waited, llm_call_id=verdict.llm_call_id, note=note,
        )  # fmt: skip


def _note(v: StoredVerdict) -> str:
    return f"{v.source} {v.verdict} {v.confidence:.2f} ({v.llm_call_id})"
