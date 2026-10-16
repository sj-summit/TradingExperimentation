"""Whole days through the real orchestrator on a SimClock (P3.5; DECISIONS D-048)."""

import json
import sqlite3
from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import trader.orchestrator.day as day_module
from tests.fixtures.dayrun import DayRun
from tests.fixtures.days import DAY, D, SyntheticDay, path, standard_day
from trader.core.clock import ET, SimClock
from trader.data.replay import ReplayHub
from trader.data.stream import Channel
from trader.data.ws import StreamError
from trader.execution.broker import BrokerUnavailable, FeeActivity, Order, OrderRejected
from trader.execution.service import ExecutionService, FlattenResult
from trader.execution.sim_broker import SimBroker
from trader.memory.shadow import ShadowRun
from trader.orchestrator.capabilities import CURRENT, SHADOW_TAPE, SKEW_HOURLY
from trader.orchestrator.eod import BookClose, EodContext
from trader.risk.ledger import LedgerEntry, LedgerError, SettlementLedger, insert_entries
from trader.strategy.triggers import TriggerEngine

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
        "CAP": "capacity_entries",  # cash and R-007 both bind; capacity names it (D-056)
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
    shadow = await normal.rows(
        "SELECT signal_id, exit_reason, sip_only_trigger FROM shadow_outcomes"
    )
    assert len(shadow) == 7  # every priced signal: six longs and the short; the doji has none
    sip_only = {r["signal_id"].split("-")[1]: r["sip_only_trigger"] for r in shadow}
    assert (sip_only["WIN"], sip_only["CHASE"]) == (0, 1)  # the live feed triggered WIN (D-051)
    assert await normal.steps() == [
        "reconcile:A",
        "decisions:A",
        "trades:A",
        "fees:A",
        "stats:A",
        "breakers:A",
        "shadow",
        "report",
    ]
    root = normal.root
    assert (root / "reports" / "2026-10-13.md").exists()
    assert (root / "backups" / "trader-2026-10-13.db").exists()
    beat = json.loads((root / "runtime" / "heartbeat.json").read_text(encoding="utf-8"))
    assert (beat["state"], beat["books"]["A"]["halted"]) == ("DONE", False)
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
    (stats,) = await run.rows("SELECT n_signals, n_trades, max_intraday_dd FROM daily_stats")
    assert (stats["n_signals"], stats["n_trades"]) == (0, 0)
    assert stats["max_intraday_dd"] is None  # it sampled none of the session: unknown (D-051)
    await run.close()


async def test_after_the_flatten_a_failed_check_promises_no_recheck(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), start=time(16, 20)).open()
    run.market.fail["clock"] = TimeoutError("no answer (test)")
    await run.run()
    (row,) = await run.rows("SELECT code, message FROM incidents")
    assert row["code"] == "clock_skew"
    assert row["message"].endswith("nothing armed today")  # no 09:29 re-check is coming
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
    assert decisions["CAP"] == "capacity_entries"
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
    await run.run(run.deps(capabilities=CURRENT - {SKEW_HOURLY}))  # boot's checks only
    # Boot reads it three times (D-051), and so does the 09:29 re-check: still 5 s off.
    assert await run.incidents() == [("P2", "clock_skew"), ("P2", "clock_skew")]
    messages = [r["message"] for r in await run.rows("SELECT message FROM incidents")]
    assert messages[0].endswith("checking again before the scan")
    assert messages[1].endswith("nothing armed today")
    assert run.market.calls.count("clock") == 6
    assert set((await run.statuses()).values()) == {"not_armed"}
    assert await run.trades() == {}
    await run.close()


async def test_one_slow_clock_answer_is_read_again_not_taken_as_skew(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day()).open()
    answer, calls = run.market.clock, []

    async def slow_once() -> datetime:  # the first answer takes 10 s (a timeout, then a retry)
        calls.append(run.clock.now())
        if len(calls) == 1:
            await run.clock.sleep_until(run.clock.now() + timedelta(seconds=10))
        return await answer()

    run.market.clock = slow_once  # type: ignore[method-assign]
    await run.run(run.deps(capabilities=CURRENT - {SKEW_HOURLY}))  # boot's reading only
    assert len(calls) == 2  # read again; the quick answer counts
    assert await run.incidents() == []
    assert (await run.trades())["WIN"][3] == "eod_flatten"
    await run.close()


