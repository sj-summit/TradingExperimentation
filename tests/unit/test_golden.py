"""The golden day, determinism across processes, and record-then-replay (P3.8b; D-049)."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fixtures import g3_day
from tests.fixtures.days import DAY
from tests.fixtures.exec_harness import RISK, SETTINGS
from tests.fixtures.golden import GOLDEN, dumps, golden_run, golden_snapshot, snapshot
from tests.helpers import REPO
from trader.cli import app
from trader.core.clock import ET, SimClock, fmt_iso
from trader.core.models import OrderIntent
from trader.execution.broker import OrderNotCancelable, OrderRejected
from trader.execution.replay import ReplayBroker
from trader.execution.sim_broker import SimBroker
from trader.orchestrator.replay import comparable, diff, replay_day
from trader.orchestrator.simdrive import drive

D = Decimal


async def test_the_golden_day(tmp_path: Path) -> None:
    got = dumps(await golden_snapshot(tmp_path))
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(got + "\n", encoding="utf-8")
    assert got + "\n" == GOLDEN.read_text(encoding="utf-8"), "run UPDATE_GOLDEN=1 if intended"


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
        outputs.append((folder / "snapshot.json").read_text(encoding="utf-8"))
    assert outputs[0] == outputs[1]
    assert outputs[0] + "\n" == GOLDEN.read_text(encoding="utf-8")


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
    engine, give the journal the live session wrote, and the engine asks nothing the recording
    lacks. The replay's own journal is pinned too; ``UPDATE_GOLDEN=1`` rewrites it."""
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
        assert diff(g3_day.recorded(), comparable(conn, g3_day.G3_DAY)) == []
        got = dumps(snapshot(conn))
    if os.environ.get("UPDATE_GOLDEN") == "1":
        g3_day.GOLDEN.write_text(got + "\n", encoding="utf-8")
    want = g3_day.GOLDEN.read_text(encoding="utf-8")
    assert got + "\n" == want, "run UPDATE_GOLDEN=1 if intended"


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
