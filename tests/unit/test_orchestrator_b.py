"""Book B in the day (P4.8, P4.9; D-068, D-069, D-070): its jobs at their times, its worker
parking a trigger whose verdict is pending, the boot record, and Book A untouched.

Whole synthetic days on a SimClock (``DayRun``), the model scripted, on the standard day with
its pre-market prints, news and filings (``days.standard_day``). P4.9's end-to-end scenarios:
a neutral day, a brief's veto, waits that enter, chase or time out, invalid answers and
degradation, ``no_trade`` and reduced days, a failed or missed brief, restarts, the call cap,
and an A-only day that asks for none of it. The A+B golden day is in ``test_golden.py``.
"""

import asyncio
import contextlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.dayrun import DayRun
from tests.fixtures.days import DAY, SyntheticDay, path, standard_day
from tests.fixtures.exec_harness import RISK, SETTINGS
from trader.books.base import BookDecision, Defer
from trader.books.verdicts import VerdictStore
from trader.config import Settings
from trader.core.clock import ET
from trader.core.models import Signal, TriggerEvent
from trader.data.recorder import Recorder, read_jsonl
from trader.llm.epoch import Fingerprint
from trader.llm.fake import Reply, Seen, answer_for, envelope
from trader.llm.replay import LLM_FILE
from trader.orchestrator.day import DayOrchestrator, _Book
from trader.orchestrator.llm_jobs import BookBJobs
from trader.orchestrator.replay import replay_day
from trader.orchestrator.simdrive import drive
from trader.reports.daily import build_daily

D = Decimal
# Whole days, two books each, some recorded and replayed: about 10 to 40 s apiece alone, more
# beside the rest of the suite.
pytestmark = pytest.mark.timeout(300)


def at(hhmmss: str) -> datetime:
    return datetime.combine(DAY, time.fromisoformat(hhmmss), ET)


async def ab_day(tmp_path: Path, **kw: Any) -> DayRun:
    return await DayRun(tmp_path, standard_day(), start=time(8, 55), books=("A", "B"), **kw).open()


async def test_book_b_trades_beside_book_a_on_a_neutral_day(tmp_path: Path) -> None:
    run = await ab_day(tmp_path)
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "DONE"
    a, b = await run.decisions("A"), await run.decisions("B")
    assert b == a  # every verdict neutral: Book B decides as Book A does
    assert a["WIN"] == "taken"
    assert a["CAP"] == "capacity_entries"
    calls = await run.rows("SELECT role, started_at, deadline, ok, candidates_json FROM llm_calls "
                           "ORDER BY started_at")  # fmt: skip
    assert [tuple(r) for r in calls] == [
        ("premarket", at("09:05:00").isoformat(), at("09:28:00").isoformat(), 1,
         '["GAPPER", "LOSE", "SHORT"]'),  # two gainers, then SHORT for its news
        ("vet", at("09:35:03").isoformat(), at("15:30:00").isoformat(), 1,
         '["WIN", "EXP", "CAP"]'),  # at arming: the armed longs the brief didn't judge
        ("review", at("16:15:00").isoformat(), at("16:29:00").isoformat(), 1,
         '["20261013-WIN-orb5", "20261013-LOSE-orb5", "20261013-GAPPER-orb5", '
         '"20261013-CAP-orb5", "20261013-CHASE-orb5"]'),  # the trades, then by |shadow R|
    ]  # fmt: skip
    vetted = await run.rows("SELECT symbol, source, signal_id, risk_flags_json FROM verdicts "
                            "ORDER BY symbol")  # fmt: skip
    assert [(r["symbol"], r["source"]) for r in vetted] == [
        ("CAP", "vet"), ("EXP", "vet"), ("GAPPER", "premarket"), ("LOSE", "premarket"),
        ("SHORT", "premarket"), ("WIN", "vet"),
    ]  # fmt: skip
    assert all(r["signal_id"] == (f"20261013-{r['symbol']}-orb5" if r["source"] == "vet" else None)
               for r in vetted)  # fmt: skip
    # GAPPER's news tried an injection: flagged in the pack, so code marks the verdict
    assert {r["symbol"] for r in vetted if "suspicious_content" in r["risk_flags_json"]} == {
        "GAPPER"
    }
    assert run.edgar.asked == ["GAPPER", "LOSE", "SHORT", "WIN", "EXP", "CAP"]  # rank order
    modes = await run.rows("SELECT book, mode, source, multiplier FROM day_modes")
    assert [tuple(r) for r in modes] == [("B", "normal", "premarket", "1")]
    taken = await run.rows(
        "SELECT s.symbol, d.llm_call_id FROM final_decisions d JOIN signals s ON s.id = "
        "d.signal_id WHERE d.book = 'B' AND d.reason_code = 'taken'"
    )
    assert {r["symbol"]: r["llm_call_id"] for r in taken} == {
        "WIN": "20261013-vet-01-1", "LOSE": "20261013-premarket-01-1",
    }  # fmt: skip
    assert (await run.trades("B")).keys() == (await run.trades("A")).keys() == {"WIN", "LOSE"}
    assert [i for i in await run.incidents() if i[0] in ("P1", "P2")] == []
    boot = json.loads((tmp_path / "recordings" / "2026-10-13" / "boot.json").read_text("utf-8"))
    assert boot["llm"] == {"book_b": "on", "fingerprint": None, "transport": "scripted"}
    await run.close()


