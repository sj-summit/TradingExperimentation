"""Book A, rules only (S§4): executes S§3 exactly, subject to the risk gate. No LLM.

On a trigger: a halted book declines first (``book_halted``: the reason that matters, D-048);
otherwise price the entry (S§3.3), check the chase guard, size it from the book's exposure
(S§3.5), and hand the intent to ExecutionService, whose gate re-checks every cap under the
book's lock. The decision is journaled whatever happens.
"""

from decimal import Decimal

from trader.books.base import BookDecision, EntryInputs, decision_for, record_decision
from trader.config import BookName, RiskConfig, StrategyConfig
from trader.core.clock import Clock
from trader.core.ids import client_order_id
from trader.core.models import OrderIntent, Signal, TriggerEvent
from trader.execution.service import ExecutionService
from trader.memory.journal import Journal
from trader.strategy.pricing import entry_limit, is_chased
from trader.strategy.sizing import sizing


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
        self._strategy, self._risk, self._multiplier = strategy, risk, multiplier

    async def on_trigger(self, ev: TriggerEvent, sig: Signal) -> BookDecision:
        decision = await self._decide(ev, sig)
        await record_decision(self._journal, decision, at=self._clock.now())
        return decision

    async def _decide(self, ev: TriggerEvent, sig: Signal) -> BookDecision:
        trigger, stop, chase = sig.trigger_price, sig.stop_price, sig.max_chase_price
        if trigger is None or stop is None or chase is None:
            raise ValueError(f"{sig.id} is not a priced long signal")
        prepared = await self._inputs.prepare(self.name, sig, ev)
        last = prepared.last_price if prepared.last_price is not None else ev.price
        seen = (
            f"{ev.source} {ev.price} x{ev.size or '?'} [{','.join(ev.conditions)}] "
            f"at {ev.ts:%H:%M:%S.%f}; last {last}"
        )
        if prepared.halts or not self._service.accepting:
            why = ", ".join(prepared.halts) or "the book flattened today"
            return self._skip(sig, "book_halted", f"{seen}; halted: {why}")
        if is_chased(last, chase):
            return self._skip(sig, "chased", f"{seen} > max_chase {chase}")
        limit = entry_limit(trigger, last, entry=self._strategy.entry, max_chase=chase)
        exposure = await self._service.exposure()
        size = sizing(
            limit,
            stop,
            exposure.equity_sod,
            exposure.available_cash,
            self._risk.max_risk_per_trade_pct,
            self._risk.max_position_pct,
            self._multiplier,
        )
        sized = (
            f"{seen}; limit {limit} stop {stop}; "
            f"E {exposure.equity_sod} S {exposure.available_cash}"
        )
        if size.qty < 1:
            reason = "insufficient_settled_cash" if size.by_cash < 1 else "qty_zero"
            return self._skip(sig, reason, sized)
        intent = OrderIntent(
            book=self.name,
            signal_id=sig.id,
            symbol=sig.symbol,
            qty=size.qty,
            entry_limit=limit,
            stop_price=stop,
            client_order_id=client_order_id(self.name, sig.date, sig.symbol, "entry", 1),
            day_size_multiplier=self._multiplier,
        )
        result = await self._service.try_enter(
            intent, self._inputs.builder(self.name, sig, prepared)
        )
        return decision_for(self.name, sig, intent, result, f"{sized}; qty {size.qty}")

    def _skip(self, sig: Signal, reason: str, detail: str) -> BookDecision:
        return BookDecision(book=self.name, signal_id=sig.id, take=False, reason=reason,
                            detail=detail)  # fmt: skip
