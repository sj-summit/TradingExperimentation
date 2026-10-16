"""Book A's decisions and the decision log (A§4.4, S§4, P3.4; DECISIONS D-044)."""

from collections.abc import AsyncIterator
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from tests.fixtures.exec_harness import RISK, SETTINGS, TIMES
from trader.books.base import (
    PreparedInputs,
    decided,
    finalize_decisions,
    record_decision,
    record_skips,
    signals_with_orders,
)
from trader.books.baseline import BaselineBook
from trader.books.reasons import reason_from_violations
from trader.core.clock import ET, SimClock
from trader.core.models import Asset, OrderIntent, RiskCheck, Signal, TriggerEvent
from trader.execution.service import EntryResult, Exposure, GateStateBuilder
from trader.memory.journal import Journal
from trader.risk.gate import GateState
from trader.strategy.orb_sip import save_signals

D = Decimal
DAY = date(2026, 10, 13)
NOW = datetime.combine(DAY, time(9, 40, 1), ET)
ASSET = Asset(symbol="XYZ", name="XYZ", exchange="NASDAQ", asset_class="us_equity",
              status="active", tradable=True)  # fmt: skip


def signal(symbol: str = "XYZ") -> Signal:
    return Signal(
        id=f"20261013-{symbol}-orb5", date=DAY, symbol=symbol, direction="long", status="triggered",
        rel_vol=8.0, rel_vol_rank=1, or_open=D("12.40"), or_high=D("12.71"), or_low=D("12.30"),
        or_close=D("12.65"), or_volume=50_000, atr14=D("0.80"), adv14=2_000_000,
        prev_close=D("12.00"), gap_pct=3.3, trigger_price=D("12.72"), stop_price=D("12.64"),
        max_chase_price=D("12.75"), entry_limit=D("12.74"),
    )  # fmt: skip


def event(price: str = "12.73", source: str = "stream_trade") -> TriggerEvent:
    return TriggerEvent(signal_id="20261013-XYZ-orb5", symbol="XYZ", rank=1, ts=NOW,
                        received_at=NOW, price=D(price), size=300, conditions=("@",),
                        source=source)  # type: ignore[arg-type]  # fmt: skip


class Service:
    """The slice of ExecutionService a book uses, with a scripted outcome."""

    def __init__(self, outcome: str = "submitted", *, violations: tuple[str, ...] = (),
                 equity: str = "1000", cash: str = "1000", held: tuple[str, ...] = (),
                 entries: int = 0) -> None:  # fmt: skip
        self.outcome, self.violations = outcome, violations
        self.equity, self.cash = D(equity), D(cash)
        self.held, self.entries = frozenset(held), entries
        self.intents: list[OrderIntent] = []
        self.states: list[GateState] = []
        self.accepting = True

    async def exposure(self, intent: OrderIntent | None = None) -> Exposure:
        return Exposure(symbols=self.held, entries_today=self.entries,
                        symbols_entered_today=self.held,
                        available_cash=self.cash, equity_sod=self.equity,
                        realized_pnl_today=D(0), open_risk=D(0), ledger_equity=self.equity,
                        unprotected=frozenset(), client_order_id_used=False,
                        open_order_for_signal=False)  # fmt: skip

    async def try_enter(self, intent: OrderIntent, build: GateStateBuilder) -> EntryResult:
        self.intents.append(intent)
        self.states.append(await build(await self.exposure(intent)))
        if self.outcome == "rejected":
            check = RiskCheck(approved=False, violations=self.violations, state_digest={})
            return EntryResult("rejected", check)
        if self.outcome == "broker_rejected":
            return EntryResult("broker_rejected", reason="insufficient buying power")
        if self.outcome == "halted":
            return EntryResult("halted", reason="the book is flattening or closed")
        return EntryResult(self.outcome)  # type: ignore[arg-type]