async def test_the_clock_is_read_every_hour_and_a_new_skew_is_a_p2_once(tmp_path: Path) -> None:
    """D-055: boot's check alone decides arming, but the PC drifts or is stepped later on."""
    run = await DayRun(tmp_path, standard_day()).open()
    run.at("12:00:00", lambda: setattr(run.market, "_skew", timedelta(seconds=3)))
    await run.run()
    readings = await run.rows("SELECT at, detail FROM day_runs WHERE kind = 'clock'")
    assert [(r["at"][11:16], r["detail"]) for r in readings][:3] == [
        ("10:20", "+0.000"),
        ("11:20", "+0.000"),
        ("12:20", "+3.000"),
    ]
    assert await run.incidents() == [("P2", "clock_skew")]  # once, though every later one is off
    assert (await run.trades())["WIN"][3] == "eod_flatten"  # it only alerts
    await run.close()


async def test_a_failed_hourly_clock_reading_is_only_logged(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day()).open()
    run.at("10:00:00", lambda: run.market.fail.__setitem__("clock", TimeoutError("test")))
    await run.run()
    readings = await run.rows("SELECT at FROM day_runs WHERE kind = 'clock'")
    assert [r["at"][11:16] for r in readings][:2] == ["11:20", "12:20"]  # 10:20's failed
    assert await run.incidents() == []
    await run.close()


# ---------------------------------------------------------------- fees (D-055)


async def test_boot_books_the_overnight_charge_before_e(tmp_path: Path) -> None:
    """Yesterday's sale was estimated at 0.01; the broker charged 0.02 overnight. Boot books
    the charge and retires the estimate before E is set, so E is the broker's cash."""
    run = await DayRun(tmp_path, standard_day(), cash=D("1100")).open()
    prev, seeded = run.calendar.previous_session(DAY), date(2026, 10, 9)
    rows = [
        LedgerEntry(book="A", kind="deposit", amount=D(1000), trade_date=seeded,
                    settle_date=seeded, ref="seed"),
        LedgerEntry(book="A", kind="sell", symbol="OLD", amount=D(100), trade_date=prev,
                    settle_date=DAY, ref="fill:old:1"),
        LedgerEntry(book="A", kind="fee", symbol="OLD", amount=D("-0.01"), trade_date=prev,
                    settle_date=DAY, ref="fee-est:old:1"),
    ]  # fmt: skip
    created = datetime.combine(seeded, time(9), ET)
    await run.journal.write(lambda conn: insert_entries(conn, rows, created))
    run.sims["A"].charge_fee(D("0.02"), day=prev)
    await run.run()
    boot = json.loads((tmp_path / "recordings" / "2026-10-13" / "boot.json").read_text("utf-8"))
    assert boot["books"]["A"]["equity_sod"] == "1099.98"  # the broker's cash at boot
    booked = await run.rows(
        "SELECT ref, created_at FROM ledger_entries WHERE ref LIKE 'fee:%' "
        "OR ref LIKE 'fee-est-reversal:%' ORDER BY id"
    )
    assert [(r["ref"], r["created_at"][11:16]) for r in booked] == [
        ("fee:fee-1", "09:20"),
        (f"fee-est-reversal:{prev}:{DAY}", "09:20"),
    ]
    stats = await run.rows("SELECT start_equity, cash_residual FROM daily_stats")
    assert [(s["start_equity"], s["cash_residual"]) for s in stats] == [("1099.98", "0.00")]
    assert await run.incidents() == []
    await run.close()


async def test_a_failed_backup_after_done_is_only_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup runs after DONE (D-055); SQLite refusing it must not turn the day into a
    traceback (review)."""
    run = await DayRun(tmp_path, standard_day()).open()

    async def refused(target: Path) -> None:
        raise sqlite3.OperationalError("attempt to write a readonly database (test)")

    monkeypatch.setattr(run.journal, "backup", refused)
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "DONE"
    assert await run.incidents() == []
    await run.close()


async def test_a_fee_read_that_fails_is_a_p3_and_the_day_goes_on(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day()).open()

    async def down(after: datetime) -> list[FeeActivity]:
        raise BrokerUnavailable("account activities: HTTP 503 (test)")

    run.sims["A"].fee_activities = down  # type: ignore[method-assign]
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "DONE"
    assert await run.incidents() == [("P3", "fees_unavailable"), ("P3", "fees_unavailable")]
    assert (await run.trades())["WIN"][3] == "eod_flatten"
    await run.close()


async def test_an_unresolved_trigger_minute_is_a_p3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-057: the day asks the shadow job to read ambiguous minutes from the tape, and says
    when one fell back to the pessimistic rule; a replay of an older day asks for no tape."""
    asked: list[bool] = []

    async def shadow(**kw: Any) -> ShadowRun:
        asked.append(kw["tape"])
        return ShadowRun(ok=True, outcomes={}, tape_unresolved=("CAP: HTTP 500 (test)",))

    monkeypatch.setattr(day_module, "compute_shadow", shadow)
    run = await DayRun(tmp_path, standard_day()).open()
    await run.run()
    assert asked == [True]
    assert ("P3", "shadow_tape_unresolved") in await run.incidents()
    await run.close()
    older = await DayRun(tmp_path / "older", standard_day()).open()
    await older.run(older.deps(capabilities=CURRENT - {SHADOW_TAPE}))
    assert asked == [True, False]
    await older.close()


