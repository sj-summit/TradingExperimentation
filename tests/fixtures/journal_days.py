"""A journal of several sessions, written row by row (Phase 5: stats, lessons, the weekly report
and the evaluation memo read many days, and every other fixture holds one).

Each helper writes the rows the live engine would for one thing: a session, a long signal with
its shadow outcome and Book B's verdict, a trade, a book's day, SPY's close. Values are chosen by
the test, so the expected numbers can be worked out by hand.
"""

import json
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from trader.core.clock import ET, fmt_iso
from trader.memory.journal import open_database

D = Decimal
OPEN, CLOSE = time(9, 30), time(16, 0)


def journal(path: Path) -> sqlite3.Connection:
    conn = open_database(path)
    conn.row_factory = sqlite3.Row
    return conn


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), ET)


def sessions(first: date, count: int) -> list[date]:
    """``count`` weekdays from ``first`` (holidays aren't modeled)."""
    out, day = [], first
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def add_session(conn: sqlite3.Connection, day: date, close: time = CLOSE) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO trading_days (date, open_ts, close_ts, is_early_close) "
        "VALUES (?, ?, ?, ?)",
        (day.isoformat(), fmt_iso(at(day, OPEN.isoformat())), fmt_iso(at(day, close.isoformat())),
         int(close < CLOSE)),
    )  # fmt: skip


def add_long(
    conn: sqlite3.Connection,
    day: date,
    symbol: str,
    *,
    shadow_r: float | None,
    verdict: str | None = None,
    source: str = "vet",
    rank: int = 1,
    epoch: int = 0,
    live_trigger: bool = True,
    features: Mapping[str, Any] | None = None,
    catalyst: str = "earnings",
    flags: tuple[str, ...] = (),
    confidence: float = 0.6,
    gap_pct: float = 3.0,
    rel_vol: float = 5.0,
    trigger_minute: str = "09:41",
) -> str:
    """A top-20 long: its signal, its shadow outcome (``shadow_r`` None: it never triggered),
    and Book B's verdict on it, if any, from a trading call of ``source``'s role."""
    signal_id = f"{day:%Y%m%d}-{symbol}-orb5"
    triggered = shadow_r is not None
    feats = {"or_range_atr": 0.4, "or_return_pct": 1.2, "gap_pct": gap_pct, "atr_pct": 4.0,
             "regime_spy_trend": "up", "regime_vol_bucket": "mid",
             "day_of_week": day.strftime("%a"), "event_day": False, **(features or {})}  # fmt: skip
    conn.execute(
        "INSERT INTO signals (id, date, symbol, strategy, direction, status, rel_vol, "
        "rel_vol_rank, or_open, or_high, or_low, or_close, or_volume, atr14, adv14, prev_close, "
        "gap_pct, trigger_price, stop_price, armed_at, triggered_at, features_json, eval_epoch) "
        "VALUES (?, ?, ?, 'orb5_sip_long', 'long', ?, ?, ?, '20.00', '20.40', '19.90', '20.30', "
        "50000, '1.00', 2000000, '19.40', ?, '20.41', '20.31', ?, ?, ?, ?)",
        (signal_id, day.isoformat(), symbol,
         "triggered" if triggered and live_trigger else "expired", rel_vol, rank, gap_pct,
         fmt_iso(at(day, "09:35:03")),
         fmt_iso(at(day, trigger_minute)) if triggered and live_trigger else None,
         json.dumps(feats, sort_keys=True), epoch),
    )  # fmt: skip
    conn.execute(
        "INSERT INTO shadow_outcomes (signal_id, entry_price, exit_price, exit_reason, shadow_r, "
        "trigger_ts, sip_only_trigger, trigger_resolution) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (signal_id, "20.42" if triggered else None, None,
         ("flatten" if (shadow_r or 0) > 0 else "stop") if triggered else "no_trigger",
         shadow_r, fmt_iso(at(day, trigger_minute)) if triggered else None,
         int(triggered and not live_trigger), "bars" if triggered else None),
    )  # fmt: skip
    if verdict is not None:
        call = f"{day:%Y%m%d}-{source}-01-1"
        conn.execute(
            "INSERT OR IGNORE INTO llm_calls (id, date, role, logical_id, attempt, final, ok, "
            "purpose, started_at, eval_epoch) VALUES (?, ?, ?, ?, 1, 1, 1, 'trading', ?, ?)",
            (call, day.isoformat(), source, call[:-2],
             fmt_iso(at(day, "09:05" if source == "premarket" else "09:35:06")), epoch),
        )  # fmt: skip
        conn.execute(
            "INSERT INTO verdicts (date, symbol, source, verdict, confidence, catalyst_type, "
            "catalyst_fresh, catalyst_confirmed, risk_flags_json, reason, llm_call_id, "
            "signal_id, eval_epoch) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, 'r', ?, ?, ?)",
            (day.isoformat(), symbol, source, verdict, confidence, catalyst,
             json.dumps(list(flags)), call, signal_id if source == "vet" else None, epoch),
        )  # fmt: skip
    return signal_id