class Inputs:
    def __init__(self, last: str | None = None) -> None:
        self.last = D(last) if last else None

    async def prepare(self, book: str, sig: Signal, ev: TriggerEvent) -> PreparedInputs:
        return PreparedInputs(last_price=self.last, quote_age_s=0.4, broker_equity=D(1000),
                              asset=ASSET)  # fmt: skip

    def builder(self, book: str, sig: Signal, prepared: PreparedInputs) -> GateStateBuilder:
        async def build(exposure: Exposure) -> GateState:
            return GateState(mode="paper", now=NOW, day=TIMES,
                             available_cash=exposure.available_cash,
                             equity_sod=exposure.equity_sod, ledger_equity=exposure.ledger_equity,
                             broker_equity=prepared.broker_equity, asset=prepared.asset,
                             quote_age_s=prepared.quote_age_s)  # fmt: skip

        return build


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    await save_signals(opened, [signal(), signal("ABC")])
    yield opened
    await opened.aclose()


def book(journal: Journal, service: Service, inputs: Inputs | None = None) -> BaselineBook:
    return BaselineBook(service=service, inputs=inputs or Inputs("12.73"), journal=journal,  # type: ignore[arg-type]
                        clock=SimClock(NOW), strategy=SETTINGS.strategy, risk=RISK)  # fmt: skip


async def rows(journal: Journal) -> list[tuple[str, int, str]]:
    return [(r["book"], r["take"], r["reason_code"]) for r in await journal.fetch_all(
        "SELECT book, take, reason_code FROM decisions ORDER BY id")]  # fmt: skip


async def test_a_trigger_is_priced_sized_and_taken(journal: Journal) -> None:
    service = Service()
    decision = await book(journal, service).on_trigger(event(), signal())
    assert (decision.take, decision.reason) == (True, "taken")
    (intent,) = service.intents
    # limit min(max(ceil(12.73 x 1.001), 12.72 + 0.02), 12.75) = 12.75; the $500 notional cap
    # binds: floor(500 / 12.75) = 39 shares
    assert (intent.entry_limit, intent.stop_price, intent.qty) == (D("12.75"), D("12.64"), 39)
    assert intent.client_order_id == "A-20261013-XYZ-entry-1"
    assert "stream_trade 12.73 x300 [@]" in decision.detail
    assert "qty 39" in decision.detail
    assert service.states[0].quote_age_s == 0.4
    assert await rows(journal) == [("A", 1, "taken")]


async def test_an_arm_check_trigger_uses_its_own_price(journal: Journal) -> None:
    service = Service()
    await book(journal, service, Inputs(last=None)).on_trigger(
        event("12.74", "arm_check"), signal()
    )
    assert service.intents[0].entry_limit == D("12.75")  # priced from the snapshot's 12.74


async def test_skips_before_the_gate(journal: Journal) -> None:
    chased = await book(journal, Service(), Inputs("12.76")).on_trigger(event(), signal())
    assert (chased.take, chased.reason) == (False, "chased")
    tiny = await book(journal, Service(equity="10")).on_trigger(event(), signal())
    assert tiny.reason == "qty_zero"  # $0.10 of risk budget can't buy a share
    broke = await book(journal, Service(cash="5")).on_trigger(event(), signal())
    assert broke.reason == "insufficient_settled_cash"
    assert await rows(journal) == [("A", 0, "chased"), ("A", 0, "qty_zero"),
                                   ("A", 0, "insufficient_settled_cash")]  # fmt: skip


@pytest.mark.parametrize(
    ("service", "reason"),
    [
        (Service(cash="5", entries=2), "capacity_entries"),  # STT on 10-14 (D-054 #5)
        (Service(cash="5", held=("ABC", "DEF"), entries=2), "capacity_positions"),
        (Service(equity="10", held=("ABC", "DEF")), "capacity_positions"),
        (Service(cash="5", entries=1), "insufficient_settled_cash"),
    ],
)
async def test_a_capacity_rule_that_also_binds_names_a_size_skip(
    journal: Journal, service: Service, reason: str
) -> None:
    """D-056: the reason no longer depends on the price. R-006 and R-007 rank first, as in
    the gate (D-044); a label only, as the skip happens either way."""
    decision = await book(journal, service).on_trigger(event(), signal())
    assert (decision.take, decision.reason) == (False, reason)
    assert service.intents == []  # never sent to the gate
    if reason.startswith("capacity"):
        assert "; capacity: R-00" in decision.detail


