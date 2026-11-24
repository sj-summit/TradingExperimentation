"""Reconcile's view of orders in flight (D-083): sent today and not yet confirmed. A lost order
never reached the broker, and an older unconfirmed one is long settled there; skipping either's
symbol would blind reconcile to it for good."""

from pathlib import Path

from trader.execution.reconcile import journal_view
from trader.memory.journal import open_database

TODAY = "2026-10-09T00:00:00-04:00"


def test_in_flight_is_todays_unconfirmed_orders(tmp_path: Path) -> None:
    conn = open_database(tmp_path / "trader.db")
    rows = [
        ("A-1", "LOST", "lost", "2026-10-09T10:00:00-04:00"),
        ("A-2", "OLD", "submitting", "2026-10-08T10:00:00-04:00"),
        ("A-3", "NOW", "submitting", "2026-10-09T10:01:00-04:00"),
        ("A-4", "UNC", "uncertain", "2026-10-09T10:02:00-04:00"),
        ("A-5", "DONE", "filled", "2026-10-09T10:03:00-04:00"),
    ]
    conn.executemany(
        "INSERT INTO orders (client_order_id, book, symbol, side, type, qty, status, submitted_at)"
        " VALUES (?, 'A', ?, 'sell', 'market', 1, ?, ?)",
        rows,
    )
    conn.commit()
    assert journal_view(conn, "A", TODAY).in_flight == {"NOW", "UNC"}
    assert journal_view(conn, "A").in_flight == {"OLD", "NOW", "UNC"}  # unbounded: never lost
    assert journal_view(conn, "B", TODAY).in_flight == frozenset()
    conn.close()