async def test_a_trigger_waits_for_a_slow_vet_then_enters(tmp_path: Path) -> None:
    run = await ab_day(tmp_path)
    run.llm_delay["vet"] = 55.0  # WIN triggers at 09:35:50, before the vet answers
    await run.run()
    b = await run.decisions("B")
    assert b["WIN"] == "entered_after_wait"
    assert (await run.decisions("A"))["WIN"] == "taken"
    rows = await run.rows(
        "SELECT d.decided_at FROM decisions d JOIN signals s ON s.id = d.signal_id "
        "WHERE d.book = 'B' AND s.symbol = 'WIN'"
    )
    vet = await run.rows("SELECT ended_at FROM llm_calls WHERE role = 'vet'")
    assert [r["decided_at"] for r in rows] == [vet[0]["ended_at"]]  # asked again at the answer
    assert "WIN" in await run.trades("B")
    await run.close()


def slow_repair(seen: Seen) -> Reply:
    """The vet's first try answers nonsense at once; its repair takes longer than any wait."""
    pack = seen.pack()
    if pack["role"] != "vet":
        return Reply(envelope=envelope(answer_for(pack)), delay_s=1.0)
    if seen.is_repair:
        return Reply(envelope=envelope(answer_for(pack)), delay_s=70.0)
    return Reply(stdout=b"not json", delay_s=5.0)


async def test_a_trigger_still_pending_at_its_deadline_is_vet_timeout(tmp_path: Path) -> None:
    """WIN triggers at 09:35:50 while the repair runs: at the call's start + 60 s it is
    ``vet_timeout``. The repair then times out, so CAP (11:00:50) has no verdict to be had.
    LOSE, which the brief judged, never waited on the vet."""
    run = await ab_day(tmp_path, llm_script=slow_repair)
    await run.run()
    b = await run.decisions("B")
    assert (b["WIN"], b["LOSE"], b["CAP"]) == ("vet_timeout", "taken", "llm_unavailable")
    assert (await run.decisions("A"))["WIN"] == "taken"
    vet = await run.rows("SELECT started_at, failure FROM llm_calls WHERE role = 'vet' "
                         "ORDER BY id")  # fmt: skip
    assert [r["failure"] for r in vet] == ["invalid_json", "timeout"]
    deadline = datetime.fromisoformat(vet[0]["started_at"]) + timedelta(seconds=60)
    cap, won = await run.rows(
        "SELECT d.decided_at, d.detail, d.llm_call_id FROM decisions d JOIN signals s "
        "ON s.id = d.signal_id WHERE d.book = 'B' AND s.symbol IN ('WIN', 'CAP') ORDER BY s.symbol"
    )
    assert datetime.fromisoformat(won["decided_at"]) == deadline
    assert f"no verdict by {deadline:%H:%M:%S}" in won["detail"]
    assert won["llm_call_id"] is None
    assert "the vet failed: timeout" in cap["detail"]
    assert cap["llm_call_id"] == "20261013-vet-01-2"
    assert [i for i in await run.incidents() if i[1].startswith("llm")] == [
        ("P3", "llm_call_failed"), ("P3", "llm_call_failed"),
    ]  # fmt: skip
    await run.close()


NEUTRAL = {  # Book A's every day here; Book B's too, when the model is neutral throughout
    "WIN": "taken", "LOSE": "taken", "CHASE": "missed_at_arm", "GAPPER": "chased",
    "CAP": "capacity_entries",
}  # fmt: skip
ALL_ARMED = '["WIN", "LOSE", "GAPPER", "EXP", "CAP"]'  # the armed longs, by rank
MORNING = "FROM llm_calls WHERE role != 'review' ORDER BY id"  # not the 16:15 review


def llm_settings(**changes: Any) -> Settings:
    return SETTINGS.model_copy(update={"llm": SETTINGS.llm.model_copy(update=changes)})


def vet_settings(**changes: Any) -> Settings:
    roles = SETTINGS.llm.roles
    vet = roles.vet.model_copy(update=changes)
    return llm_settings(roles=roles.model_copy(update={"vet": vet}))


async def b_rows(run: DayRun, symbol: str) -> list[sqlite3.Row]:
    return await run.rows(
        "SELECT d.reason_code, d.detail, d.llm_call_id, d.decided_at FROM decisions d "
        "JOIN signals s ON s.id = d.signal_id WHERE d.book = 'B' AND s.symbol = ? ORDER BY d.id",
        (symbol,),
    )


async def test_a_premarket_veto_frees_book_b_s_cash_for_cap(tmp_path: Path) -> None:
    """S§5.2: the brief vetoes LOSE, so Book B never takes it. With one entry and $509.68 of
    settled cash left after WIN, Book B takes CAP at 11:00:50, which Book A, two entries in,
    can't (R-007)."""
    run = await ab_day(tmp_path)
    run.verdicts = {"LOSE": "veto"}
    await run.run()
    assert await run.decisions("A") == NEUTRAL
    b = await run.decisions("B")
    assert (b["WIN"], b["LOSE"], b["CAP"]) == ("taken", "vetoed", "taken")
    (lose,) = await b_rows(run, "LOSE")
    assert lose["llm_call_id"] == "20261013-premarket-01-1"
    assert "premarket veto" in lose["detail"]
    trades = await run.trades("B")
    assert set(trades) == {"WIN", "CAP"}
    assert trades["CAP"][:2] == (14, "35.43")
    await run.close()


def win_jumps_at_0936() -> SyntheticDay:
    """The standard day, but WIN's 09:36 minute opens at 20.70, past its 20.47 chase cap."""
    day = standard_day()
    win = path("WIN", open_="20.00", prev_close="19.80", or_volume_per_min=16_000,
               keys=[("09:30", "20.10"), ("09:34", "20.40"), ("09:35", "20.41"),
                     ("12:00", "22.00")],
               gaps={"09:36": "20.70"})  # fmt: skip
    day.symbols = [win if s.symbol == "WIN" else s for s in day.symbols]
    return day