@pytest.mark.parametrize(
    ("service", "reason", "take", "detail"),
    [
        (Service("rejected", violations=("R-006", "R-007")), "capacity_positions", False,
         "R-006, R-007"),
        (Service("rejected", violations=("R-012",)), "risk_gate:R-012", False, "R-012"),
        (Service("rejected", violations=("R-009",)), "insufficient_settled_cash", False, "R-009"),
        (Service("broker_rejected"), "broker_rejected", False, "insufficient buying power"),
        (Service("uncertain"), "taken", True, "submit_uncertain"),
        (Service("halted"), "book_halted", False, "flattening or closed"),
    ],
)  # fmt: skip
async def test_execution_outcomes_map_to_reason_codes(
    journal: Journal, service: Service, reason: str, take: bool, detail: str
) -> None:
    decision = await book(journal, service).on_trigger(event(), signal())
    assert (decision.reason, decision.take) == (reason, take)
    assert detail in decision.detail


def test_gate_violations_name_a_reason_in_priority_order() -> None:
    assert reason_from_violations(("R-007", "R-005")) == "book_halted"
    assert reason_from_violations(("R-016", "R-021")) == "risk_gate:R-016"
    assert reason_from_violations(()) == "risk_gate:unknown"


async def test_the_log_is_append_only_and_skips_repeats(journal: Journal) -> None:
    taken = await book(journal, Service()).on_trigger(event(), signal())
    assert await record_decision(journal, taken, at=NOW) is False  # the same again: no row
    await record_skips(journal, ("A", "B"), [signal("ABC")], "missed_at_arm", at=NOW)
    await record_skips(journal, ("A", "B"), [signal("ABC")], "missed_at_arm", at=NOW)
    assert await rows(journal) == [("A", 1, "taken"), ("A", 0, "missed_at_arm"),
                                   ("B", 0, "missed_at_arm")]  # fmt: skip
    assert await decided(journal, DAY) == {
        "20261013-XYZ-orb5": frozenset({"A"}), "20261013-ABC-orb5": frozenset({"A", "B"}),
    }  # fmt: skip


async def entry(journal: Journal, status: str, *, filled: int = 0, symbol: str = "XYZ") -> None:
    sid = f"20261013-{symbol}-orb5"
    await journal.execute(
        "INSERT INTO orders (client_order_id, broker_order_id, book, signal_id, symbol, side, "
        "type, order_class, qty, limit_price, stop_price, status, submitted_at) "
        "VALUES (?, ?, 'A', ?, ?, 'buy', 'limit', 'oto', 39, '12.75', '12.64', ?, ?)",
        (f"A-20261013-{symbol}-entry-1", f"ord-{symbol}", sid, symbol, status, NOW.isoformat()),
    )
    if filled:
        await journal.execute(
            "INSERT INTO fills (id, broker_order_id, book, symbol, side, qty, price, filled_at) "
            "VALUES (?, ?, 'A', ?, 'buy', ?, '12.74', ?)",
            (f"ord-{symbol}:{filled}", f"ord-{symbol}", symbol, filled, NOW.isoformat()),
        )


async def test_eod_marks_taken_entries_that_never_filled(journal: Journal) -> None:
    await book(journal, Service()).on_trigger(event(), signal())
    await entry(journal, "canceled")
    assert await signals_with_orders(journal, DAY) == frozenset({"20261013-XYZ-orb5"})
    assert await finalize_decisions(journal, "A", DAY, at=NOW) == ["20261013-XYZ-orb5"]
    assert await finalize_decisions(journal, "A", DAY, at=NOW) == []  # idempotent
    final = await journal.fetch_all("SELECT reason_code FROM final_decisions")
    assert [r["reason_code"] for r in final] == ["entry_unfilled"]
    assert await rows(journal) == [("A", 1, "taken"), ("A", 0, "entry_unfilled")]


@pytest.mark.parametrize(
    ("status", "filled", "appended"),
    [("rejected", 0, "broker_rejected"), ("filled", 39, None), ("canceled", 12, None),
     ("new", 0, None), ("lost", 0, "entry_unfilled")],
)  # fmt: skip
async def test_eod_outcomes(
    journal: Journal, status: str, filled: int, appended: str | None
) -> None:
    await book(journal, Service()).on_trigger(event(), signal())
    await entry(journal, status, filled=filled)
    await finalize_decisions(journal, "A", DAY, at=NOW)
    final = await journal.fetch_one("SELECT reason_code FROM final_decisions")
    assert final is not None
    assert final["reason_code"] == (appended or "taken")
