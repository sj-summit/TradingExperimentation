"""RISK.md §3: a passing and a failing case for every rule (P2.4 wants 100% branch coverage)."""

import json
from collections.abc import AsyncIterator
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import REPO
from trader.config import RiskConfig, Settings
from trader.core.calendar import Session, day_times
from trader.core.clock import ET
from trader.core.events import Event
from trader.core.models import Asset, OrderIntent, RiskCheck
from trader.core.yamlfile import load_yaml
from trader.memory.journal import Journal
from trader.risk.gate import GateState, RiskGate, capacity_violations, record_risk_check

D = Decimal
RISK = RiskConfig.model_validate(load_yaml(REPO / "config" / "risk.example.yaml"))
SETTINGS = Settings.model_validate(load_yaml(REPO / "config" / "settings.example.yaml"))
GATE = RiskGate(RISK, SETTINGS.strategy.universe)
DAY = date(2026, 10, 9)


def session(close: time = time(16, 0)) -> Session:
    return Session(
        date=DAY,
        open=datetime.combine(DAY, time(9, 30), ET),
        close=datetime.combine(DAY, close, ET),
    )


TIMES = day_times(
    session(),
    entry_window_start=RISK.entry_window_start,
    last_entry=SETTINGS.schedule.last_entry,
    flatten=SETTINGS.schedule.flatten,
)


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(DAY, time(hh, mm, ss), ET)


def asset(**changes: Any) -> Asset:
    base = Asset(
        symbol="XYZ",
        name="XYZ Corp",
        exchange="NASDAQ",
        asset_class="us_equity",
        status="active",
        tradable=True,
    )
    return base.model_copy(update=changes)


def intent(**changes: Any) -> OrderIntent:
    """20 x $25.00 with a $24.80 stop: $500 notional (the 50% cap exactly), $4.00 at risk."""
    base = OrderIntent(
        book="A",
        signal_id="20261009-XYZ-orb5",
        symbol="XYZ",
        qty=20,
        entry_limit=D("25.00"),
        stop_price=D("24.80"),
        client_order_id="A-20261009-XYZ-entry-1",
        day_size_multiplier=D("1.0"),
    )
    return base.model_copy(update=changes)


def state(**changes: Any) -> GateState:
    base = GateState(
        mode="paper",
        now=at(10, 0),
        day=TIMES,
        available_cash=D(1000),
        equity_sod=D(1000),
        ledger_equity=D(1000),
        broker_equity=D(1000),
        asset=asset(),
        quote_age_s=1.0,
    )
    return base.model_copy(update=changes)


def check(intent_changes: dict[str, Any] | None = None, **state_changes: Any) -> RiskCheck:
    return GATE.check(intent(**(intent_changes or {})), state(**state_changes))


def event(hh: int, mm: int, impact: str = "high") -> Event:
    return Event(
        date=DAY,
        time=time(hh, mm),
        name="CPI",
        impact=impact,  # type: ignore[arg-type]
        source="https://www.bls.gov/schedule/",
    )


# ---------------------------------------------------------------- the happy path


def test_a_clean_entry_passes_with_nothing_adjusted() -> None:
    result = check()
    assert (result.approved, result.violations, result.adjusted_qty) == (True, (), None)
    json.dumps(result.state_digest)  # journaled as JSON


def test_every_rule_is_evaluated_and_reported_in_order() -> None:
    result = check({"side": "sell"}, mode="live", kill_switch=True, quote_age_s=None)
    assert result.violations == ("R-001", "R-002", "R-005", "R-016")
    assert not result.approved


# ---------------------------------------------------------------- one rule at a time