async def test_a_trigger_past_its_cap_when_its_verdict_comes_is_chased_after_wait(
    tmp_path: Path,
) -> None:
    """WIN triggers at 09:35:50 and waits for the vet; by its answer, at 09:36:01.5, WIN trades
    at 20.70. The chase guard runs once the verdict is in (S§5.4): ``chased_after_wait``."""
    run = await DayRun(tmp_path, win_jumps_at_0936(), start=time(8, 55), books=("A", "B")).open()
    run.llm_delay["vet"] = 58.5  # from 09:35:03: after the jump, before the 09:36:03 deadline
    await run.run()
    assert (await run.decisions("A"))["WIN"] == "taken"  # at 09:35:50, before the jump
    (win,) = await b_rows(run, "WIN")
    assert win["reason_code"] == "chased_after_wait"
    assert win["decided_at"] == at("09:36:01.500000").isoformat()  # at the answer
    assert "last 20.70 > max_chase 20.47" in win["detail"]
    assert win["llm_call_id"] == "20261013-vet-01-1"
    assert "WIN" not in await run.trades("B")
    await run.close()


def invalid_until_degraded(seen: Seen) -> Reply:
    """The brief's first try isn't JSON, and its repair answers well. The vet's try isn't JSON
    and its repair doesn't fit the schema: three failed attempts, ``llm_failures_degrade_after``."""
    pack = seen.pack()
    if pack["role"] == "review":  # the morning's failures are the point; the review answers
        return Reply(envelope=envelope(answer_for(pack)), delay_s=30.0)
    if not seen.is_repair:
        return Reply(stdout=b"not json", delay_s=1.0)
    if pack["role"] == "premarket":
        return Reply(envelope=envelope(answer_for(pack)), delay_s=10.0)
    return Reply(envelope=envelope({"pack_id": pack["pack_id"], "assessments": "none"}),
                 delay_s=1.0)  # fmt: skip


async def test_an_answer_still_invalid_after_its_repair_degrades_book_b_alone(
    tmp_path: Path,
) -> None:
    run = await ab_day(tmp_path, llm_script=invalid_until_degraded)
    await run.run()
    calls = await run.rows(f"SELECT id, ok, failure {MORNING}")
    assert [tuple(r) for r in calls] == [
        ("20261013-premarket-01-1", 0, "invalid_json"), ("20261013-premarket-01-2", 1, None),
        ("20261013-vet-01-1", 0, "invalid_json"), ("20261013-vet-01-2", 0, "schema_invalid"),
    ]  # fmt: skip
    assert await run.decisions("B") == {
        "WIN": "llm_degraded", "LOSE": "llm_degraded", "CHASE": "missed_at_arm",
        "GAPPER": "llm_degraded", "CAP": "llm_degraded",
    }  # fmt: skip
    assert await run.halts("B") == ["llm_degraded"]
    assert await run.trades("B") == {}
    assert [i for i in await run.incidents() if i[1].startswith("llm")] == [
        ("P3", "llm_call_failed"), ("P3", "llm_call_failed"), ("P3", "llm_call_failed"),
        ("P2", "llm_degraded"),
    ]  # fmt: skip
    assert await run.decisions("A") == NEUTRAL  # Book A's day, untouched
    assert set(await run.trades("A")) == {"WIN", "LOSE"}
    assert await run.halts("A") == []
    await run.close()


async def test_a_no_trade_day_keeps_book_b_out_and_still_vets(tmp_path: Path) -> None:
    """S§5.2-5.3: ``no_trade`` comes before any verdict; the vet still runs, for the record."""
    run = await ab_day(tmp_path)
    run.day_mode = "no_trade"
    await run.run()
    assert await run.decisions("B") == {
        "WIN": "day_mode_no_trade", "LOSE": "day_mode_no_trade", "CHASE": "missed_at_arm",
        "GAPPER": "day_mode_no_trade", "CAP": "day_mode_no_trade",
    }  # fmt: skip
    for symbol in ("WIN", "CAP"):  # each rests on the brief that set the day mode
        assert [r["llm_call_id"] for r in await b_rows(run, symbol)] == ["20261013-premarket-01-1"]
    calls = await run.rows(f"SELECT id, ok {MORNING}")
    assert [tuple(r) for r in calls] == [("20261013-premarket-01-1", 1), ("20261013-vet-01-1", 1)]
    assert await run.trades("B") == {}
    assert await run.decisions("A") == NEUTRAL
    await run.close()


async def test_a_reduced_day_halves_book_b_s_entries(tmp_path: Path) -> None:
    """S§3.5, S§5.1: the multiplier 0.5 halves the notional cap that sizes both entries."""
    run = await ab_day(tmp_path)
    run.day_mode = "reduced"
    await run.run()
    assert await run.decisions("B") == NEUTRAL
    a, b = await run.trades("A"), await run.trades("B")
    assert {s: t[0] for s, t in a.items()} == {"WIN": 24, "LOSE": 16}
    assert {s: t[0] for s, t in b.items()} == {"WIN": 12, "LOSE": 8}
    (win,) = await b_rows(run, "WIN")
    assert "day mode reduced x0.5" in win["detail"]
    await run.close()


def brief_not_json(seen: Seen) -> Reply:
    pack = seen.pack()
    if pack["role"] == "premarket":
        return Reply(stdout=b"not json", delay_s=1.0)
    return Reply(envelope=envelope(answer_for(pack)), delay_s=5.0)


