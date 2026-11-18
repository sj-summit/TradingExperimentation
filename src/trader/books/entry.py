"""The entry path both books share (S§3.3-3.5, A§4.4; DECISIONS D-068).

Book A runs it on every trigger unless the book is halted (S§4). Book B runs it once a verdict
lets the signal through (S§5.4), with its day mode's size multiplier (S§5.1). After a wait, a
chased entry is ``chased_after_wait`` and a taken one ``entered_after_wait``.

In order:

1. the chase guard (S§3.3);
2. the real limit;
3. the size, from the book's exposure (S§3.5). A size of nothing names its reason before the
   gate (D-056);
4. the intent, to ExecutionService, whose gate re-checks every cap under the book's lock.
"""

from dataclasses import dataclass, replace
from decimal import Decimal

from trader.books.base import BookDecision, EntryInputs, PreparedInputs, decision_for
from trader.books.reasons import reason_for_no_size
from trader.config import BookName, RiskConfig, StrategyConfig
from trader.core.ids import client_order_id
from trader.core.models import OrderIntent, Signal, TriggerEvent
from trader.execution.service import ExecutionService
from trader.risk.gate import capacity_violations
from trader.strategy.pricing import entry_limit, is_chased
from trader.strategy.sizing import sizing


def priced(sig: Signal) -> tuple[Decimal, Decimal, Decimal]:
    """A long's trigger, stop and chase cap; a signal without them can't be entered."""
    trigger, stop, chase = sig.trigger_price, sig.stop_price, sig.max_chase_price
    if trigger is None or stop is None or chase is None:
        raise ValueError(f"{sig.id} is not a priced long signal")
    return trigger, stop, chase


def seen(ev: TriggerEvent, last: Decimal) -> str:
    """The trigger and the price the book decided at, as a decision's detail starts."""
    return (
        f"{ev.source} {ev.price} x{ev.size or '?'} [{','.join(ev.conditions)}] "
        f"at {ev.ts:%H:%M:%S.%f}; last {last}"
    )


def skip(
    book: BookName, sig: Signal, reason: str, detail: str, *, llm_call_id: str | None = None
) -> BookDecision:
    return BookDecision(book=book, signal_id=sig.id, take=False, reason=reason, detail=detail,
                        llm_call_id=llm_call_id)  # fmt: skip


@dataclass(frozen=True)
class EntryPath:
    name: BookName
    service: ExecutionService
    inputs: EntryInputs
    strategy: StrategyConfig
    risk: RiskConfig

    async def enter(
        self,
        ev: TriggerEvent,
        sig: Signal,
        prepared: PreparedInputs,
        *,
        last: Decimal,
        seen: str,
        multiplier: Decimal = Decimal(1),
        waited: bool = False,
        llm_call_id: str | None = None,
        note: str = "",
    ) -> BookDecision:
        """The decision for a trigger the book may act on. ``note`` (Book B's verdict) ends
        every detail; ``llm_call_id`` goes with the decision, whatever it is."""
        trigger, stop, chase = priced(sig)

        def detail(text: str) -> str:
            return f"{text}; {note}" if note else text

        if is_chased(last, chase):
            reason = "chased_after_wait" if waited else "chased"
            return skip(self.name, sig, reason, detail(f"{seen} > max_chase {chase}"),
                        llm_call_id=llm_call_id)  # fmt: skip
        limit = entry_limit(trigger, last, entry=self.strategy.entry, max_chase=chase)
        exposure = await self.service.exposure()
        size = sizing(
            limit,
            stop,
            exposure.equity_sod,
            exposure.available_cash,
            self.risk.max_risk_per_trade_pct,
            self.risk.max_position_pct,
            multiplier,
        )
        sized = (
            f"{seen}; limit {limit} stop {stop}; "
            f"E {exposure.equity_sod} S {exposure.available_cash}"
        )
        if size.qty < 1:
            capacity = capacity_violations(
                self.risk, positions=len(exposure.symbols), entries_today=exposure.entries_today
            )
            also = f"; capacity: {', '.join(capacity)}" if capacity else ""
            reason = reason_for_no_size(capacity, by_cash=size.by_cash)
            return skip(self.name, sig, reason, detail(f"{sized}{also}"),
                        llm_call_id=llm_call_id)  # fmt: skip
        intent = OrderIntent(
            book=self.name,
            signal_id=sig.id,
            symbol=sig.symbol,
            qty=size.qty,
            entry_limit=limit,
            stop_price=stop,
            client_order_id=client_order_id(self.name, sig.date, sig.symbol, "entry", 1),
            day_size_multiplier=multiplier,
        )
        result = await self.service.try_enter(intent, self.inputs.builder(self.name, sig, prepared))
        decision = decision_for(self.name, sig, intent, result, detail(f"{sized}; qty {size.qty}"))
        if waited and decision.reason == "taken":
            decision = replace(decision, reason="entered_after_wait")
        return replace(decision, llm_call_id=llm_call_id)