async def test_a_clock_unverified_at_boot_is_checked_again_before_the_scan(
    tmp_path: Path,
) -> None:
    run = await DayRun(tmp_path, standard_day()).open()
    run.market.fail["clock"] = TimeoutError("no answer (test)")
    run.market.fail["assets"] = TimeoutError("no answer (test)")
    await run.run()
    assert await run.incidents() == [("P2", "clock_skew"), ("P2", "assets_unavailable")]
    assert (await run.statuses())["WIN"] == "triggered"  # the 09:29 checks passed
    assert (await run.trades())["WIN"][3] == "eod_flatten"
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
    assert [c for s, c in await run.incidents() if s == "P1"] == ["not_flat", "not_flat"]
    p1s = await run.rows("SELECT message FROM incidents WHERE severity = 'P1' ORDER BY id")
    assert [r["message"] for r in p1s] == [
        "Book A: not flat by 15:58",
        "Book A: held after the close: WIN 24; GTC stops: WIN @ 20.31",  # what protects it
    ]
    assert await run.halts() == ["p1_incident"]  # halted once
    held = await run.sims["A"].positions()
    assert [(p.symbol, p.qty) for p in held] == [("WIN", 24)]
    stops = [o for o in await run.sims["A"].open_orders() if o.type == "stop"]
    assert [(o.symbol, o.time_in_force, o.stop_price) for o in stops] == [
        ("WIN", "gtc", D("20.31"))
    ]
    await run.close()


# ---------------------------------------------------------------- failures (D-051)


async def test_a_dead_trigger_engine_is_a_p2_at_once_and_the_day_still_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def dies(self: TriggerEngine, events: object) -> None:
        raise RuntimeError("engine failure (test)")

    monkeypatch.setattr(TriggerEngine, "run", dies)
    run = await DayRun(tmp_path, standard_day()).open()
    feeds: list[object] = []
    run.at("09:40:00", lambda: feeds.append(run.orchestrator._events))
    outcome = await run.run()
    assert feeds == [None]  # detached from the hub at once, not at last entry
    assert outcome is not None
    assert outcome.state == "DONE"
    assert await run.states() == STATES  # FLATTEN, EOD and REPORT still ran
    (row,) = await run.rows("SELECT code, message, ts FROM incidents")
    assert (row["code"], row["message"]) == (
        "state_failed",
        "the trigger engine died (RuntimeError): no more entries today",
    )
    assert row["ts"][11:19] < "09:36:00"  # at once, not at last entry
    assert await run.trades() == {}
    await run.close()


async def test_a_broker_error_after_the_close_is_retried_not_a_p1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flatten, close, failures = ExecutionService.flatten, datetime.combine(DAY, time(16), ET), []

    async def blip(self: ExecutionService, **kwargs: Any) -> FlattenResult:
        if self._clock.now() >= close and len(failures) < 2:  # a 503 at 16:00, twice
            failures.append(self._clock.now())
            raise BrokerUnavailable("no answer (test)")
        return await flatten(self, **kwargs)

    monkeypatch.setattr(ExecutionService, "flatten", blip)
    run = await DayRun(tmp_path, standard_day()).open()
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "DONE"
    assert [f.strftime("%H:%M:%S") for f in failures] == ["16:00:00", "16:00:05"]
    assert await run.incidents() == []
    assert await run.halts() == []
    await run.close()