async def test_a_failed_brief_sends_every_armed_long_to_the_vet(tmp_path: Path) -> None:
    """S§5.2: no brief by its deadline: the day mode is normal, and the vet judges every signal,
    the brief's own candidates among them."""
    run = await ab_day(tmp_path, llm_script=brief_not_json)
    await run.run()
    calls = await run.rows(f"SELECT id, ok, candidates_json {MORNING}")
    picks = '["GAPPER", "LOSE", "SHORT"]'
    assert [tuple(r) for r in calls] == [
        ("20261013-premarket-01-1", 0, picks), ("20261013-premarket-01-2", 0, picks),
        ("20261013-vet-01-1", 1, ALL_ARMED),
    ]  # fmt: skip
    modes = await run.rows("SELECT mode, source FROM day_modes")
    assert [tuple(r) for r in modes] == [("normal", "default")]
    assert await run.decisions("B") == NEUTRAL
    assert [i for i in await run.incidents() if i[1].startswith(("llm", "brief"))] == [
        ("P3", "llm_call_failed"), ("P3", "llm_call_failed"),
    ]  # fmt: skip
    await run.close()


async def test_a_start_after_the_brief_s_latest_start_skips_the_morning(tmp_path: Path) -> None:
    """Started at 09:28, past 09:27: no scan, no brief (a P3 ``brief_missed``), the default day
    mode, and the vet for every armed long."""
    run = await DayRun(tmp_path, standard_day(), start=time(9, 28), books=("A", "B")).open()
    await run.run()
    calls = await run.rows(f"SELECT id, candidates_json {MORNING}")
    assert [tuple(r) for r in calls] == [("20261013-vet-01-1", ALL_ARMED)]
    modes = await run.rows("SELECT mode, source FROM day_modes")
    assert [tuple(r) for r in modes] == [("normal", "default")]
    assert ("P3", "brief_missed") in await run.incidents()
    assert run.edgar.asked == ["WIN", "LOSE", "GAPPER", "EXP", "CAP"]  # the vet's only
    assert [m for t, m in run.market.asked if t < at("09:30:00") and m == "snapshots"] == []
    assert await run.decisions("B") == NEUTRAL
    await run.close()


async def test_a_restart_mid_vet_never_vets_again_what_book_b_decided(tmp_path: Path) -> None:
    """With a 30 s wait, WIN (09:35:50) is ``vet_timeout`` while the vet is still out. The
    engine dies at 09:35:55, abandoning that call. The restart vets EXP and CAP again, never
    WIN: Book B decided it (D-069)."""
    run = await ab_day(tmp_path)
    settings = vet_settings(max_wait_s=30)
    run.llm_delay["vet"] = 55.0
    assert await run.run(run.deps(settings=settings), until="09:35:55") is None
    assert await run.decisions("B") == {"WIN": "vet_timeout", "CHASE": "missed_at_arm"}
    await run.wait_until("09:36:30")
    run.new_hub()
    run.llm_delay["vet"] = 5.0
    await run.run(run.deps(settings=settings))
    calls = await run.rows("SELECT id, failure, candidates_json FROM llm_calls WHERE role = 'vet' "
                           "ORDER BY id")  # fmt: skip
    assert [tuple(r) for r in calls] == [
        ("20261013-vet-01-1", "abandoned", '["WIN", "EXP", "CAP"]'),
        ("20261013-vet-02-1", None, '["EXP", "CAP"]'),
    ]
    assert [r["reason_code"] for r in await b_rows(run, "WIN")] == ["vet_timeout"]
    await run.close()


async def test_a_restart_after_the_vet_asks_nothing_again(tmp_path: Path) -> None:
    """The brief ended and the vet answered: a restart at 09:45:30 makes no call and asks
    EDGAR nothing, and Book B's day ends as it would have."""
    run = await ab_day(tmp_path)
    assert await run.run(until="09:45:00") is None
    calls = [r["id"] for r in await run.rows(f"SELECT id {MORNING}")]
    assert calls == ["20261013-premarket-01-1", "20261013-vet-01-1"]
    asked = list(run.edgar.asked)
    await run.wait_until("09:45:30")
    run.new_hub()
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "DONE"
    assert [r["id"] for r in await run.rows(f"SELECT id {MORNING}")] == calls
    assert run.edgar.asked == asked
    assert await run.decisions("B") == NEUTRAL
    await run.close()


async def test_the_day_s_call_cap_fails_closed(tmp_path: Path) -> None:
    """``max_calls_per_day`` 1: the brief spends it, and the vet is refused before any attempt
    (a P3). Its signals then have no verdict: ``llm_unavailable``, never an unvetted entry. LOSE
    rests on the brief's verdict."""
    run = await ab_day(tmp_path)
    await run.run(run.deps(settings=llm_settings(max_calls_per_day=1)))
    calls = [r["id"] for r in await run.rows(f"SELECT id {MORNING}")]
    assert calls == ["20261013-premarket-01-1"]
    assert await run.decisions("B") == {**NEUTRAL, "WIN": "llm_unavailable",
                                        "CAP": "llm_unavailable"}  # fmt: skip
    assert ("P3", "llm_call_refused") in await run.incidents()
    assert set(await run.trades("B")) == {"LOSE"}
    await run.close()


