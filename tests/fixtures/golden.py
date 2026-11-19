"""The golden synthetic days (P3.8b, P4.9; DECISIONS D-049, D-070): runs of the real engine,
journaled.

``standard_day`` plus a partial fill on WIN's entry gives every case the plan asks for:
- a winner held to the flatten (WIN, 10 of 24 shares, protected by its own stop after the
  partial) and a stop-out (LOSE);
- ``chased`` (GAPPER), ``missed_at_arm`` (CHASE) and ``expired`` (EXP);
- a capacity rejection (CAP: R-007, the day's two entries are used);
- a partial fill;
- a shadow short (SHORT) and a doji (DOJI).

The A+B golden day is the same day, with Book B beside Book A from 08:55:
- the 09:00 scan picks GAPPER, LOSE and SHORT; the 09:05 brief vetoes LOSE, and flags GAPPER,
  whose news tried an injection;
- the vet at arming judges WIN, EXP and CAP, slowly: WIN, triggered at 09:35:50, waits for it
  (``entered_after_wait``);
- with LOSE vetoed, Book B still has an entry and the cash for CAP, which Book A can't take.

``snapshot`` keeps natural keys only (no surrogate ids, no raw JSON), so two runs compare equal
exactly when they behaved the same. ``python -m tests.fixtures.golden <folder>`` writes both
days' snapshots to ``<folder>/snapshot.json`` and ``<folder>/snapshot_ab.json``, which is how
the test compares separate processes. ``UPDATE_GOLDEN=1`` rewrites the golden files.
"""

import asyncio
import json
import sqlite3
import sys
from datetime import time
from pathlib import Path
from typing import Any

from tests.fixtures.dayrun import DayRun
from tests.fixtures.days import standard_day

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "standard_day.json"
GOLDEN_AB = Path(__file__).resolve().parents[1] / "golden" / "standard_day_ab.json"

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
    # its columns before P4.10's spy_move_pct, by name, so Book A's golden days stay the same
    "shadow": "SELECT signal_id, entry_price, exit_price, exit_reason, shadow_r, mfe_r, mae_r, "
    "minutes_held, path_source, computed_at, trigger_ts, sip_only_trigger, trigger_resolution "
    "FROM shadow_outcomes ORDER BY signal_id",
    "incidents": "SELECT ts, severity, component, code, message FROM incidents ORDER BY ts, code",
    "day_runs": "SELECT kind, name, at FROM day_runs ORDER BY at, kind, name",
}


# Book B's part too: each decision's call, the calls (their packs by ``pack_id``, which hashes
# the pack's bytes), the verdicts, the day mode and the halts; fills by book.
TABLES_AB = {
    **TABLES,
    "decisions": "SELECT signal_id, book, take, reason_code, detail, llm_call_id, decided_at "
    "FROM decisions ORDER BY signal_id, book, decided_at, reason_code",
    "fills": "SELECT book, symbol, side, qty, price, filled_at FROM fills "
    "ORDER BY filled_at, book, symbol, side",
    "llm_calls": "SELECT id, role, logical_id, attempt, final, ok, failure, timed_out, "
    "started_at, ended_at, deadline, purpose, pack_id, candidates_json, clamped FROM llm_calls "
    "ORDER BY id",
    "verdicts": "SELECT date, symbol, source, verdict, confidence, catalyst_type, "
    "catalyst_fresh, catalyst_confirmed, risk_flags_json, reason, signal_id, llm_call_id "
    "FROM verdicts ORDER BY llm_call_id, symbol",
    "day_modes": "SELECT date, book, mode, reason, source, multiplier, llm_call_id "
    "FROM day_modes ORDER BY date, book",
    "halts": "SELECT book, reason, scope, detail, created_at, expires_at, cleared_at FROM halts "
    "ORDER BY created_at, book, reason",
    # P4.10: SPY's move with each shadow outcome, and the review with its parts
    "shadow": "SELECT * FROM shadow_outcomes ORDER BY signal_id",
    "daily_reviews": "SELECT * FROM daily_reviews ORDER BY date",
    "signal_reviews": "SELECT * FROM signal_reviews ORDER BY signal_id",
    "process_issues": "SELECT * FROM process_issues ORDER BY id",
    "review_notes": "SELECT * FROM review_notes ORDER BY id",
    "review_mentions": "SELECT date, kind, item_id, severity, component, text, llm_call_id "
    "FROM review_mentions ORDER BY id",
    "lessons": "SELECT * FROM lessons ORDER BY id",
}


def snapshot(
    conn: sqlite3.Connection, tables: dict[str, str] = TABLES
) -> dict[str, list[list[Any]]]:
    return {name: [list(row) for row in conn.execute(sql)] for name, sql in tables.items()}


def snapshot_ab(conn: sqlite3.Connection) -> dict[str, list[list[Any]]]:
    return snapshot(conn, TABLES_AB)


async def golden_run(root: Path, *, recording: bool = False) -> DayRun:
    """The golden day, run to the end; the caller closes it."""
    run = await DayRun(root, standard_day(), recording=recording).open()
    run.at("09:35:49", lambda: run.sims["A"].partial_next(10))  # WIN's entry fills 10 of 24
    await run.run()
    return run


async def golden_ab_run(root: Path, *, recording: bool = False) -> DayRun:
    """The A+B golden day (the module's docstring), run to the end; the caller closes it."""
    run = await DayRun(root, standard_day(), start=time(8, 55), books=("A", "B"),
                       recording=recording).open()  # fmt: skip
    run.at("09:35:49", lambda: run.sims["A"].partial_next(10))  # as in Book A's golden day
    run.verdicts = {"LOSE": "veto"}  # the brief's
    run.llm_delay["vet"] = 55.0  # the vet answers at 09:35:58: WIN waits for it
    await run.run()
    return run


async def golden_snapshot(root: Path) -> dict[str, list[list[Any]]]:
    run = await golden_run(root)
    try:
        return await run.journal.read(snapshot)
    finally:
        await run.close()


async def golden_ab_snapshot(root: Path) -> dict[str, list[list[Any]]]:
    run = await golden_ab_run(root)
    try:
        return await run.journal.read(snapshot_ab)
    finally:
        await run.close()


def dumps(snap: dict[str, list[list[Any]]]) -> str:
    return json.dumps(snap, indent=1, sort_keys=True, default=str)


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    for name, make in (("snapshot", golden_snapshot), ("snapshot_ab", golden_ab_snapshot)):
        (folder / name).mkdir()
        snap = asyncio.run(make(folder / name))
        (folder / f"{name}.json").write_text(dumps(snap), encoding="utf-8")