async def test_a_failed_eod_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    close_book, tries = day_module.close_book, []

    async def flaky(ctx: EodContext, b: BookClose) -> None:
        tries.append(ctx.clock.now())
        if len(tries) == 1:
            raise BrokerUnavailable("no answer at 16:05 (test)")
        await close_book(ctx, b)

    monkeypatch.setattr(day_module, "close_book", flaky)
    run = await DayRun(tmp_path, standard_day()).open()
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "DONE"
    assert [t.strftime("%H:%M:%S") for t in tries] == ["16:05:00", "16:05:30"]
    assert await run.incidents() == []
    assert (await run.trades())["WIN"][3] == "eod_flatten"
    await run.close()


async def test_an_eod_that_keeps_failing_leaves_the_day_for_a_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    close_book, mode, tries = day_module.close_book, ["down"], []

    async def flaky(ctx: EodContext, b: BookClose) -> None:
        tries.append(ctx.clock.now())
        if mode[0] == "down" or (mode[0] == "blip" and len(tries) == 1):
            raise BrokerUnavailable("no answer (test)")
        await close_book(ctx, b)

    monkeypatch.setattr(day_module, "close_book", flaky)
    run = await DayRun(tmp_path, standard_day()).open()
    outcome = await run.run()
    assert outcome is not None
    assert (outcome.state, outcome.note) == (
        "REPORT",
        "EOD failed: run run-day again today to finish",
    )
    assert tries[-1].strftime("%H:%M:%S") == "16:11:30"  # retried until the shadow time
    assert await run.incidents() == [("P2", "state_failed")]
    assert "report" not in await run.steps()  # the report lacks the trades: rebuilt on a rerun
    assert await run.trades() == {}
    mode[0], tries[:] = "blip", []  # the rerun meets one more failure, and retries it
    run.new_hub()
    again = await run.run()
    assert again is not None
    assert again.state == "DONE"
    assert len(tries) == 2
    assert tries[1] - tries[0] == timedelta(seconds=30)
    assert (await run.trades())["WIN"][3] == "eod_flatten"
    steps = await run.steps()
    assert sorted(steps) == sorted(set(steps))  # nothing ran twice
    assert steps[-1] == "report"
    assert "stats:A" in steps
    (stats,) = await run.rows("SELECT max_intraday_dd FROM daily_stats")
    assert stats["max_intraday_dd"] is None  # the rerun sampled none of the session
    await run.close()


class NoBidsFlakyStops(NoBids):
    """No market for the shares, and the first two GTC stops get no answer."""

    gtc_failures = 2

    async def submit_stop(
        self, symbol: str, qty: int, stop_price: Decimal, client_order_id: str, *, tif: Any = "day"
    ) -> Order:
        if tif == "gtc" and self.gtc_failures:
            self.gtc_failures -= 1
            raise BrokerUnavailable("no answer (test)")
        return await super().submit_stop(symbol, qty, stop_price, client_order_id, tif=tif)


async def test_after_the_close_a_failed_gtc_stop_is_tried_again(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), sim_class=NoBidsFlakyStops).open()
    await run.run()
    p1s = await run.rows("SELECT message FROM incidents WHERE severity = 'P1' ORDER BY id")
    assert [r["message"] for r in p1s] == [
        "Book A: not flat by 15:58",
        "Book A: held after the close: WIN 24; GTC stops: WIN @ 20.31",  # no "NO STOP" part
    ]
    stops = [o for o in await run.sims["A"].open_orders() if o.time_in_force == "gtc"]
    assert [(o.symbol, o.qty) for o in stops] == [("WIN", 24)]
    await run.close()


async def test_a_slow_open_is_not_a_p1(tmp_path: Path) -> None:
    """G3 run 1's open (D-052, D-053): paper took 13 s to accept an entry and 4 s to cancel
    one, held a cancel sent before acceptance for 9.6 s, and filled an entry in part."""
    run = await DayRun(tmp_path, standard_day()).open()

    def slow_paper() -> None:
        sim = run.sims["A"]
        sim.accept_delay, sim.slow_cancels = timedelta(seconds=13), timedelta(seconds=4)
        sim.queued_cancel_delay = timedelta(seconds=9.6)
        sim.partial_next(13)

    run.at("09:35:49", slow_paper)  # WIN triggers at 09:35:50
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "DONE"
    assert [code for severity, code in await run.incidents() if severity == "P1"] == []
    assert await run.halts() == []
    assert (await run.trades())["WIN"][3] == "eod_flatten"
    buys = await run.rows(
        "SELECT filled_at FROM fills WHERE symbol = 'WIN' AND side = 'buy' ORDER BY filled_at"
    )
    assert buys[0]["filled_at"][11:19] >= "09:36:03"  # filled only once accepted
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