async def test_an_a_only_day_asks_for_nothing_of_book_b_s(tmp_path: Path) -> None:
    """The standard day's pre-market prints, news and filings are Book B's: an A-only day,
    even one booted at 08:55, makes no LLM call and no pre-market read. So Book A's golden day
    stays byte-identical (test_golden.py)."""
    run = await DayRun(tmp_path, standard_day(), start=time(8, 55)).open()
    await run.run()
    assert await run.rows("SELECT * FROM llm_calls") == []
    assert (run.news.started, run.news.reads, run.edgar.asked) == ([], 0, [])
    assert [m for t, m in run.market.asked if t < at("09:30:00") and m == "snapshots"] == []
    assert await run.decisions("A") == NEUTRAL
    await run.close()


# ---------------------------------------------------------------- the review (P4.10)


def reviewed_day(seen: Seen) -> Reply:
    """The vet vetoes CAP; the review answers with a process issue, a note, a lesson proposal,
    and a review of EXP, which isn't a target."""
    pack = seen.pack()
    if pack["role"] != "review":
        out = answer_for(pack)
        out["assessments"] = [{**a, "verdict": "veto" if a["symbol"] == "CAP" else "neutral"}
                              for a in out["assessments"]]  # fmt: skip
        return Reply(envelope=envelope(out), delay_s=5.0)
    out = answer_for(pack)
    out["summary"] = "CAP was vetoed on its 8-K."
    out["signal_reviews"].append({**out["signal_reviews"][0], "signal_id": "20261013-EXP-orb5"})
    out["process_issues"] = [
        {"existing_issue_id": None, "severity": "warn", "component": "vet",
         "description": "The vet judged CAP before its 8-K was read."}
    ]  # fmt: skip
    out["human_notes"] = [{"existing_note_id": None, "note": "Consider a later vet for CAP."}]
    out["lesson_proposals"] = [
        {"statement": "Small gaps fade.", "rationale": "One day of evidence.",
         "evidence_query": {"filters": [{"field": "gap_pct", "op": "lt", "value": 1.5}],
                            "compare_to": "complement", "metric": "shadow_r"},
         "claimed_direction": "subset_worse", "suggested_use": "caution_hint"}
    ]  # fmt: skip
    return Reply(envelope=envelope(out), delay_s=40.0)


async def test_the_16_15_review_covers_the_targets_and_stores_every_part(tmp_path: Path) -> None:
    """S§6.1, S§6.5: code picks the targets (the trades, Book B's vetoes, the largest untraded
    moves), each stamped with its decision; the answer goes to its tables, the review of a
    non-target dropped."""
    run = await ab_day(tmp_path, llm_script=reviewed_day)
    await run.run()
    assert (await run.decisions("B"))["CAP"] == "vetoed"
    pack = json.loads((tmp_path / "llm" / "2026-10-13" / "20261013-review-01-1.input.json")
                      .read_text("utf-8"))  # fmt: skip
    assert [(t["symbol"], t["selected_for"], t["decision_basis"], t["decision_at"][11:19])
            for t in pack["review_targets"]] == [
        ("WIN", "traded", "vet_pack", "09:35:03"), ("LOSE", "traded", "premarket_pack", "09:05:00"),
        ("CAP", "b_vetoed", "vet_pack", "09:35:03"),
        ("GAPPER", "notable", "premarket_pack", "09:05:00"),
        ("CHASE", "notable", "armed", "09:35:03"),
    ]  # fmt: skip
    gapper = next(t for t in pack["review_targets"] if t["symbol"] == "GAPPER")
    assert gapper["suspicious_news"] is True  # its injection headline, before the brief
    assert [n["published"] for n in gapper["news"]] == ["before_decision"]
    assert all(t["spy_move_pct"] is not None for t in pack["review_targets"])
    assert pack["books"]["B"]["trades"] == 2
    review = await run.rows("SELECT status, summary, targets_json FROM daily_reviews")
    assert [(r["status"], r["summary"]) for r in review] == [("ok", "CAP was vetoed on its 8-K.")]
    stored = await run.rows("SELECT signal_id FROM signal_reviews ORDER BY signal_id")
    symbols = ("CAP", "CHASE", "GAPPER", "LOSE", "WIN")
    assert [r["signal_id"] for r in stored] == [f"20261013-{s}-orb5" for s in symbols]
    assert [tuple(r) for r in await run.rows(
        "SELECT kind, item_id, text FROM review_mentions ORDER BY id")] == [
        ("issue", 1, "The vet judged CAP before its 8-K was read."),
        ("note", 1, "Consider a later vet for CAP."),
    ]  # fmt: skip
    lessons = await run.rows("SELECT id, status, source_date FROM lessons")
    assert [tuple(r) for r in lessons] == [("L-001", "proposed", "2026-10-13")]
    md = (tmp_path / "reports" / "2026-10-13.md").read_text(encoding="utf-8")
    assert "- The review (ok): `CAP was vetoed on its 8-K.`" in md
    assert "| issue | 1 | new | warn | `The vet judged CAP before its 8-K was read.` |" in md
    await run.close()


async def test_a_review_that_times_out_is_recorded_missing(tmp_path: Path) -> None:
    """An answer still out at the review's timeout (600 s, inside its 16:29 cutoff) is recorded
    as ``missing`` with why, and never degrades Book B: the day's entries are over."""
    run = await ab_day(tmp_path)
    run.llm_delay["review"] = 900.0
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "DONE"
    row = await run.rows("SELECT status, failure, llm_call_id FROM daily_reviews")
    assert [tuple(r) for r in row] == [("missing", "timeout", "20261013-review-01-1")]
    assert await run.halts("B") == []
    md = (tmp_path / "reports" / "2026-10-13.md").read_text(encoding="utf-8")
    assert "- The review (missing): timeout" in md
    await run.close()


