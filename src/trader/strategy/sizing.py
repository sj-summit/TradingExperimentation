"""Position sizing (S§3.5, A§4.3): pure.

    risk_budget  = max_risk_per_trade_pct x E x day_size_multiplier
    notional_cap = max_position_pct x E x day_size_multiplier
    qty = floor(min(risk_budget / (limit - stop), notional_cap / limit, S / limit))

Percentages are given as in risk.yaml (1.0 means 1%). The gate re-checks every cap under the
book's lock (R-009..R-011); this only proposes the quantity.
"""

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Literal

HUNDRED = Decimal(100)
ZERO = Decimal(0)

SizeTerm = Literal["cash", "notional", "risk"]


@dataclass(frozen=True)
class Sizing:
    qty: int
    binding: SizeTerm | None  # None when nothing can be sized (stop at or above the limit)
    by_risk: int
    by_notional: int
    by_cash: int


def sizing(
    limit: Decimal,
    stop: Decimal,
    equity: Decimal,
    settled_available: Decimal,
    risk_pct: Decimal,
    notional_pct: Decimal,
    multiplier: Decimal,
) -> Sizing:
    """The size and the term that set it. On a tie, cash names the reason before notional and
    risk, so a book out of settled cash reports ``insufficient_settled_cash``."""
    if limit <= ZERO or stop >= limit:
        return Sizing(qty=0, binding=None, by_risk=0, by_notional=0, by_cash=0)
    scale = max(multiplier, ZERO)
    by_risk = _floor(risk_pct / HUNDRED * equity * scale / (limit - stop))
    by_notional = _floor(notional_pct / HUNDRED * equity * scale / limit)
    by_cash = _floor(settled_available / limit)
    qty = min(by_risk, by_notional, by_cash)
    binding: SizeTerm = "cash" if by_cash == qty else "notional" if by_notional == qty else "risk"
    return Sizing(qty=qty, binding=binding, by_risk=by_risk, by_notional=by_notional,
                  by_cash=by_cash)  # fmt: skip


def size_position(
    limit: Decimal,
    stop: Decimal,
    equity: Decimal,
    settled_available: Decimal,
    risk_pct: Decimal,
    notional_pct: Decimal,
    multiplier: Decimal,
) -> int:
    """A§4.3's signature: the whole-share quantity, never negative."""
    return sizing(limit, stop, equity, settled_available, risk_pct, notional_pct, multiplier).qty


def binding_term(
    limit: Decimal,
    stop: Decimal,
    equity: Decimal,
    settled_available: Decimal,
    risk_pct: Decimal,
    notional_pct: Decimal,
    multiplier: Decimal,
) -> SizeTerm | None:
    """Which term set the size: it picks the reason code when the size is 0."""
    return sizing(
        limit, stop, equity, settled_available, risk_pct, notional_pct, multiplier
    ).binding


def _floor(value: Decimal) -> int:
    return max(int(value.to_integral_value(rounding=ROUND_FLOOR)), 0)
