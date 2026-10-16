"""Decision reason codes (A§4.4; DECISIONS D-040, D-042), shared by the books and the backtest."""

from collections.abc import Sequence
from typing import Literal

ReasonCode = Literal[
    "taken",
    "entered_after_wait",
    "capacity_positions",
    "capacity_entries",
    "insufficient_settled_cash",
    "qty_zero",
    "chased",
    "chased_after_wait",
    "missed_at_arm",
    "entry_unfilled",
    "vetoed",
    "vet_timeout",
    "llm_unavailable",
    "llm_degraded",
    "day_mode_no_trade",
    "book_halted",
    "broker_rejected",  # the broker refused an entry the gate approved (D-037)
    "restart_lost",  # a crash left a trigger undecided until after last entry
]

# Gate violations with their own A§4.4 code, in priority order; any other is risk_gate:<rule>.
_NAMED = (
    ("R-005", "book_halted"),
    ("R-006", "capacity_positions"),
    ("R-007", "capacity_entries"),
    ("R-009", "insufficient_settled_cash"),
)


def reason_from_violations(violations: Sequence[str]) -> str:
    """One reason code for a gate rejection; the decision's detail lists every violation."""
    for rule, code in _NAMED:
        if rule in violations:
            return code
    return f"risk_gate:{sorted(violations)[0]}" if violations else "risk_gate:unknown"


def reason_for_no_size(capacity: Sequence[str], *, by_cash: int) -> str:
    """Why sizing gave no shares, decided before the gate. A capacity rule that also binds
    (R-006, R-007) names it, ranked as the gate ranks them (D-044); else the settled cash;
    else the size rules. So the reason never depends on the price (D-054 #5, D-056)."""
    if capacity:
        return reason_from_violations(capacity)
    return "insufficient_settled_cash" if by_cash < 1 else "qty_zero"
