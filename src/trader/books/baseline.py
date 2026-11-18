"""Book A, rules only (S§4): executes S§3 exactly, subject to the risk gate. No LLM.

On a trigger:

1. A halted book declines first (``book_halted``, the reason that matters; D-048).
2. Otherwise the shared entry path runs (books/entry.py). It checks the chase guard, prices the
   entry (S§3.3), sizes it from the book's exposure (S§3.5), and hands the intent to
   ExecutionService, whose gate re-checks every cap under the book's lock.

The decision is journaled whatever happens.
"""

from decimal import Decimal

from trader.books.base import BookDecision, EntryInputs, record_decision
from trader.books.entry import EntryPath, priced, seen, skip
from trader.config import BookName, RiskConfig, StrategyConfig
from trader.core.clock import Clock
from trader.core.models import Signal, TriggerEvent
from trader.execution.service import ExecutionService
from trader.memory.journal import Journal


class BaselineBook:
    def __init__(
        self,
        *,
        service: ExecutionService,
        inputs: EntryInputs,
        journal: Journal,
        clock: Clock,
        strategy: StrategyConfig,
        risk: RiskConfig,
        name: BookName = "A",
        multiplier: Decimal = Decimal(1),
    ) -> None:
        self.name: BookName = name
        self._service, self._inputs, self._journal, self._clock = service, inputs, journal, clock
        self._multiplier = multiplier
        self._path = EntryPath(name, service, inputs, strategy, risk)

    async def on_trigger(
        self, ev: TriggerEvent, sig: Signal, *, waited: bool = False
    ) -> BookDecision:
        """Book A never waits, so ``waited`` changes nothing."""
        decision = await self._decide(ev, sig)
        await record_decision(self._journal, decision, at=self._clock.now())
        return decision

    async def _decide(self, ev: TriggerEvent, sig: Signal) -> BookDecision:
        priced(sig)
        prepared = await self._inputs.prepare(self.name, sig, ev)
        last = prepared.last_price if prepared.last_price is not None else ev.price
        what = seen(ev, last)
        if prepared.halts or not self._service.accepting:
            why = ", ".join(prepared.halts) or "the book flattened today"
            return skip(self.name, sig, "book_halted", f"{what}; halted: {why}")
        return await self._path.enter(
            ev, sig, prepared, last=last, seen=what, multiplier=self._multiplier
        )