def add_trade(
    conn: sqlite3.Connection,
    day: date,
    book: str,
    symbol: str,
    *,
    r: float | None,
    pnl: str | None,
    fees: str = "0.02",
    entry: str = "09:41:05",
    exit_: str | None = "10:41:05",
    slippage: str = "0.02",
    delay_ms: int = 1500,
    epoch: int = 0,
) -> None:
    signal_id = f"{day:%Y%m%d}-{symbol}-orb5"
    conn.execute(
        "INSERT INTO trades (id, book, signal_id, symbol, qty, entry_ts, entry_price, stop_price, "
        "exit_ts, exit_price, exit_reason, pnl, r_multiple, fees_est, entry_slippage, "
        "entry_delay_ms, eval_epoch) VALUES (?, ?, ?, ?, 10, ?, '20.43', '20.31', ?, ?, ?, ?, ?, "
        "?, ?, ?, ?)",
        (f"{book}-{signal_id}", book, signal_id, symbol, fmt_iso(at(day, entry)),
         fmt_iso(at(day, exit_)) if exit_ else None, None, "stop" if (r or 0) < 0 else "flatten",
         pnl, r, fees, slippage, delay_ms, epoch),
    )  # fmt: skip


def add_book_day(
    conn: sqlite3.Connection,
    day: date,
    book: str,
    *,
    start: str,
    end: str,
    epoch: int = 0,
    trades: int = 0,
) -> None:
    pnl = D(end) - D(start)
    conn.execute(
        "INSERT INTO daily_stats (date, book, start_equity, end_equity, pnl, n_signals, "
        "n_triggered, n_trades, regime_json, eval_epoch, cash_residual) "
        "VALUES (?, ?, ?, ?, ?, 0, 0, ?, '{}', ?, '0.00')",
        (day.isoformat(), book, start, end, str(pnl), trades, epoch),
    )


def add_spy(conn: sqlite3.Connection, day: date, close: str) -> None:
    """A whole ``regime_daily`` row, as the boot job stores one: its close is what matters."""
    conn.execute(
        "INSERT OR REPLACE INTO regime_daily (date, spy_close, spy_sma20, spy_trend, spy_atr_pct, "
        "tercile_low, tercile_high, vol_bucket, computed_at) "
        "VALUES (?, ?, ?, 'up', 1.0, 0.8, 1.2, 'mid', ?)",
        (day.isoformat(), close, close, fmt_iso(at(day, "16:30"))),
    )


def decide(
    conn: sqlite3.Connection, day: date, symbol: str, book: str, reason: str, call: str | None
) -> None:
    conn.execute(
        "INSERT INTO decisions (signal_id, book, take, reason_code, llm_call_id, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (f"{day:%Y%m%d}-{symbol}-orb5", book, int(reason in ("taken", "entered_after_wait")),
         reason, call, fmt_iso(at(day, "09:41"))),
    )  # fmt: skip