async def test_a_restart_after_the_review_never_asks_again(tmp_path: Path) -> None:
    """Plan decision 11: the answer and the day's review marker commit together, so a restart
    after the review makes no call."""
    run = await ab_day(tmp_path)
    assert await run.run(until="16:20:00") is None
    assert [r["id"] for r in await run.rows("SELECT id FROM llm_calls WHERE role = 'review'")] == [
        "20261013-review-01-1"
    ]
    await run.wait_until("16:21:00")
    run.new_hub()
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "DONE"
    calls = await run.rows("SELECT id FROM llm_calls WHERE role = 'review'")
    assert [r["id"] for r in calls] == ["20261013-review-01-1"]
    assert [r["status"] for r in await run.rows("SELECT status FROM daily_reviews")] == ["ok"]
    await run.close()


# A recorded A+B day that replays the same is the A+B golden day's (test_golden.py).


async def test_a_replay_names_an_llm_answer_it_lacks(tmp_path: Path) -> None:
    run = await ab_day(tmp_path, recording=True)
    await run.run()
    await run.close()
    llm = tmp_path / "recordings" / "2026-10-13" / LLM_FILE
    kept = [r for r in read_jsonl(llm) if not (r["kind"] == "attempt" and r["role"] == "vet")]
    llm.unlink()
    recorder = Recorder(tmp_path / "recordings", DAY)
    for record in kept:
        recorder.append(LLM_FILE, record)
    recorder.close()
    report = await replay_day(recordings=tmp_path / "recordings", day=DAY,
                              out=tmp_path / "replay", settings=SETTINGS, risk=RISK,
                              recorded_db=tmp_path / "trader.db")  # fmt: skip
    # Book B then never traded, and the recorded broker answers it got no longer fit: the
    # replay diverges from there on, as any replay that lost a recorded answer does.
    assert "llm: a vet attempt the recording doesn't have" in report.divergences
    assert not report.matches
    assert "decision replayed but not recorded: 20261013-WIN-orb5 B 0 llm_unavailable" in (
        report.differences
    )


async def test_a_recorded_answer_the_replay_never_asked_for_is_named(tmp_path: Path) -> None:
    """On a day the engine ran once, an attempt the replay didn't make is one the live engine
    made and the replay lost: a divergence. After a restart (``boot-2.json``) the recording
    holds the restart's attempts too, and the replay covers the first run: only a note."""
    run = await ab_day(tmp_path, recording=True)
    await run.run()
    await run.close()
    folder = tmp_path / "recordings" / "2026-10-13"
    records = list(read_jsonl(folder / LLM_FILE))
    extra = next(r for r in reversed(records) if r["kind"] == "attempt")
    (folder / LLM_FILE).unlink()
    recorder = Recorder(tmp_path / "recordings", DAY)
    for record in [*records, extra]:
        recorder.append(LLM_FILE, record)
    recorder.close()
    left = "the replay left 1 recorded LLM attempts and 0 packs unused"

    async def replay(out: str) -> Any:
        return await replay_day(recordings=tmp_path / "recordings", day=DAY, out=tmp_path / out,
                                settings=SETTINGS, risk=RISK,
                                recorded_db=tmp_path / "trader.db")  # fmt: skip

    once = await replay("replay")
    assert f"llm: {left}" in once.divergences
    assert left not in once.notes
    (folder / "boot-2.json").write_bytes((folder / "boot.json").read_bytes())
    restarted = await replay("replay-2")
    assert left in restarted.notes
    assert f"llm: {left}" not in restarted.divergences


async def test_book_b_off_leaves_a_day_of_book_a_alone(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), books=("A",)).open()
    await run.run(run.deps(book_b_off="--no-llm"))
    assert await run.rows("SELECT * FROM llm_calls") == []
    boot = json.loads((tmp_path / "recordings" / "2026-10-13" / "boot.json").read_text("utf-8"))
    assert boot["llm"] == {"book_b": "off: --no-llm"}
    await run.close()


async def test_book_b_needs_its_llm(tmp_path: Path) -> None:
    run = await ab_day(tmp_path)
    with pytest.raises(ValueError, match="Book B needs its LLM runner"):
        await run.run(run.deps(llm=None))
    await run.close()


async def test_the_news_starts_after_the_stock_stream_and_stops_with_the_day(
    tmp_path: Path,
) -> None:
    """Q2: the stock stream takes the first connection; and boot never waits for the news's
    backfill (D-069)."""
    run = await ab_day(tmp_path)
    hub_running: list[bool] = []
    run.news.on_start = lambda: hub_running.append(run.hub._task is not None)
    deps = run.deps()
    assert deps.llm is not None
    print_ = Fingerprint("f1ng3rpr1nt", {})
    await run.run(replace(deps, llm=replace(deps.llm, fingerprint=print_)))
    assert (run.news.started, hub_running, run.news.stopped) == ([timedelta(hours=24)], [True], 1)
    boot = json.loads((tmp_path / "recordings" / "2026-10-13" / "boot.json").read_text("utf-8"))
    assert boot["llm"]["fingerprint"] == "f1ng3rpr1nt"
    # Book B's packs carry its own start-of-day E
    vet = json.loads((tmp_path / "llm" / "2026-10-13" / "20261013-vet-01-1.input.json").read_text())
    assert vet["book_b"]["equity_usd"] == 1000.0
    await run.close()


