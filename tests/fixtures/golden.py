"""The golden synthetic day (P3.8b; DECISIONS D-049): one run of the real engine, journaled.

``standard_day`` plus a partial fill on WIN's entry gives every case the plan asks for:
- a winner held to the flatten (WIN, 10 of 24 shares, protected by its own stop after the
  partial) and a stop-out (LOSE);
- ``chased`` (GAPPER), ``missed_at_arm`` (CHASE) and ``expired`` (EXP);
- a capacity rejection (CAP: R-007, the day's two entries are used);
- a partial fill;
- a shadow short (SHORT) and a doji (DOJI).

``snapshot`` keeps natural keys only (no surrogate ids, no raw JSON), so two runs compare equal
exactly when they behaved the same. ``python -m tests.fixtures.golden <folder>`` writes it to
``<folder>/snapshot.json``, which is how the test compares separate processes.
``UPDATE_GOLDEN=1`` rewrites the golden file.
"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from tests.fixtures.dayrun import DayRun
from tests.fixtures.days import standard_day

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "standard_day.json"

TABLES = {
    "signals": "SELECT id, symbol, direction, status, rel_vol_rank, rel_vol, or_open, or_high, "
    "or_low, or_close, or_volume, trigger_price, stop_price, entry_limit, max_chase_price, "
    "armed_at, triggered_at, features_json FROM signals ORDER BY id",
    "decisions": "SELECT signal_id, book, take, reason_code, detail, decided_at FROM decisions "
    "ORDER BY signal_id, book, decided_at, reason_code",
    "risk_checks": "SELECT signal_id, book, approved, violations_json, checked_at FROM "
    "risk_checks ORDER BY checked_at, signal_id, book",
    "orders": "SELECT client_order_id, book, signal_id, symbol, side, type, order_class, qty, "
    "limit_price, stop_price, status, purpose, submitted_at FROM orders "
    "ORDER BY submitted_at, client_order_id",
    "fills": "SELECT symbol, side, qty, price, filled_at FROM fills "
    "ORDER BY filled_at, symbol, side",
    "trades": "SELECT * FROM trades ORDER BY id",
    "ledger": "SELECT book, kind, symbol, amount, trade_date, settle_date FROM ledger_entries "
    "ORDER BY trade_date, kind, symbol, amount",
    "daily_stats": "SELECT * FROM daily_stats ORDER BY date, book",
    "shadow": "SELECT * FROM shadow_outcomes ORDER BY signal_id",
    "incidents": "SELECT ts, severity, component, code, message FROM incidents ORDER BY ts, code",
    "day_runs": "SELECT kind, name, at FROM day_runs ORDER BY at, kind, name",
}


def snapshot(conn: sqlite3.Connection) -> dict[str, list[list[Any]]]:
    return {name: [list(row) for row in conn.execute(sql)] for name, sql in TABLES.items()}


async def golden_run(root: Path, *, recording: bool = False) -> DayRun:
    """The golden day, run to the end; the caller closes it."""
    run = await DayRun(root, standard_day(), recording=recording).open()
    run.at("09:35:49", lambda: run.sims["A"].partial_next(10))  # WIN's entry fills 10 of 24
    await run.run()
    return run


async def golden_snapshot(root: Path) -> dict[str, list[list[Any]]]:
    run = await golden_run(root)
    try:
        return await run.journal.read(snapshot)
    finally:
        await run.close()


def dumps(snap: dict[str, list[list[Any]]]) -> str:
    return json.dumps(snap, indent=1, sort_keys=True, default=str)


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    snap = asyncio.run(golden_snapshot(folder))
    (folder / "snapshot.json").write_text(dumps(snap), encoding="utf-8")
