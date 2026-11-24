"""The epoch record (STRATEGY.md §9; the Phase 5 plan's decision 9; DECISIONS D-075).

``meta.eval_epoch`` is the epoch every row written today is stamped with. ``epochs`` keeps one
row per epoch: when it began, why, the DECISIONS entry behind it, the review note that led to
it (the human improvement loop: a note accepted, a change made, an epoch begun), and the hashes
of the setup it froze: the settings, risk and events files and the LLM fingerprint.

Only the human starts an epoch (``trader epoch start``), as only the human closes a review's
follow-ups. ``start_epoch`` writes the row and bumps ``meta.eval_epoch`` in one transaction, and
only to the next number: epochs are never skipped or reused.
"""

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from trader.core.clock import fmt_iso, parse_et_iso

DECISION = re.compile(r"D-\d{3,}")
HASHED = ("settings", "risk", "events")


class EpochError(ValueError):
    """The start can't be recorded: a wrong number, a bad DECISIONS id, an unknown note."""


@dataclass(frozen=True)
class EpochRow:
    epoch: int
    started_at: str | None
    reason: str
    decision: str | None
    note_id: int | None
    settings_sha256: str | None
    risk_sha256: str | None
    events_sha256: str | None
    llm_fingerprint: str | None


def current_epoch(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'eval_epoch'").fetchone()
    return int(row[0]) if row is not None else 0


def current_epoch_start(conn: sqlite3.Connection) -> datetime | None:
    """When the current epoch began: None for epoch 0, the build, which has no start (and for an
    epoch with no row, which can't happen: retention then keeps everything, D-084)."""
    row = conn.execute(
        "SELECT started_at FROM epochs WHERE epoch = ?", (current_epoch(conn),)
    ).fetchone()
    return None if row is None or row[0] is None else parse_et_iso(str(row[0]))


def epochs(conn: sqlite3.Connection) -> list[EpochRow]:
    return [
        EpochRow(
            epoch=r["epoch"],
            started_at=r["started_at"],
            reason=r["reason"],
            decision=r["decision"],
            note_id=r["note_id"],
            settings_sha256=r["settings_sha256"],
            risk_sha256=r["risk_sha256"],
            events_sha256=r["events_sha256"],
            llm_fingerprint=r["llm_fingerprint"],
        )
        for r in conn.execute("SELECT * FROM epochs ORDER BY epoch")
    ]


def start_epoch(
    conn: sqlite3.Connection,
    *,
    epoch: int,
    at: datetime,
    reason: str,
    decision: str,
    note_id: int | None,
    hashes: Mapping[str, str | None],
) -> EpochRow:
    """Record epoch ``epoch``, the next one, and make it current. Call it inside a transaction
    (``Journal.write``)."""
    now = current_epoch(conn)
    if epoch != now + 1:
        raise EpochError(f"the epoch now is {now}: the next is {now + 1}, not {epoch}")
    if not DECISION.fullmatch(decision):
        raise EpochError(f"{decision!r} is not a DECISIONS id like D-080")
    if not reason.strip():
        raise EpochError("say why the epoch begins (--reason)")
    if note_id is not None and (
        conn.execute("SELECT 1 FROM review_notes WHERE id = ?", (note_id,)).fetchone() is None
    ):
        raise EpochError(f"there is no review note {note_id}")
    fingerprint = conn.execute("SELECT value FROM meta WHERE key = 'llm_fingerprint'").fetchone()
    row = EpochRow(
        epoch=epoch,
        started_at=fmt_iso(at),
        reason=reason.strip(),
        decision=decision,
        note_id=note_id,
        settings_sha256=hashes.get("settings"),
        risk_sha256=hashes.get("risk"),
        events_sha256=hashes.get("events"),
        llm_fingerprint=None if fingerprint is None else str(fingerprint[0]),
    )
    conn.execute(
        "INSERT INTO epochs (epoch, started_at, reason, decision, note_id, settings_sha256, "
        "risk_sha256, events_sha256, llm_fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row.epoch, row.started_at, row.reason, row.decision, row.note_id,
         row.settings_sha256, row.risk_sha256, row.events_sha256, row.llm_fingerprint),
    )  # fmt: skip
    conn.execute("UPDATE meta SET value = ? WHERE key = 'eval_epoch'", (str(epoch),))
    return row