@pytest.mark.parametrize(
    ("rule", "intent_changes", "state_changes"),
    [
        ("R-001", {}, {"mode": "live"}),
        ("R-002", {"side": "sell"}, {}),
        ("R-003", {}, {"asset": None}),
        ("R-003", {}, {"asset": asset(symbol="ABC")}),
        ("R-003", {}, {"asset": asset(asset_class="crypto")}),
        ("R-003", {}, {"asset": asset(tradable=False)}),
        ("R-003", {}, {"asset": asset(status="inactive")}),
        ("R-003", {}, {"asset": asset(exchange="OTC")}),
        ("R-003", {"symbol": "BRK-B"}, {"asset": asset(symbol="BRK-B")}),
        ("R-004", {}, {"day": None}),
        ("R-004", {}, {"now": at(9, 35, 2)}),  # before the entry window opens at 09:35:03
        ("R-004", {}, {"now": at(15, 30, 1)}),  # after the last entry
        ("R-004", {}, {"now": at(16, 5)}),  # after the close
        ("R-005", {}, {"halts": ("manual",)}),
        ("R-005", {}, {"kill_switch": True}),
        ("R-006", {}, {"exposure_symbols": frozenset({"AAA", "BBB"})}),
        ("R-007", {}, {"entries_today": 2}),
        ("R-008", {}, {"symbols_entered_today": frozenset({"XYZ"})}),
        ("R-009", {}, {"available_cash": D(20)}),
        ("R-009", {}, {"available_cash": D(-5)}),
        ("R-010", {}, {"equity_sod": D(990)}),
        ("R-010", {"day_size_multiplier": D("0.5")}, {}),
        ("R-011", {"stop_price": D("24.40")}, {}),  # 20 x 0.60 = $12 > 1% of $1,000
        ("R-012", {"stop_price": D("24.99")}, {}),  # 1 cent away: under min_stop_distance
        ("R-012", {"stop_price": D("25.00")}, {}),  # not below the limit at all
        ("R-012", {"qty": 4, "stop_price": D("23.00")}, {}),  # 8% away: over max 5%
        ("R-013", {"entry_limit": D("4.00"), "stop_price": D("3.90")}, {}),
        ("R-013", {"qty": 10, "entry_limit": D("25.005")}, {}),  # sub-penny
        ("R-013", {"qty": 10, "stop_price": D("24.805")}, {}),
        ("R-014", {"qty": 0}, {}),
        ("R-014", {"qty": -3}, {}),
        ("R-015", {}, {"realized_pnl_today": D(-28)}),  # $28 lost + $4 new risk > $30
        ("R-015", {}, {"open_risk": D(27)}),
        ("R-016", {}, {"quote_age_s": 16.0}),
        ("R-016", {}, {"quote_age_s": None}),
        ("R-016", {}, {"hub_stale": True}),
        ("R-017", {}, {"symbol_halted": True}),
        ("R-017", {}, {"corporate_action_today": True}),
        ("R-018", {}, {"client_order_id_used": True}),
        ("R-018", {}, {"open_order_for_signal": True}),
        ("R-019", {}, {"broker_equity": D(799)}),  # 20.1% under the ledger
        ("R-019", {}, {"broker_equity": None}),
        ("R-020", {}, {"unprotected_symbols": frozenset({"AAA"})}),
        ("R-021", {}, {"events": (event(10, 4),)}),  # within 5 min before
        ("R-021", {}, {"events": (event(9, 50),)}),  # within 10 min after
    ],
)
def test_each_rule_fails_alone(
    rule: str, intent_changes: dict[str, Any], state_changes: dict[str, Any]
) -> None:
    result = check(intent_changes, **state_changes)
    assert result.violations == (rule,)
    assert not result.approved


@pytest.mark.parametrize(
    ("intent_changes", "state_changes"),
    [
        ({}, {"mode": "live", "live_gate_ok": True}),  # R-001 with the full live gate
        ({}, {"now": at(9, 35, 3)}),  # R-004: the window's first second
        ({}, {"now": at(15, 30)}),  # R-004: the last entry second
        ({}, {"exposure_symbols": frozenset({"AAA"})}),  # R-006: one slot left
        ({}, {"entries_today": 1}),
        ({}, {"symbols_entered_today": frozenset({"ABC"})}),
        ({"qty": 4, "stop_price": D("23.75")}, {}),  # R-012: exactly 5% away
        ({"stop_price": D("24.97")}, {}),  # R-012: exactly the minimum distance
        ({}, {"realized_pnl_today": D(50)}),  # R-015: gains don't add headroom
        ({}, {"realized_pnl_today": D(-26)}),  # R-015: $26 + $4 == $30 exactly
        ({}, {"quote_age_s": 15.0}),  # R-016: at the limit
        ({}, {"symbol_halted": False}),
        ({}, {"broker_equity": D(800)}),  # R-019: exactly 20% under
        ({}, {"events": (event(10, 6),)}),  # R-021: the window opens at 10:01
        ({}, {"events": (event(9, 49),)}),  # R-021: the window closed at 09:59
        ({}, {"events": (event(10, 0, impact="medium"),)}),  # medium never blocks
    ],
)
def test_boundaries_that_pass(
    intent_changes: dict[str, Any], state_changes: dict[str, Any]
) -> None:
    assert check(intent_changes, **state_changes).violations == ()


