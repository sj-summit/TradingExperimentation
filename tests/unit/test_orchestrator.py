"""Whole days through the real orchestrator on a SimClock (P3.5; DECISIONS D-048)."""

import json
from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from tests.fixtures.dayrun import DayRun
from tests.fixtures.days import DAY, D, SyntheticDay, path, standard_day
from trader.core.clock import ET, SimClock
from trader.data.replay import ReplayHub
from trader.data.stream import Channel
from trader.data.ws import StreamError
from trader.execution.broker import Order, OrderRejected
from trader.execution.sim_broker import SimBroker
from trader.risk.ledger import LedgerError, SettlementLedger

STATES = ["BOOT", "PREMARKET", "OR_SCAN", "TRADING", "FLATTEN", "EOD", "REVIEW", "REPORT", "DONE"]


@pytest.fixture
async def normal(tmp_path: Path) -> AsyncIterator[DayRun]:
    run = await DayRun(tmp_path, standard_day()).open()
    yield run
    await run.close()


async def test_a_normal_day(normal: DayRun) -> None:
    outcome = await normal.run()
    assert (outcome.day, outcome.state) == (DAY, "DONE")
    assert await normal.states() == STATES
    assert await normal.statuses() == {
        "WIN": "triggered",
        "LOSE": "triggered",
        "CHASE": "missed_at_arm",
        "GAPPER": "triggered",
        "EXP": "expired",
        "SHORT": "not_armed",
        "DOJI": "not_armed",
        "CAP": "triggered",
    }
    assert await normal.decisions() == {
        "WIN": "taken",
        "LOSE": "taken",
        "CHASE": "missed_at_arm",
        "GAPPER": "chased",
        "CAP": "insufficient_settled_cash",
    }
    assert await normal.trades() == {
        "WIN": (24, "20.43", "21.98", "eod_flatten"),
        "LOSE": (16, "30.53", "30.39", "stop"),
    }
    assert await normal.incidents() == []
    assert normal.sims["A"].cash == D("1034.96")  # 1000 + 24 x 1.55 - 16 x 0.14
    (stats,) = await normal.rows("SELECT * FROM daily_stats")
    assert (stats["book"], stats["start_equity"], stats["end_equity"]) == ("A", "1000", "1034.92")
    assert (stats["pnl"], stats["n_signals"], stats["n_triggered"], stats["n_trades"]) == (
        "34.92",
        8,
        4,
        2,
    )
    assert json.loads(stats["regime_json"])["day_of_week"] == "Tue"
    shadow = await normal.rows("SELECT signal_id, exit_reason FROM shadow_outcomes")
    assert len(shadow) == 7  # every priced signal: six longs and the short; the doji has none
    assert await normal.steps() == [
        "reconcile:A",
        "decisions:A",
        "trades:A",
        "stats:A",
        "breakers:A",
        "shadow",
        "report",
    ]
    root = normal.root
    assert (root / "reports" / "2026-10-13.md").exists()
    assert (root / "backups" / "trader-2026-10-13.db").exists()
    beat = json.loads((root / "runtime" / "heartbeat.json").read_text(encoding="utf-8"))
    assert (beat["state"], beat["books"]["A"]["halted"]) == ("REPORT", False)
    boot = json.loads((root / "recordings" / "2026-10-13" / "boot.json").read_text("utf-8"))
    assert boot["books"]["A"]["broker_cash"] == "1000"
    assert boot["books"]["A"]["ledger"][0]["ref"] == "seed"
    assert (root / "recordings" / "2026-10-13" / "or_scan.parquet").exists()
    assert [title for title, _, _ in normal.notifier.sent] == ["trader 2026-10-13"]
    states = [
        r["run_status"]
        for r in await normal.rows("SELECT run_status FROM trading_days WHERE date = '2026-10-13'")
    ]
    assert states == ["DONE"]


async def test_a_finished_day_is_not_run_again(normal: DayRun) -> None:
    await normal.run()
    again = await normal.run()
    assert again is not None
    assert (again.state, again.note) == ("DONE", "this session already ran to the end")
    assert (await normal.states()).count("BOOT") == 1


# ---------------------------------------------------------------- the calendar


async def test_an_early_close_moves_the_afternoon(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(date(2026, 11, 27))).open()  # 13:00 close
    await run.run()
    assert (await run.trades())["WIN"] == (24, "20.43", "21.98", "eod_flatten")
    rows = await run.rows("SELECT name, at FROM day_runs WHERE kind = 'step' ORDER BY id")
    at = {r["name"]: r["at"][11:16] for r in rows}
    assert (at["reconcile:A"], at["shadow"], at["report"]) == ("13:05", "13:12", "13:30")
    fills = await run.rows("SELECT filled_at FROM fills WHERE side = 'sell' AND symbol = 'WIN'")
    assert [f["filled_at"][11:16] for f in fills] == ["12:55"]
    assert (await run.statuses())["EXP"] == "expired"
    await run.close()