async def test_a_restart_owes_book_b_the_trigger_it_was_waiting_on(tmp_path: Path) -> None:
    """D-069: Book A entered WIN while Book B waited for the vet, journaling nothing; then a
    crash. The restart re-serves WIN to Book B alone, pending again from 09:36:30; the re-vet
    answers at 09:37:03, two seconds after WIN's 09:37:01 print, and Book B enters."""
    run = await ab_day(tmp_path)
    run.llm_delay["vet"] = 55.0
    assert await run.run(until="09:35:55") is None  # WIN triggered at 09:35:50: B waits
    assert await run.decisions("B") == {"CHASE": "missed_at_arm"}
    await run.wait_until("09:36:30")
    run.new_hub()
    run.llm_delay["vet"] = 33.0  # a fresh print to price it by (R-016's 15 s)
    await run.run()
    (win,) = await b_rows(run, "WIN")
    assert (win["reason_code"], win["decided_at"]) == (
        "entered_after_wait", at("09:37:03").isoformat()
    )  # fmt: skip
    assert win["llm_call_id"] == "20261013-vet-02-1"
    assert "WIN" in await run.trades("B")
    revet = await run.rows("SELECT id FROM llm_calls WHERE role = 'vet' ORDER BY id")
    assert [r["id"] for r in revet] == ["20261013-vet-01-1", "20261013-vet-02-1"]  # re-vetted
    a_win = await run.rows(
        "SELECT d.reason_code FROM decisions d JOIN signals s ON s.id = d.signal_id "
        "WHERE d.book = 'A' AND s.symbol = 'WIN' ORDER BY d.id"
    )
    assert [r["reason_code"] for r in a_win] == ["taken"]  # Book A's order stands: never again
    await run.close()


async def test_book_b_s_boot_failing_leaves_book_a_s_day_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await ab_day(tmp_path)

    async def broken(self: VerdictStore) -> None:
        raise sqlite3.DatabaseError("malformed")

    monkeypatch.setattr(VerdictStore, "load", broken)
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "DONE"
    assert (await run.decisions("A"))["WIN"] == "taken"
    assert set((await run.decisions("B")).values()) <= {"llm_degraded", "missed_at_arm"}
    assert await run.halts("B") == ["llm_degraded"]
    assert ("P2", "llm_job_failed") in await run.incidents()
    assert await run.rows("SELECT * FROM llm_calls") == []  # its jobs asked nothing
    stats = await run.rows("SELECT book FROM daily_stats ORDER BY book")
    assert [r["book"] for r in stats] == ["A", "B"]  # Book B's account still had its EOD
    await run.close()


async def test_the_review_runs_after_a_shadow_job_that_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await ab_day(tmp_path)
    reviewed: list[str] = []

    async def failing_shadow(self: DayOrchestrator) -> None:
        raise RuntimeError("no SIP bars")

    async def review(self: BookBJobs) -> None:
        reviewed.append(self._clock.now().strftime("%H:%M"))

    monkeypatch.setattr(DayOrchestrator, "_shadow", failing_shadow)
    monkeypatch.setattr(BookBJobs, "review", review)
    outcome = await run.run()
    assert outcome is not None
    assert outcome.state == "REPORT"  # REVIEW failed: the day is left for a rerun
    assert reviewed == ["16:12"]  # Book B's review job still ran, after the shadow job
    await run.close()


async def test_a_day_stopped_during_the_shadow_job_never_starts_the_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shutdown (a crash, Ctrl-C) isn't a failed shadow job: it doesn't wait for the review,
    which would sleep to 16:15 and call the model while the engine stops (D-069)."""
    run = await ab_day(tmp_path)
    reviewed: list[str] = []

    async def slow_shadow(self: DayOrchestrator) -> None:
        await self._clock.sleep_until(at("16:14:00"))

    async def review(self: BookBJobs) -> None:
        reviewed.append(self._clock.now().strftime("%H:%M:%S"))

    monkeypatch.setattr(DayOrchestrator, "_shadow", slow_shadow)
    monkeypatch.setattr(BookBJobs, "review", review)
    assert await run.run(until="16:13:00") is None
    assert reviewed == []
    await run.close()


async def test_a_day_book_b_was_kept_off_says_why_in_its_report(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), books=("A",)).open()
    await run.run(run.deps(book_b_off="--no-llm"))
    md = (tmp_path / "reports" / "2026-10-13.md").read_text(encoding="utf-8")
    assert "- LLM calls: none (Book B off: --no-llm)" in md
    report = await build_daily(run.journal, DAY, books=("A",), mode="paper", session=None,
                               now=at("17:00:00"))  # regenerated: from the day's note  # fmt: skip
    assert report.book_b_off == "--no-llm"
    await run.close()


# ---------------------------------------------------------------- the worker and the stop


class Store:
    """The store's version and its wait, for the worker (a stand-in for ``VerdictStore``)."""

    def __init__(self) -> None:
        self.version = 0
        self._changed = asyncio.Event()

    def bump(self) -> None:
        self.version += 1
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait_for_change(self, version: int) -> int:
        while self.version == version:
            await self._changed.wait()
        return self.version


class Scripted:
    """A Book B stand-in: each signal's answers in turn (``defer``, a reason code, or an
    exception to raise), recording every ask."""

    name = "B"

    def __init__(self, answers: dict[str, list[Any]], until: datetime) -> None:
        self.answers, self.until = answers, until
        self.asked: list[tuple[str, bool]] = []

    async def on_trigger(
        self, ev: TriggerEvent, sig: Signal, *, waited: bool = False
    ) -> BookDecision | Defer:
        self.asked.append((sig.symbol, waited))
        answer = self.answers[sig.symbol].pop(0)
        if isinstance(answer, Exception):
            raise answer
        if answer == "defer":
            return Defer(until=self.until)
        return BookDecision(book="B", signal_id=sig.id, take=False, reason=answer)