def test_live_account_over_the_ceiling_fails_r019() -> None:
    result = check(
        mode="live",
        live_gate_ok=True,
        broker_equity=D("2000.01"),
        ledger_equity=D("2000.01"),
        equity_sod=D("2000.01"),
        available_cash=D("2000.01"),
    )
    assert result.violations == ("R-019",)


def test_the_size_multiplier_can_only_shrink() -> None:
    assert check({"day_size_multiplier": D("2.0"), "qty": 21}).violations == ("R-010",)


# ---------------------------------------------------------------- R-009 adjusts


def test_r009_reduces_the_quantity_to_fit_settled_cash() -> None:
    result = check(available_cash=D(300))
    assert (result.approved, result.adjusted_qty) == (True, 12)  # floor(300 / 25)
    assert result.state_digest["qty_after_cash"] == 12


def test_later_rules_judge_the_reduced_quantity() -> None:
    # 40 shares fail R-010 ($1,000 > $500), but cash cuts them to 12 ($300), which passes.
    assert check({"qty": 40}, available_cash=D(300)).violations == ()


def test_a_zero_limit_is_left_to_the_price_rules() -> None:
    result = check({"entry_limit": D(0), "stop_price": D(0)})
    assert "R-009" not in result.violations
    assert {"R-012", "R-013"} <= set(result.violations)


# ---------------------------------------------------------------- early close and journal


def test_early_close_moves_last_entry_and_flatten_up() -> None:
    early = day_times(
        session(close=time(13, 0)),
        entry_window_start=RISK.entry_window_start,
        last_entry=SETTINGS.schedule.last_entry,
        flatten=SETTINGS.schedule.flatten,
    )
    assert (early.last_entry.time(), early.flatten.time(), early.flat_by.time()) == (
        time(12, 30),
        time(12, 55),
        time(12, 58),
    )
    assert (TIMES.last_entry.time(), TIMES.flatten.time(), TIMES.flat_by.time()) == (
        time(15, 30),
        time(15, 55),
        time(15, 58),
    )
    assert check(day=early, now=at(12, 31)).violations == ("R-004",)


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    yield opened
    await opened.aclose()


async def test_the_check_is_journaled(journal: Journal) -> None:
    result = check(available_cash=D(20))
    await record_risk_check(journal, intent(), result, at(10, 0))
    row = await journal.fetch_one("SELECT * FROM risk_checks")
    assert row is not None
    assert (row["signal_id"], row["book"], row["approved"]) == ("20261009-XYZ-orb5", "A", 0)
    assert json.loads(row["violations_json"]) == ["R-009"]
    assert json.loads(row["state_json"])["available_cash"] == "20"


def test_capacity_violations_name_r006_and_r007_alone() -> None:
    """What a book uses to name a skip it made for size before the gate (D-056)."""
    assert capacity_violations(RISK, positions=1, entries_today=1) == ()
    assert capacity_violations(RISK, positions=2, entries_today=1) == ("R-006",)
    assert capacity_violations(RISK, positions=1, entries_today=2) == ("R-007",)
    assert capacity_violations(RISK, positions=2, entries_today=2) == ("R-006", "R-007")
    loose = RISK.model_copy(update={"max_entries_per_day": 99})  # past the cap of 6
    assert capacity_violations(loose, positions=0, entries_today=6) == ("R-007",)
