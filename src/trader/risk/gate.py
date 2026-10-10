"""The pre-trade risk gate (RISK.md §3): R-001 to R-021 as small pure functions.

``RiskGate.check(intent, state)`` judges one entry against one ``GateState`` snapshot. Every rule
is evaluated and every violation listed; any violation rejects the entry. Only R-009 changes
anything: it lowers the quantity to fit the settled cash, and the later rules judge the lowered
quantity. The ceilings in risk/caps.py are applied again on top of the configuration, as
defense in depth. Exits never pass through here: entries fail closed, exits fail open (A§1).

The caller builds the state and runs the check under its book's lock, and journals the pending
order before releasing it, so two signals can't both pass on the same capacity (D-035).
"""

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from trader.config import RiskConfig, UniverseConfig
from trader.core.calendar import DayTimes
from trader.core.clock import fmt_iso
from trader.core.events import Event
from trader.core.models import Asset, OrderIntent, RiskCheck
from trader.execution.orders import MIN_STOP_GAP, valid_price
from trader.memory.journal import Journal
from trader.risk import caps

HUNDRED = Decimal(100)


class GateState(BaseModel):
    """Everything the rules read, captured at one instant for one book."""

    model_config = ConfigDict(frozen=True)

    mode: Literal["paper", "live"]
    live_gate_ok: bool = False  # the RISK.md §7 live gate; nothing sets it before Phase 7
    now: datetime
    day: DayTimes | None  # None: not a trading day
    halts: tuple[str, ...] = ()  # reasons of the book's active halts
    kill_switch: bool = False
    # Exposure, counted as D-035 says (smoke entries excluded from the entry counts).
    exposure_symbols: frozenset[str] = frozenset()  # a position, or an open or uncertain entry
    entries_today: int = 0  # entries that filled, are open, or are uncertain
    symbols_entered_today: frozenset[str] = frozenset()  # any accepted or uncertain entry
    # Money (ledger).
    available_cash: Decimal  # settled cash, open entries' unfilled part already reserved
    equity_sod: Decimal  # E
    realized_pnl_today: Decimal = Decimal(0)  # net of fee estimates
    open_risk: Decimal = Decimal(0)  # positions and unfilled entries, down to their stops
    ledger_equity: Decimal  # ledger cash before fee estimates, plus marked positions
    broker_equity: Decimal | None  # None: unknown, which fails R-019
    # The symbol.
    asset: Asset | None  # None: Alpaca doesn't know it
    symbol_halted: bool | None = None  # None: no status feed
    corporate_action_today: bool = False
    quote_age_s: float | None  # None: no trade or quote at all
    hub_stale: bool = False
    # Idempotency and protection.
    client_order_id_used: bool = False
    open_order_for_signal: bool = False
    unprotected_symbols: frozenset[str] = frozenset()
    events: tuple[Event, ...] = ()  # today's scheduled events


Rule = Callable[[OrderIntent, int, GateState], bool]


