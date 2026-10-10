"""Incidents (RISK.md §11): journaled and logged. Push alerts arrive with notify.py (P3.7).

Severities: P1 = immediate alert, flatten and halt the affected book until a human acks;
P2 = alert, halt new entries as the rule requires; P3 = log and show in the daily report.
"""

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

import structlog

from trader.core.clock import fmt_iso
from trader.memory.journal import Journal

log = structlog.get_logger(__name__)

Severity = Literal["P1", "P2", "P3"]


def insert_incident(
    conn: sqlite3.Connection,
    *,
    at: datetime,
    severity: Severity,
    component: str,
    code: str,
    message: str,
    detail: Mapping[str, Any] | None = None,
) -> int:
    """Write the row inside the caller's transaction; returns its id."""
    cursor = conn.execute(
        "INSERT INTO incidents (ts, severity, component, code, message, detail_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            fmt_iso(at),
            severity,
            component,
            code,
            message,
            None if detail is None else json.dumps(detail, sort_keys=True, default=str),
        ),
    )
    return cursor.lastrowid or 0


async def record_incident(
    journal: Journal,
    *,
    at: datetime,
    severity: Severity,
    component: str,
    code: str,
    message: str,
    detail: Mapping[str, Any] | None = None,
) -> int:
    emit = {"P1": log.error, "P2": log.warning, "P3": log.info}[severity]
    emit("incident", severity=severity, component=component, code=code, message=message)
    return await journal.write(
        lambda conn: insert_incident(
            conn,
            at=at,
            severity=severity,
            component=component,
            code=code,
            message=message,
            detail=detail,
        )
    )