async def test_a_holiday_does_nothing(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(DAY)).open()
    run.clock = SimClock(datetime(2026, 11, 26, 8, 30, tzinfo=ET))  # Thanksgiving
    outcome = await run.run()
    assert outcome is not None
    assert outcome.note == "not a trading day"
    assert await run.states() == []
    await run.close()


# ---------------------------------------------------------------- late starts and restarts


async def test_a_late_start_saves_the_scan_but_arms_nothing(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), start=time(10, 0)).open()
    await run.run()
    assert set((await run.statuses()).values()) == {"not_armed"}
    assert await run.decisions() == {}
    assert await run.incidents() == [("P2", "late_start")]
    features = await run.rows("SELECT features_json FROM signals")
    assert all(json.loads(r["features_json"])["late_start"] is True for r in features)
    assert len(await run.rows("SELECT 1 FROM shadow_outcomes")) == 7  # H4 keeps the day
    await run.close()


async def test_a_start_after_the_flatten_runs_only_the_end_of_the_day(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), start=time(16, 20)).open()
    await run.run()
    assert await run.states() == STATES
    assert await run.statuses() == {}
    assert "bars:5Min" not in run.market.calls
    assert await run.incidents() == []
    (stats,) = await run.rows("SELECT n_signals, n_trades FROM daily_stats")
    assert (stats["n_signals"], stats["n_trades"]) == (0, 0)
    await run.close()


async def test_a_crash_mid_day_is_picked_up_without_a_p1(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day()).open()
    assert await run.run(until="09:50:00") is None  # WIN and LOSE are held
    await run.wait_until("10:20:00")  # LOSE's stop fills at 10:14:50 while the engine is down
    run.new_hub()
    await run.run()
    assert await run.trades() == {
        "WIN": (24, "20.43", "21.98", "eod_flatten"),
        "LOSE": (16, "30.53", "30.39", "stop"),
    }
    decisions = await run.decisions()
    assert decisions["GAPPER"] == "missed_at_arm"  # it ran past its cap while we were down
    assert decisions["CAP"] == "insufficient_settled_cash"
    assert await run.incidents() == []
    assert (await run.states()).count("BOOT") == 2
    await run.close()


async def test_a_restart_after_the_flatten_time_still_flattens(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day()).open()
    await run.run(until="15:50:00")
    await run.wait_until("15:57:00")
    run.new_hub()
    await run.run()
    assert (await run.trades())["WIN"] == (24, "20.43", "21.98", "eod_flatten")
    sells = await run.rows("SELECT filled_at FROM fills WHERE side = 'sell' AND symbol = 'WIN'")
    assert [s["filled_at"][11:16] for s in sells] == ["15:57"]
    assert await run.incidents() == []
    await run.close()


async def test_an_eod_rerun_repeats_nothing(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day()).open()
    await run.run(until="16:10:00")  # EOD (16:05) is done; shadow outcomes (16:12) aren't
    await run.wait_until("16:20:00")
    run.new_hub()
    await run.run()
    steps = await run.steps()
    assert sorted(steps) == sorted(set(steps))
    assert steps[-2:] == ["shadow", "report"]
    (row,) = await run.rows("SELECT consecutive_losing_days, streak_through FROM book_state")
    assert (row["consecutive_losing_days"], row["streak_through"]) == (0, "2026-10-13")
    await run.close()


# ---------------------------------------------------------------- boot checks


def overnight_day() -> SyntheticDay:
    day = standard_day()
    old = path(
        "OLD", open_="12.00", keys=[("09:30", "12.00"), ("16:00", "12.10")], in_universe=False
    )
    return SyntheticDay(day=day.day, symbols=[*day.symbols, old])


async def test_a_position_carried_overnight_is_a_p1_and_flattened_at_the_open(
    tmp_path: Path,
) -> None:
    run = await DayRun(tmp_path, overnight_day()).open()
    run.sims["A"].hold("OLD", 10, D("11.50"))  # $885 cash left
    ledger = SettlementLedger(run.journal, "A", run.clock)
    await ledger.seed(run.sims["A"].cash, positions=0, open_orders=0)
    await run.run()
    # Someone else's shares, with no stop and no plan: each problem is reported once, and the
    # adoption leaves the journal flat once they're sold (no mismatch at EOD).
    assert await run.incidents() == [("P1", "boot_unexplained"), ("P1", "unprotected_position")]
    adopted = await run.rows("SELECT side, qty, price FROM fills WHERE id LIKE 'adopt:%'")
    assert [(r["side"], r["qty"], r["price"]) for r in adopted] == [("buy", 10, "11.50")]
    assert "p1_incident" in await run.halts()
    assert run.hub.history[0] == ("09:29:00", ["IWM", "OLD", "QQQ", "SPY"])  # held: streamed
    assert "OLD" not in run.hub.history[-1][1]  # sold at the open, so not at 09:35
    sells = await run.rows("SELECT symbol, filled_at FROM fills WHERE side = 'sell'")
    assert [s["symbol"] for s in sells] == ["OLD"]
    assert "09:30:00" <= sells[0]["filled_at"][11:19] <= "09:30:05"  # sold at the open
    decisions = await run.decisions()
    assert decisions["WIN"] == "book_halted"
    assert await run.trades() == {}
    await run.close()


