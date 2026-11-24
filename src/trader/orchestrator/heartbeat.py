"""Reading ``runtime/heartbeat.json`` from another process: the scheduled wrapper and the
watchdog (RISK.md §6; the Phase 6 plan's decision 12; DECISIONS D-080).

The engine replaces the file atomically every 10 s (``tasks.Heartbeat``), but on Windows a reader
that opens it at that instant can fail, and a beat can be skipped. So a read is tried three
times, 200 ms apart, and "absent" is told apart from "unreadable". A ``ts`` up to 5 s ahead of
the reader's clock counts as fresh (age 0); one further ahead is unreadable, as after a clock
step.
"""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from trader.core.clock import parse_et_iso

HEARTBEAT_NAME = "heartbeat.json"  # beside the journal (live.folders)
STALE_AFTER_S = 60.0  # RISK.md §6
FUTURE_TOLERANCE_S = 5.0
READ_ATTEMPTS = 3
READ_PAUSE_S = 0.2

BeatStatus = Literal["fresh", "stale", "absent", "unreadable"]


@dataclass(frozen=True)
class Beat:
    status: BeatStatus
    age_s: float | None = None
    ts: datetime | None = None
    state: str | None = None
    pid: int | None = None
    books: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def fresh(self) -> bool:
        return self.status == "fresh"


def heartbeat_path(db_path: Path) -> Path:
    """Where the engine of the journal at ``db_path`` writes its heartbeat."""
    return db_path.parent / HEARTBEAT_NAME


def read_heartbeat(
    path: Path,
    now: datetime,
    *,
    pause: Callable[[float], None] = time.sleep,
    attempts: int = READ_ATTEMPTS,
    stale_after_s: float = STALE_AFTER_S,
) -> Beat:
    """The heartbeat as of ``now``: fresh, stale, absent or unreadable."""
    for attempt in range(attempts):
        if not path.exists():
            return Beat("absent")  # the engine's replace is atomic: the name never disappears
        try:
            return _parse(json.loads(path.read_text(encoding="utf-8")), now, stale_after_s)
        except (OSError, ValueError, KeyError, TypeError):
            if attempt + 1 < attempts:
                pause(READ_PAUSE_S)
    return Beat("unreadable")


def _parse(data: Any, now: datetime, stale_after_s: float) -> Beat:
    ts = parse_et_iso(str(data["ts"]))
    age = (now - ts).total_seconds()
    if age < -FUTURE_TOLERANCE_S:
        return Beat("unreadable", age_s=age, ts=ts)
    age = max(age, 0.0)
    pid = data.get("pid")
    books = data.get("books")
    return Beat(
        "fresh" if age <= stale_after_s else "stale",
        age_s=age,
        ts=ts,
        state=str(data.get("state")) if data.get("state") is not None else None,
        pid=pid if isinstance(pid, int) else None,
        books=books if isinstance(books, dict) else {},
    )
