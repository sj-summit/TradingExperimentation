"""The golden days, determinism across processes, and record-then-replay (P3.8b, P4.9; D-049,
D-070)."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fixtures import g3_day, g4b_day
from tests.fixtures.dayrun import DayRun
from tests.fixtures.days import DAY, standard_day
from tests.fixtures.exec_harness import RISK, SETTINGS
from tests.fixtures.golden import (
    GOLDEN,
    GOLDEN_AB,
    TABLES,
    dumps,
    golden_ab_run,
    golden_ab_snapshot,
    golden_run,
    golden_snapshot,
    snapshot,
    snapshot_ab,
)
from tests.helpers import REPO
from trader.cli import app
from trader.core.clock import ET, SimClock, fmt_iso
from trader.core.models import OrderIntent
from trader.execution.broker import OrderNotCancelable, OrderRejected
from trader.execution.replay import ReplayBroker
from trader.execution.sim_broker import SimBroker
from trader.memory.stats import Bootstrap
from trader.orchestrator.capabilities import CURRENT
from trader.orchestrator.replay import comparable, diff, replay_day
from trader.orchestrator.simdrive import drive
from trader.reports.evaluation import build_memo, counts
from trader.reports.evaluation import render_html as memo_html
from trader.reports.weekly import build_weekly
from trader.reports.weekly import render_html as weekly_html
from trader.reports.weekly import render_markdown as weekly_md
from trader.risk.ledger import LedgerEntry, insert_entries

D = Decimal


async def test_the_golden_day(tmp_path: Path) -> None:
    got = dumps(await golden_snapshot(tmp_path))
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(got + "\n", encoding="utf-8")
    assert got + "\n" == GOLDEN.read_text(encoding="utf-8"), "run UPDATE_GOLDEN=1 if intended"


async def test_the_ab_golden_day(tmp_path: Path) -> None:
    """P4.9: Book A and Book B on the same day (``golden.py``'s docstring)."""
    got = dumps(await golden_ab_snapshot(tmp_path))
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN_AB.write_text(got + "\n", encoding="utf-8")
    assert got + "\n" == GOLDEN_AB.read_text(encoding="utf-8"), "run UPDATE_GOLDEN=1 if intended"


@pytest.mark.timeout(300)
def test_two_processes_with_different_hash_seeds_agree(tmp_path: Path) -> None:
    outputs = []
    for seed in ("1", "2"):
        folder = tmp_path / f"seed-{seed}"
        folder.mkdir()
        env = {**os.environ, "PYTHONHASHSEED": seed}
        subprocess.run(
            [sys.executable, "-m", "tests.fixtures.golden", str(folder)],
            cwd=REPO,
            env=env,
            check=True,
            capture_output=True,
            timeout=300,
        )
        outputs.append(tuple((folder / f"{name}.json").read_text(encoding="utf-8")
                             for name in ("snapshot", "snapshot_ab")))  # fmt: skip
    assert outputs[0] == outputs[1]
    assert outputs[0][0] + "\n" == GOLDEN.read_text(encoding="utf-8")
    assert outputs[0][1] + "\n" == GOLDEN_AB.read_text(encoding="utf-8")


# Book A's own rows, and the rows both books share
BOOK_A_ROWS = {
    "signals": TABLES["signals"],
    "shadow": TABLES["shadow"],
    "decisions": "SELECT signal_id, take, reason_code, detail, llm_call_id, decided_at "
    "FROM decisions WHERE book = 'A' ORDER BY signal_id, decided_at, reason_code",
    "risk_checks": "SELECT signal_id, approved, violations_json, state_json, checked_at "
    "FROM risk_checks WHERE book = 'A' ORDER BY checked_at, signal_id",
    "orders": "SELECT client_order_id, broker_order_id, signal_id, symbol, side, type, "
    "order_class, qty, limit_price, stop_price, status, purpose, submitted_at, updated_at "
    "FROM orders WHERE book = 'A' ORDER BY submitted_at, client_order_id",
    "fills": "SELECT id, symbol, side, qty, price, filled_at FROM fills WHERE book = 'A' "
    "ORDER BY filled_at, id",
    "trades": "SELECT * FROM trades WHERE book = 'A' ORDER BY id",
    "ledger": "SELECT kind, symbol, amount, trade_date, settle_date, ref FROM ledger_entries "
    "WHERE book = 'A' ORDER BY ref, kind",
    "daily_stats": "SELECT * FROM daily_stats WHERE book = 'A'",
}


async def test_book_a_trades_the_same_beside_book_b(tmp_path: Path) -> None:
    """S§5.4: Book A never depends on the LLM. Its every row on the A+B golden day is the row
    its own golden day writes, though Book B boots first, waits, vetoes and trades beside it."""
    alone = await golden_run(tmp_path / "a")
    beside = await golden_ab_run(tmp_path / "ab")
    try:
        got = await beside.journal.read(lambda conn: snapshot(conn, BOOK_A_ROWS))
        want = await alone.journal.read(lambda conn: snapshot(conn, BOOK_A_ROWS))
    finally:
        await alone.close()
        await beside.close()
    assert all(want[name] for name in ("decisions", "orders", "fills", "trades", "ledger"))
    assert got == want


@pytest.mark.timeout(300)  # the day, then its replay: about 40 s
async def test_a_recorded_ab_golden_day_replays_the_same(tmp_path: Path) -> None:
    """Decision 10: the replay serves the recorded packs and answers, each at its recorded
    time, so WIN waits for the vet as long, with no news, EDGAR or claude; every Book B row,
    each call's ``pack_id`` among them, comes back the same."""
    run = await golden_ab_run(tmp_path, recording=True)
    live = await run.journal.read(snapshot_ab)
    await run.close()
    report = await replay_day(
        recordings=tmp_path / "recordings",
        day=DAY,
        out=tmp_path / "replay",
        settings=SETTINGS,
        risk=RISK,
        recorded_db=tmp_path / "trader.db",
    )
    assert (report.state, report.divergences, report.differences, report.notes) == (
        "DONE", [], [], []
    )  # fmt: skip
    with closing(sqlite3.connect(tmp_path / "replay" / "trader.db")) as conn:
        replayed = snapshot_ab(conn)
    assert {"day_mode", "verdict", "llm_call", "b_call"} <= set(
        comparable_at(tmp_path / "trader.db")
    )  # Book B's rows were compared
    assert replayed == live
    assert (tmp_path / "replay" / "llm" / "2026-10-13" / "20261013-vet-01-1.output.json").exists()


def comparable_at(db: Path) -> dict[str, list[str]]:
    with closing(sqlite3.connect(db)) as conn:
        return comparable(conn, DAY)


async def test_a_recorded_day_replays_the_same(tmp_path: Path) -> None:
    run = await golden_run(tmp_path, recording=True)
    await run.close()
    report = await replay_day(
        recordings=tmp_path / "recordings",
        day=DAY,
        out=tmp_path / "replay",
        settings=SETTINGS,
        risk=RISK,
        recorded_db=tmp_path / "trader.db",
    )
    assert report.state == "DONE"
    assert (report.divergences, report.differences) == ([], [])
    assert report.lines()[-1] == "the replay matches the recorded day"
    assert (tmp_path / "replay" / "reports" / "2026-10-13.md").exists()
    # The replay ran what the recording did: the fees step and every hourly clock reading
    # (D-055). Without the capabilities, it would skip them and still "match" above.
    boot = json.loads((tmp_path / "recordings" / "2026-10-13" / "boot.json").read_text("utf-8"))
    assert boot["capabilities"] == sorted(CURRENT)

    def day_runs(db: Path) -> list[tuple[str, str, str]]:
        with closing(sqlite3.connect(db)) as conn:
            return conn.execute("SELECT kind, name, at FROM day_runs ORDER BY id").fetchall()

    recorded = day_runs(tmp_path / "trader.db")
    assert ("step", "fees:A") in {(k, n) for k, n, _ in recorded}
    assert [r for r in recorded if r[0] == "clock"] != []
    assert day_runs(tmp_path / "replay" / "trader.db") == recorded

    def shadows(db: Path) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM shadow_outcomes ORDER BY signal_id").fetchall()
            return [dict(r) for r in rows]

    # The tape reads replay too: CAP's and GAPPER's ambiguous minutes come from them (D-057).
    live_shadows = shadows(tmp_path / "trader.db")
    assert [r for r in live_shadows if r["trigger_resolution"] == "tape"] != []
    assert {r["spy_move_pct"] for r in live_shadows} == {None}  # an A-only day doesn't review
    assert shadows(tmp_path / "replay" / "trader.db") == live_shadows


async def test_a_recorded_day_that_books_fees_replays_the_same(tmp_path: Path) -> None:
    """D-055: boot books a charge from the last session, two weeks back, and EOD one posted
    during the day. The replay rebuilds the ledger from boot.json, where that estimate is
    already retired, so its boot read asks a different ``after``; reads still match the
    recording by time, and the seed time comes back from boot.json."""
    run = await DayRun(tmp_path, standard_day(), cash=D("1100"), recording=True).open()
    seeded, sold = date(2026, 9, 1), date(2026, 9, 28)  # the last session two weeks back
    rows = [
        LedgerEntry(book="A", kind="deposit", amount=D(1000), trade_date=seeded,
                    settle_date=seeded, ref="seed"),
        LedgerEntry(book="A", kind="sell", symbol="OLD", amount=D(100), trade_date=sold,
                    settle_date=date(2026, 9, 29), ref="fill:old:1"),
        LedgerEntry(book="A", kind="fee", symbol="OLD", amount=D("-0.01"), trade_date=sold,
                    settle_date=date(2026, 9, 29), ref="fee-est:old:1"),
    ]  # fmt: skip
    created = datetime.combine(seeded, time(9), ET)
    await run.journal.write(lambda conn: insert_entries(conn, rows, created))
    sim = run.sims["A"]
    sim.charge_fee(D("0.02"), day=sold, posted=datetime.combine(sold, time(20, 30), ET))
    run.at("12:00:00", lambda: sim.charge_fee(D("0.01"), sub_type="TAF"))
    await run.run()
    await run.close()
    report = await replay_day(
        recordings=tmp_path / "recordings",
        day=DAY,
        out=tmp_path / "replay",
        settings=SETTINGS,
        risk=RISK,
        recorded_db=tmp_path / "trader.db",
    )
    assert (report.state, report.divergences, report.differences) == ("DONE", [], [])
    boot = json.loads((tmp_path / "recordings" / "2026-10-13" / "boot.json").read_text("utf-8"))
    assert boot["books"]["A"]["seeded_at"] == "2026-09-01T09:00:00-04:00"

    def table(db: Path, sql: str) -> list[tuple[Any, ...]]:
        with closing(sqlite3.connect(db)) as conn:
            return [tuple(r) for r in conn.execute(sql).fetchall()]

    ledger = "SELECT kind, symbol, amount, trade_date, settle_date, ref FROM ledger_entries"
    for sql in (f"{ledger} ORDER BY ref", "SELECT * FROM daily_stats"):
        assert table(tmp_path / "replay" / "trader.db", sql) == table(tmp_path / "trader.db", sql)
    # Each charge booked when it was live: one at boot, one at EOD. Keyed by ``after``, the
    # boot read found EOD's answer instead and booked the midday charge before E.
    booked = "SELECT ref, created_at FROM ledger_entries WHERE ref LIKE 'fee:%' ORDER BY ref"
    assert table(tmp_path / "trader.db", booked) == [
        ("fee:fee-1", "2026-10-13T09:20:00-04:00"),
        ("fee:fee-2", "2026-10-13T16:05:00-04:00"),
    ]
    assert table(tmp_path / "replay" / "trader.db", booked) == table(tmp_path / "trader.db", booked)


async def test_replay_reports_what_it_could_not_reproduce(tmp_path: Path) -> None:
    run = await golden_run(tmp_path, recording=True)
    await run.close()
    broker_file = tmp_path / "recordings" / "2026-10-13" / "broker-A.jsonl.gz"
    broker_file.unlink()  # as if Book A's broker traffic was never recorded
    report = await replay_day(
        recordings=tmp_path / "recordings",
        day=DAY,
        out=tmp_path / "replay",
        settings=SETTINGS,
        risk=RISK,
        recorded_db=tmp_path / "trader.db",
    )
    assert not report.matches
    assert any("not in the recording" in d for d in report.divergences)


def test_trader_replay_compares_with_the_journal(repo: Path) -> None:
    import asyncio

    async def record() -> None:
        run = await golden_run(repo / "runtime", recording=True)
        await run.close()

    asyncio.run(record())
    result = CliRunner().invoke(app, ["--root", str(repo), "replay", "--date", "2026-10-13"])
    assert result.exit_code == 0, result.output
    assert "the replay matches the recorded day" in result.output
    missing = CliRunner().invoke(app, ["--root", str(repo), "replay", "--date", "2026-10-14"])
    assert missing.exit_code == 1
    assert "no recording to replay" in missing.output


@pytest.mark.timeout(300)  # a whole real day: about 30 s
async def test_the_g3_day_replays_as_it_was_recorded(tmp_path: Path) -> None:
    """G3 run 2 (2026-10-14; D-054): Alpaca paper's real answers, replayed through the current
    engine, give the journal the live session wrote, apart from the changes ``CODE_CHANGES``
    lists, and the engine asks nothing the recording lacks. The replay's own journal is pinned
    too; ``UPDATE_GOLDEN=1`` rewrites it."""
    report = await replay_day(
        recordings=g3_day.RECORDINGS,
        day=g3_day.G3_DAY,
        out=tmp_path / "replay",
        settings=g3_day.settings(),
        risk=g3_day.risk(),
        events=g3_day.events(),
    )
    assert report.state == "DONE"
    assert report.divergences == []
    with closing(sqlite3.connect(tmp_path / "replay" / "trader.db")) as conn:
        conn.row_factory = sqlite3.Row
        assert diff(g3_day.recorded(), comparable(conn, g3_day.G3_DAY)) == g3_day.CODE_CHANGES
        got = dumps(snapshot(conn))
    if os.environ.get("UPDATE_GOLDEN") == "1":
        g3_day.GOLDEN.write_text(got + "\n", encoding="utf-8")
    want = g3_day.GOLDEN.read_text(encoding="utf-8")
    assert got + "\n" == want, "run UPDATE_GOLDEN=1 if intended"


@pytest.mark.timeout(600)  # a whole real A+B day
async def test_the_g4b_day_replays_as_it_was_recorded(tmp_path: Path) -> None:
    """G4b (2026-11-20; D-072): both books on Alpaca paper's real answers, with Claude's recorded
    packs and answers served back, give the journal the live session wrote. The engine asks
    nothing the recording lacks and sends every call the pack it recorded. The replay's own
    journal, the review's tables included, is pinned too; ``UPDATE_GOLDEN=1`` rewrites it."""
    report = await replay_day(
        recordings=g4b_day.RECORDINGS,
        day=g4b_day.G4B_DAY,
        out=tmp_path / "replay",
        settings=g4b_day.settings(),
        risk=g4b_day.risk(),
        events=g4b_day.events(),
    )
    assert report.state == "DONE"
    assert report.divergences == []
    with closing(sqlite3.connect(tmp_path / "replay" / "trader.db")) as conn:
        conn.row_factory = sqlite3.Row
        assert diff(g4b_day.recorded(), comparable(conn, g4b_day.G4B_DAY)) == g4b_day.CODE_CHANGES
        reviewed = conn.execute(
            "SELECT status, targets_json FROM daily_reviews WHERE date = ?",
            (g4b_day.G4B_DAY.isoformat(),),
        ).fetchone()
        targets = json.loads(reviewed["targets_json"])
        stored = {r[0] for r in conn.execute("SELECT signal_id FROM signal_reviews")}
        got = dumps(snapshot_ab(conn))
        reports_read_the_g4b_journal(conn)
    assert reviewed["status"] == "ok"
    assert len(targets) == 7
    assert stored == set(targets)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        g4b_day.GOLDEN.write_text(got + "\n", encoding="utf-8")
    want = g4b_day.GOLDEN.read_text(encoding="utf-8")
    assert got + "\n" == want, "run UPDATE_GOLDEN=1 if intended"


def reports_read_the_g4b_journal(conn: sqlite3.Connection) -> None:
    """The weekly report and the evaluation memo on a journal the real engine wrote (P5.4,
    P5.5). On G4b's day 10 longs triggered live and VNT's shadow on SIP bars alone: 11 with a
    standing verdict, all allowed or neutral. BILL's veto never triggered, so there is no V
    (D-072)."""
    cfg = g4b_day.settings().lessons
    week = build_weekly(conn, year=2026, week=47, boot=Bootstrap.of(cfg))
    veto = week.epoch_stats.veto
    assert (veto.n_allowed, veto.n_vetoed, veto.v) == (11, 0, None)
    assert week.epoch_stats.b_minus_a.value == pytest.approx(-0.148, abs=5e-4)
    md = weekly_md(week)
    assert md.startswith("# Weekly report 2026-W47 (2026-11-16 to 2026-11-20)")
    assert "| 2026-11-20 09:35 | P3 | execution.A | partial_fill_protected |" in md
    assert "<h1>" in weekly_html(week)

    memo = build_memo(conn, through=g4b_day.G4B_DAY, cfg=cfg)
    assert memo.outcome == "keep running"
    assert counts(memo) == "1 trading day, 11 triggered longs, 0 Book B vetoes"
    assert memo.hypotheses[0].numbers.startswith("V n/a, no interval; allowed n 11, vetoed n 0")
    assert "<h2>Hypotheses</h2>" in memo_html(memo)


# ---------------------------------------------------------------- ReplayBroker

T0 = datetime(2026, 10, 13, 9, 35, 50, tzinfo=ET)


def at(seconds: float) -> str:
    return fmt_iso(T0 + timedelta(seconds=seconds))


def order(cid: str, status: str = "new", oid: str = "o-1") -> dict[str, Any]:
    return {
        "id": oid,
        "client_order_id": cid,
        "symbol": "XYZ",
        "side": "buy",
        "type": "limit",
        "order_class": "oto",
        "time_in_force": "day",
        "qty": 10,
        "filled_qty": 0,
        "status": status,
        "legs": [],
    }


RECORDS: list[dict[str, Any]] = [
    {"method": "positions", "args": {}, "start_rx": at(1), "end_rx": at(1.1), "result": []},
    {
        "method": "positions",
        "args": {},
        "start_rx": at(10),
        "end_rx": at(10.1),
        "result": [
            {
                "symbol": "XYZ",
                "qty": 10,
                "qty_available": 0,
                "avg_entry_price": "12.33",
                "current_price": "12.35",
            }
        ],
    },
    {
        "method": "submit_entry_oto",
        "args": {"client_order_id": "A-1"},
        "start_rx": at(0),
        "end_rx": at(0.2),
        "result": order("A-1"),
    },
    {
        "method": "submit_entry_oto",
        "args": {"client_order_id": "A-2"},
        "start_rx": at(1),
        "end_rx": at(1.1),
        "error": {
            "type": "OrderRejected",
            "message": "no",
            "reason": "no money",
            "status": 403,
            "code": 40310000,
        },
    },
    {
        "method": "cancel",
        "args": {"order_id": "o-1"},
        "start_rx": at(2),
        "end_rx": at(2.05),
        "error": {"type": "OrderNotCancelable", "message": "already filled"},
    },
    {
        "method": "trade_update",
        "rx": at(0.3),
        "event": {
            "type": "update",
            "event": "fill",
            "order": order("A-1", "filled"),
            "at": at(0.25),
            "qty": 10,
            "price": "12.33",
        },
    },
]


def intent(cid: str) -> OrderIntent:
    return OrderIntent(
        book="A",
        signal_id="s",
        symbol="XYZ",
        qty=10,
        entry_limit=D("12.35"),
        stop_price=D("12.21"),
        client_order_id=cid,
        day_size_multiplier=D(1),
    )


async def test_the_replay_broker_answers_from_the_recording() -> None:
    clock = SimClock(T0)
    broker = ReplayBroker(RECORDS, clock, fallback=SimBroker(clock=clock))

    async def day() -> None:
        placed = await broker.submit_entry_oto(intent("A-1"))
        assert (placed.id, clock.now()) == ("o-1", T0 + timedelta(seconds=0.2))
        with pytest.raises(OrderRejected) as rejected:
            await broker.submit_entry_oto(intent("A-2"))
        assert (rejected.value.reason, rejected.value.code) == ("no money", 40310000)
        with pytest.raises(OrderNotCancelable):
            await broker.cancel("o-1")
        assert await broker.positions() == []  # the call recorded nearest in time (1 s)
        await clock.sleep_until(T0 + timedelta(seconds=9.5))  # 0.5 s before the second one
        assert [p.qty for p in await broker.positions()] == [10]
        updates = broker.trade_updates()
        update = await updates.__anext__()
        assert getattr(update, "event", None) == "fill"
        assert broker.divergences == []
        await broker.submit_entry_oto(intent("A-3"))  # never recorded: the fallback answers
        assert broker.divergences == ["submit A-3: not in the recording"]

    await drive(clock, day())


def test_the_golden_file_is_valid_json() -> None:
    assert set(json.loads(GOLDEN.read_text(encoding="utf-8"))) >= {"signals", "decisions"}