async def test_a_first_boot_refuses_an_account_that_is_not_flat(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, overnight_day()).open()
    run.sims["A"].hold("OLD", 10, D("11.50"))
    with pytest.raises(LedgerError, match="flatten it first"):
        await run.run()
    await run.close()


async def test_a_skewed_clock_arms_nothing(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), skew=timedelta(seconds=5)).open()
    await run.run()
    assert await run.incidents() == [("P2", "clock_skew")]
    assert set((await run.statuses()).values()) == {"not_armed"}
    assert await run.trades() == {}
    await run.close()


async def test_a_missed_scan_deadline_arms_nothing(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day()).open()
    run.market.delays["bars:5Min"] = timedelta(seconds=100)  # done 09:36:43, deadline 09:36:30
    await run.run()
    assert await run.incidents() == [("P2", "scan_deadline_missed")]
    assert set((await run.statuses()).values()) == {"not_armed"}
    assert await run.decisions() == {}
    await run.close()


# ---------------------------------------------------------------- the kill switch and flatten


async def test_the_kill_file_flattens_through_a_broker_error(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day()).open()

    def kill() -> None:
        run.kill_file.parent.mkdir(parents=True, exist_ok=True)
        run.kill_file.touch()
        run.sims["A"].fail_next_submit("timeout_before")  # the first sell never arrives

    run.at("10:30:00", kill)
    await run.run()
    assert (await run.trades())["WIN"][3] == "kill_switch"
    assert (await run.trades())["LOSE"][3] == "stop"  # stopped at 10:14:50, before the kill
    assert (await run.decisions())["CAP"] == "book_halted"
    assert await run.halts() == ["kill_switch"]
    lost = await run.rows("SELECT 1 FROM orders WHERE status = 'lost' AND purpose = 'kill_switch'")
    assert len(lost) == 1
    await run.close()


async def test_the_kill_switch_and_the_flatten_take_turns(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day()).open()
    run.at(
        "15:55:00",
        lambda: (run.kill_file.parent.mkdir(parents=True, exist_ok=True), run.kill_file.touch()),
    )
    await run.run()
    sells = await run.rows("SELECT qty FROM fills WHERE side = 'sell' AND symbol = 'WIN'")
    assert [s["qty"] for s in sells] == [24]  # sold once
    assert (await run.trades())["WIN"][3] in ("eod_flatten", "kill_switch")
    assert await run.incidents() == []
    await run.close()


class NoBids(SimBroker):
    """A broker that refuses every market sell: nothing can be flattened."""

    async def submit_market_sell(self, symbol: str, qty: int, client_order_id: str) -> Order:
        raise OrderRejected("no market for the shares (test)", status=403)


async def test_not_flat_by_close_is_a_p1_and_leaves_a_gtc_stop(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), sim_class=NoBids).open()
    await run.run()
    assert ("P1", "not_flat") in await run.incidents()
    assert [c for s, c in await run.incidents() if s == "P1"] == ["not_flat"]
    assert "p1_incident" in await run.halts()
    held = await run.sims["A"].positions()
    assert [(p.symbol, p.qty) for p in held] == [("WIN", 24)]
    stops = [o for o in await run.sims["A"].open_orders() if o.type == "stop"]
    assert [(o.symbol, o.time_in_force, o.stop_price) for o in stops] == [
        ("WIN", "gtc", D("20.31"))
    ]
    await run.close()


# ---------------------------------------------------------------- the stream


class TradesOnly(ReplayHub):
    """A server that refuses trades and quotes for more than four symbols (a 405)."""

    async def set_subscriptions(
        self, symbols: Iterable[str], *, channels: Sequence[Channel] | None = None
    ) -> dict[str, list[str]]:
        wanted = list(symbols)
        if channels is not None and "quotes" in channels and len(wanted) > 4:
            raise StreamError(405, "symbol limit exceeded")
        return await super().set_subscriptions(wanted, channels=channels)


async def test_a_405_falls_back_to_trades_only(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day()).open()
    run.hub = TradesOnly(clock=run.clock, events=run.events, symbol_limit=30)  # type: ignore[assignment]
    await run.run()
    assert await run.incidents() == [("P2", "subscription_limited")]
    assert run.hub.acked == {"trades": sorted(run.hub.symbols)}
    assert (await run.trades())["WIN"][3] == "eod_flatten"
    await run.close()