class Worker:
    """Book B's worker (``DayOrchestrator._work``) on a scripted book, a queue and a store."""

    def __init__(self, run: DayRun, book: Scripted, *symbols: str) -> None:
        self.run, self.book = run, book
        self.orch = DayOrchestrator(run.deps())
        self.store = Store()
        self.queue: asyncio.Queue[TriggerEvent] = asyncio.Queue()
        self.b = _Book(name="B", broker=run.sims["A"], records=None, service=None,  # type: ignore[arg-type]
                       policy=book, equity_sod=D(1000), store=self.store)  # type: ignore[arg-type]  # fmt: skip
        self.sigs = {s: _signal(s, rank) for rank, s in enumerate(symbols, 1)}
        self.orch._signals = {s.id: s for s in self.sigs.values()}
        self.orch._books = {"B": self.b}

    def trigger(self, symbol: str) -> None:
        self.queue.put_nowait(_event(self.sigs[symbol]))

    async def at(self, hhmmss: str) -> None:
        await self.run.clock.sleep_until(at(hhmmss))


async def drive_worker(w: Worker, steps: Any) -> None:
    async def job() -> None:
        w.b.worker = asyncio.ensure_future(w.orch._work(w.b, w.queue))
        await steps(w)
        w.b.worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await w.b.worker

    await drive(w.run.clock, job(), watched=[w.run.journal])


async def test_the_worker_asks_parked_and_new_triggers_together_in_rank_order(
    tmp_path: Path,
) -> None:
    run = await DayRun(tmp_path, standard_day(), start=time(10, 0)).open()
    book = Scripted({"AAA": ["defer", "vetoed"], "BBB": ["taken"]}, until=at("10:01:00"))
    w = Worker(run, book, "AAA", "BBB")

    async def steps(w: Worker) -> None:
        w.trigger("AAA")
        await w.at("10:00:05")
        w.trigger("BBB")  # a new trigger and a change in the store, together
        w.store.bump()
        await w.at("10:00:10")

    await drive_worker(w, steps)
    assert book.asked == [("AAA", False), ("AAA", True), ("BBB", False)]
    assert w.b.parked == {}
    await run.close()


async def test_a_parked_trigger_is_asked_again_at_its_deadline(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), start=time(10, 0)).open()
    book = Scripted({"AAA": ["defer", "vet_timeout"]}, until=at("10:01:00"))
    w = Worker(run, book, "AAA")

    async def steps(w: Worker) -> None:
        w.trigger("AAA")
        await w.at("10:00:02")
        w.trigger("AAA")  # the same signal again while it waits: never asked as new
        await w.at("10:00:30")
        assert book.asked == [("AAA", False)]
        await w.at("10:01:05")

    await drive_worker(w, steps)
    assert book.asked == [("AAA", False), ("AAA", True)]  # at 10:01:00, its deadline
    await run.close()


async def test_a_trigger_whose_ask_raised_is_not_parked_any_more(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day(), start=time(10, 0)).open()
    book = Scripted({"AAA": ["defer", RuntimeError("a bug"), "taken"]}, until=at("10:05:00"))
    w = Worker(run, book, "AAA")

    async def steps(w: Worker) -> None:
        w.trigger("AAA")
        await w.at("10:00:05")
        w.store.bump()  # asked again: it raises
        await w.at("10:00:10")
        w.store.bump()  # not parked any more: not asked
        await w.at("10:00:15")

    await drive_worker(w, steps)
    assert book.asked == [("AAA", False), ("AAA", True)]
    assert ("P2", "trigger_failed") in await run.incidents()
    await run.close()


async def test_the_stop_settles_every_parked_trigger_in_rank_order(tmp_path: Path) -> None:
    run = await DayRun(tmp_path, standard_day()).open()
    book = Scripted({"AAA": ["vet_timeout"], "BBB": ["vet_timeout"], "CCC": ["defer"]},
                    until=at("15:30:00"))  # fmt: skip
    w = Worker(run, book, "AAA", "BBB", "CCC")
    for symbol in ("CCC", "BBB", "AAA"):  # parked out of rank order
        w.b.parked[w.sigs[symbol].id] = (_event(w.sigs[symbol]), at("15:30:00"))
    w.b.worker = asyncio.ensure_future(asyncio.sleep(3600))
    await w.orch._stop_trading()
    assert book.asked == [("AAA", True), ("BBB", True), ("CCC", True)]  # by rank, waited
    assert w.b.parked == {}  # CCC still deferred: cleared, left undecided for EOD
    await run.close()


def _signal(symbol: str, rank: int) -> Signal:
    return Signal(id=f"20261013-{symbol}-orb5", date=DAY, symbol=symbol, direction="long",
                  status="triggered", rel_vol=2.0, rel_vol_rank=rank, or_open=D(10),
                  or_high=D(11), or_low=D(9), or_close=D(10), or_volume=1, atr14=D(1),
                  adv14=1, prev_close=D(10), gap_pct=0.0, trigger_price=D("11.01"),
                  stop_price=D("10.91"), max_chase_price=D("11.03"))  # fmt: skip


def _event(sig: Signal) -> TriggerEvent:
    return TriggerEvent(signal_id=sig.id, symbol=sig.symbol, rank=sig.rel_vol_rank,
                        ts=at("09:40:00"), received_at=at("09:40:00"), price=D("11.01"),
                        source="stream_trade")  # fmt: skip