class RiskGate:
    def __init__(self, risk: RiskConfig, universe: UniverseConfig) -> None:
        self._risk, self._universe = risk, universe
        self._rules: tuple[tuple[str, Rule], ...] = (
            ("R-001", self._mode),
            ("R-002", self._long_only),
            ("R-003", self._asset),
            ("R-004", self._session),
            ("R-005", self._book_state),
            ("R-006", self._positions),
            ("R-007", self._entries),
            ("R-008", self._symbol_once),
            ("R-010", self._notional),
            ("R-011", self._risk_per_trade),
            ("R-012", self._stop_sanity),
            ("R-013", self._price),
            ("R-014", self._quantity),
            ("R-015", self._daily_loss_headroom),
            ("R-016", self._freshness),
            ("R-017", self._tradability),
            ("R-018", self._idempotency),
            ("R-019", self._account_sanity),
            ("R-020", self._no_naked_positions),
            ("R-021", self._event_blackout),
        )

    def check(self, intent: OrderIntent, state: GateState) -> RiskCheck:
        qty, cash_ok = self._settled_cash(intent, state)
        violations = [] if cash_ok else ["R-009"]
        violations += [rule_id for rule_id, rule in self._rules if not rule(intent, qty, state)]
        violations.sort()
        adjusted = qty if cash_ok and qty != intent.qty else None
        return RiskCheck(
            approved=not violations,
            violations=tuple(violations),
            adjusted_qty=adjusted,
            state_digest=_digest(intent, qty, state),
        )

    # ---------------------------------------------------------------- R-009 (adjusts qty)

    @staticmethod
    def _settled_cash(intent: OrderIntent, state: GateState) -> tuple[int, bool]:
        """R-009: the buy must fit the settled cash; if not, reduce the quantity to fit.

        Returns the quantity the other rules judge: the reduced one, or the requested one when
        not even one share fits (R-009 then rejects on its own).
        """
        limit, qty = intent.entry_limit, intent.qty
        if limit <= 0 or qty * limit <= state.available_cash:
            return qty, True
        cash = max(state.available_cash, Decimal(0))
        fitting = int((cash / limit).to_integral_value(ROUND_FLOOR))
        return (fitting, True) if fitting >= 1 else (qty, False)

    # ---------------------------------------------------------------- the other rules

    @staticmethod
    def _mode(intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-001: paper, or live only with the full live gate."""
        return state.mode == "paper" or state.live_gate_ok

    @staticmethod
    def _long_only(intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-002: the entry opens a long position."""
        return intent.side == "buy" or caps.ALLOW_SHORT

    def _asset(self, intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-003: a tradable, active us_equity on an allowed exchange, with a plain symbol."""
        asset = state.asset
        if asset is None or asset.symbol != intent.symbol:
            return False
        plain = not any(char in intent.symbol for char in self._universe.exclude_symbol_chars)
        return (
            asset.asset_class in caps.ALLOWED_ASSET_CLASSES
            and asset.tradable
            and asset.status == "active"
            and asset.exchange in self._universe.exchanges
            and plain
        )

    @staticmethod
    def _session(intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-004: inside the entry window of a trading day, before the flatten; no extended
        hours."""
        day, now = state.day, state.now
        if day is None:
            return False
        regular = day.open <= now < day.close or caps.ALLOW_EXTENDED_HOURS
        return regular and day.entry_start <= now <= day.last_entry and now < day.flatten

    @staticmethod
    def _book_state(intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-005: the book has no active halt and the kill switch is off."""
        return not state.halts and not state.kill_switch

    def _positions(self, intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-006: symbols held or being entered stay under the concurrency limit."""
        limit = min(self._risk.max_concurrent_positions, caps.MAX_CONCURRENT_POSITIONS)
        return len(state.exposure_symbols) < limit

    def _entries(self, intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-007: today's entries stay under the daily limit."""
        return state.entries_today < min(self._risk.max_entries_per_day, caps.MAX_ENTRIES_PER_DAY)

    @staticmethod
    def _symbol_once(intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-008: one entry per symbol per day per book."""
        return intent.symbol not in state.symbols_entered_today

    def _notional(self, intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-010: qty * limit <= max_position_pct * E * multiplier."""
        pct = min(self._risk.max_position_pct, caps.MAX_POSITION_PCT)
        cap = pct / HUNDRED * state.equity_sod * _multiplier(intent) * caps.LEVERAGE
        return qty * intent.entry_limit <= cap

    def _risk_per_trade(self, intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-011: qty * (limit - stop) <= max_risk_per_trade_pct * E * multiplier."""
        pct = min(self._risk.max_risk_per_trade_pct, caps.MAX_RISK_PER_TRADE_PCT)
        return _new_risk(intent, qty) <= pct / HUNDRED * state.equity_sod * _multiplier(intent)

    def _stop_sanity(self, intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-012: the stop sits a sane distance below the limit."""
        limit, stop = intent.entry_limit, intent.stop_price
        gap = limit - stop
        return (
            stop <= limit - MIN_STOP_GAP
            and gap >= self._risk.min_stop_distance
            and limit > 0
            and gap / limit * HUNDRED <= self._risk.max_stop_distance_pct
        )

    def _price(self, intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-013: min_price <= limit <= max_price, and both prices are valid ticks."""
        floor = max(self._risk.min_price, caps.MIN_PRICE_FLOOR)
        limit = intent.entry_limit
        in_range = floor <= limit <= self._risk.max_price
        return in_range and valid_price(limit) and valid_price(intent.stop_price)

    @staticmethod
    def _quantity(intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-014: a whole number of shares, at least one."""
        return qty >= 1

    def _daily_loss_headroom(self, intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-015: realized loss today + open risk + new risk <= daily_loss_limit_pct * E."""
        pct = min(self._risk.daily_loss_limit_pct, caps.MAX_DAILY_LOSS_LIMIT_PCT)
        realized_loss = max(Decimal(0), -state.realized_pnl_today)
        exposure = realized_loss + state.open_risk + _new_risk(intent, qty)
        return exposure <= pct / HUNDRED * state.equity_sod

    def _freshness(self, intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-016: the symbol's latest trade or quote is recent and the hub isn't stale."""
        age = state.quote_age_s
        fresh = age is not None and age <= self._risk.symbol_quote_max_age_s
        return fresh and not state.hub_stale

    @staticmethod
    def _tradability(intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-017: not halted (when a status feed says so), no corporate action today."""
        return state.symbol_halted is not True and not state.corporate_action_today

    @staticmethod
    def _idempotency(intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-018: a fresh client_order_id and no open order for this signal."""
        return not state.client_order_id_used and not state.open_order_for_signal

    def _account_sanity(self, intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-019: broker equity within the band of ledger equity; a live account under the
        live equity ceiling."""
        broker = state.broker_equity
        if broker is None:
            return False
        band = self._risk.account_equity_band_pct / HUNDRED * state.ledger_equity
        within = abs(broker - state.ledger_equity) <= band
        return within and (state.mode == "paper" or broker <= caps.LIVE_MAX_ACCOUNT_EQUITY)

    @staticmethod
    def _no_naked_positions(intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-020: every position has a working stop."""
        return not state.unprotected_symbols

    def _event_blackout(self, intent: OrderIntent, qty: int, state: GateState) -> bool:
        """R-021: outside the blackout around every high-impact event."""
        before = timedelta(minutes=self._risk.event_blackout_before_min)
        after = timedelta(minutes=self._risk.event_blackout_after_min)
        return not any(
            event.at() - before <= state.now <= event.at() + after
            for event in state.events
            if event.impact == "high"
        )


def _multiplier(intent: OrderIntent) -> Decimal:
    """The day's size multiplier can only shrink an entry (STRATEGY.md §5.1)."""
    return min(intent.day_size_multiplier, Decimal(1))


def _new_risk(intent: OrderIntent, qty: int) -> Decimal:
    return qty * (intent.entry_limit - intent.stop_price)


def _digest(intent: OrderIntent, qty: int, state: GateState) -> dict[str, Any]:
    """What the rules saw, for the ``risk_checks`` row."""
    return {
        "now": fmt_iso(state.now),
        "qty": intent.qty,
        "qty_after_cash": qty,
        "available_cash": str(state.available_cash),
        "equity_sod": str(state.equity_sod),
        "realized_pnl_today": str(state.realized_pnl_today),
        "open_risk": str(state.open_risk),
        "exposure_symbols": sorted(state.exposure_symbols),
        "entries_today": state.entries_today,
        "quote_age_s": state.quote_age_s,
        "hub_stale": state.hub_stale,
        "broker_equity": None if state.broker_equity is None else str(state.broker_equity),
        "ledger_equity": str(state.ledger_equity),
        "halts": list(state.halts),
        "unprotected": sorted(state.unprotected_symbols),
    }


# ---------------------------------------------------------------- journal


def insert_risk_check(
    conn: sqlite3.Connection, intent: OrderIntent, check: RiskCheck, at: datetime
) -> None:
    """Write the ``risk_checks`` row inside the caller's transaction."""
    conn.execute(
        "INSERT INTO risk_checks (signal_id, book, approved, violations_json, state_json, "
        "checked_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            intent.signal_id,
            intent.book,
            int(check.approved),
            json.dumps(list(check.violations)),
            json.dumps(check.state_digest, sort_keys=True),
            fmt_iso(at),
        ),
    )


async def record_risk_check(
    journal: Journal, intent: OrderIntent, check: RiskCheck, at: datetime
) -> None:
    await journal.write(lambda conn: insert_risk_check(conn, intent, check, at))
