"""The post-close review's content (P4.10; S§6.5-6.6; DECISIONS D-071): targets, decision times,
news stamps, storing an answer with its links, history and follow-ups."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fixtures.exec_harness import SETTINGS
from trader.cli import app
from trader.core.clock import ET, SimClock
from trader.core.models import NewsItem, Signal
from trader.llm.client import LLMResult
from trader.llm.schemas import (
    DailyReview,
    EvidenceFilter,
    EvidenceQuery,
    HumanNote,
    LessonProposal,
    ProcessIssue,
    SignalReview,
    SymbolAssessment,
)
from trader.memory.journal import Journal, open_database
from trader.memory.reviews import (
    DailyReviewWork,
    DaySignal,
    FollowupError,
    VerdictRow,
    decision_point,
    epoch_stats,
    followups,
    persist_review,
    read_history,
    record_missing,
    select_targets,
    set_followup,
    target_news,
)
from trader.memory.shadow import ShadowOutcome
from trader.memory.trades import TradeRow
from trader.strategy.orb_sip import insert_signals

D = Decimal
DAY = date(2026, 10, 13)


def at(hhmm: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), ET)


def sig(
    symbol: str,
    rank: int,
    *,
    day: date = DAY,
    direction: str = "long",
    triggered: str | None = "09:41",
    armed: str | None = "09:35:03",
) -> Signal:
    return Signal(
        id=f"{day:%Y%m%d}-{symbol}-orb5", date=day, symbol=symbol, direction=direction,  # type: ignore[arg-type]
        status="triggered" if triggered else "armed", rel_vol=9.0 - rank / 10,
        rel_vol_rank=rank, or_open=D("12.40"), or_high=D("12.71"), or_low=D("12.30"),
        or_close=D("12.65"), or_volume=50_000, atr14=D("0.80"), adv14=2_000_000,
        prev_close=D("12.00"), gap_pct=3.3, trigger_price=D("12.72"), stop_price=D("12.64"),
        max_chase_price=D("12.75"),
        armed_at=at(armed, day) if armed else None,
        triggered_at=at(triggered, day) if triggered else None,
    )  # fmt: skip


def shadow(r: float | None, trigger: str | None = "09:41") -> ShadowOutcome:
    if trigger is None:
        return ShadowOutcome(direction="long", exit_reason="no_trigger")
    return ShadowOutcome(direction="long", exit_reason="flatten", trigger_ts=at(trigger),
                         shadow_r=r)  # fmt: skip


def trade(s: Signal, book: str) -> TradeRow:
    return TradeRow(
        id=f"{book}-{s.id}",
        book=book,  # type: ignore[arg-type]
        signal_id=s.id,
        symbol=s.symbol,
        qty=10,
        entry_ts=at("09:41"),
        entry_price=D("12.74"),
        stop_price=D("12.64"),
        exit_ts=None,
        exit_price=None,
        exit_reason=None,
        pnl=None,
        r_multiple=None,
        fees_est=D(0),
        entry_slippage=None,
        entry_delay_ms=None,
        eval_epoch=0,
    )


def verdict(
    symbol: str, word: str, source: str = "vet", sent: str | None = "09:35:03"
) -> VerdictRow:
    assessment = SymbolAssessment.model_validate(
        {"symbol": symbol, "catalyst_type": "earnings", "catalyst_fresh": True,
         "catalyst_confirmed": True, "risk_flags": [], "verdict": word, "confidence": 0.7,
         "reason": "r"}
    )  # fmt: skip
    return VerdictRow(
        assessment=assessment,
        source=source,  # type: ignore[arg-type]
        llm_call_id=f"20261013-{source}-01-1",
        sent_at=at(sent) if sent else None,
    )


# ---------------------------------------------------------------- the targets (S§6.5)


def test_the_targets_are_every_trade_every_b_veto_and_the_largest_untraded_moves() -> None:
    win, lose, veto, sip, small, mid, big, short, quiet, far, idle = (
        sig("WIN", 1), sig("LOSE", 2), sig("VETO", 3), sig("SIP", 4, triggered=None),
        sig("SMALL", 5), sig("MID", 6), sig("BIG", 7), sig("SHRT", 8, direction="shadow_short"),
        sig("QUIET", 9, triggered=None), sig("FAR", 25), sig("IDLE", 10, triggered=None),
    )  # fmt: skip
    day = [
        DaySignal(win, shadow(2.6), decisions={"A": "taken", "B": "taken"},
                  trades={"A": trade(win, "A"), "B": trade(win, "B")}),
        # traded by Book A and vetoed by Book B: a trade first
        DaySignal(lose, shadow(-1.0), decisions={"A": "taken", "B": "vetoed"},
                  trades={"A": trade(lose, "A")}, verdict=verdict("LOSE", "veto")),
        DaySignal(veto, shadow(1.2), decisions={"A": "capacity_entries", "B": "vetoed"},
                  verdict=verdict("VETO", "veto")),
        # only the SIP bars reached the trigger: no decision from Book B, but its veto counts
        DaySignal(sip, shadow(-0.4), sip_only=True, verdict=verdict("SIP", "veto")),
        DaySignal(big, shadow(-3.0), decisions={"A": "chased", "B": "chased"}),
        DaySignal(mid, shadow(1.5), decisions={"A": "capacity_entries", "B": "chased"}),
        DaySignal(small, shadow(0.2), decisions={"A": "capacity_entries", "B": "chased"}),
        DaySignal(short, shadow(5.0)),  # never a target, whatever its move
        DaySignal(quiet, shadow(None, trigger=None)),  # never triggered
        DaySignal(far, shadow(4.0), trades={"A": trade(far, "A")}),  # outside the top 20
        # vetoed, but it never triggered, live or on SIP: nothing happened to review
        DaySignal(idle, shadow(None, trigger=None), verdict=verdict("IDLE", "veto")),
    ]  # fmt: skip
    picked = select_targets(day, top_n=20, notable=2)
    assert [(p.day.signal.symbol, p.selected_for) for p in picked] == [
        ("WIN", "traded"), ("LOSE", "traded"), ("VETO", "b_vetoed"), ("SIP", "b_vetoed"),
        ("BIG", "notable"), ("MID", "notable"),  # by |shadow R|, not by rank
    ]  # fmt: skip
    assert [p.day.signal.symbol for p in select_targets(day, top_n=20, notable=5)][-3:] == [
        "BIG", "MID", "SMALL"
    ]  # fmt: skip


def test_at_most_twenty_targets_the_notable_going_first() -> None:
    day = [DaySignal(s, shadow(1.0), trades={"A": trade(s, "A")})
           for s in (sig(f"T{i:02d}", i) for i in range(1, 20))]  # fmt: skip
    day += [DaySignal(sig("VETO", 20), shadow(0.5), decisions={"B": "vetoed"}),
            DaySignal(sig("NOTE", 21), shadow(-4.0))]  # fmt: skip
    picked = select_targets(day, top_n=30, notable=5)
    assert len(picked) == 20
    assert picked[-1].day.signal.symbol == "VETO"  # the notable one went


def test_a_target_is_decided_when_the_pack_behind_b_s_verdict_was_sent() -> None:
    win, sip = sig("WIN", 1), sig("SIP", 2, triggered=None, armed=None)
    assert decision_point(DaySignal(win, verdict=verdict("WIN", "allow", "premarket", "09:05"))) \
        == (at("09:05"), "premarket_pack")  # fmt: skip
    assert decision_point(DaySignal(win, verdict=verdict("WIN", "veto"))) == (
        at("09:35:03"),
        "vet_pack",
    )
    assert decision_point(DaySignal(win)) == (at("09:41"), "trigger")  # no verdict
    assert decision_point(DaySignal(sig("ARM", 3, triggered=None))) == (at("09:35:03"), "armed")
    assert decision_point(DaySignal(sip, shadow(1.0, "10:02"))) == (at("10:02"), "trigger")
    assert decision_point(DaySignal(sip)) is None


def news(n: int, created: str, symbol: str = "WIN", day: date = DAY) -> NewsItem:
    when = at(created, day)
    return NewsItem(id=n, headline=f"headline {n}", summary="", author="", source="benzinga",
                    url=None, symbols=(symbol,), created_at=when, updated_at=when)  # fmt: skip


def test_a_target_s_news_is_stamped_against_its_decision() -> None:
    """At most 3 items from before the decision and 2 from after, nearest first; an item counts
    from when the engine could read it (D-063)."""
    yesterday = DAY - timedelta(days=1)
    stored: list[tuple[NewsItem, datetime | None]] = [
        (news(1, "08:00"), None), (news(2, "08:30"), None), (news(3, "09:00"), None),
        (news(4, "09:20"), None), (news(5, "07:00"), None),
        (news(6, "09:30"), at("09:40")),  # created before 09:35, read after: after
        (news(7, "10:00"), None), (news(8, "11:00"), None),
        (news(9, "09:10", "LOSE"), None),  # another symbol's
        (news(10, "08:00", day=yesterday), None),  # more than 24 h before the decision
        (news(11, "17:00"), None),  # after the review's as_of
    ]  # fmt: skip
    got = target_news("WIN", stored, decided=at("09:35"), as_of=at("16:15"),
                      lookback=timedelta(hours=24))  # fmt: skip
    assert [n.item.id for n in got] == [4, 3, 2, 6, 7]
    assert got[3].received_at == at("09:40")
    sparse = [(news(10, "08:00", day=yesterday), None), (news(4, "09:20"), None)]
    got = target_news("WIN", sparse, decided=at("09:35"), as_of=at("16:15"),
                      lookback=timedelta(hours=24))  # fmt: skip
    assert [n.item.id for n in got] == [4]  # room for three, but item 10 is out of the window


# ---------------------------------------------------------------- storing an answer


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_database(tmp_path / "trader.db")
    insert_signals(c, [sig("WIN", 1), sig("LOSE", 2), sig("OTHER", 3)])
    c.execute(
        "INSERT INTO llm_calls (id, date, role, logical_id, attempt, final, ok, purpose, "
        "candidates_json, started_at) VALUES ('20261013-review-01-1', '2026-10-13', 'review', "
        "'20261013-review-01', 1, 1, 1, 'trading', ?, ?)",
        (json.dumps(["20261013-WIN-orb5", "20261013-LOSE-orb5"]), at("16:15").isoformat()),
    )
    c.executescript(
        "INSERT INTO process_issues (id, first_seen, last_seen, occurrences, severity, component, "
        "description, status) VALUES (1, '2026-10-01', '2026-10-09', 3, 'warn', 'vet', "
        "'the first text', 'open'), (2, '2026-09-20', '2026-09-22', 2, 'info', 'scan', 'closed', "
        "'fixed');"
        "INSERT INTO review_notes (id, first_raised, last_raised, times_raised, note, status) "
        "VALUES (1, '2026-10-05', '2026-10-08', 2, 'raise the floor?', 'open');"
    )
    yield c
    c.close()


def review(**changes: Any) -> DailyReview:
    base: dict[str, Any] = {
        "pack_id": "review-20261013-0123456789abcdef",
        "summary": "A day.",
        "signal_reviews": [],
        "process_issues": [],
        "lesson_proposals": [],
        "human_notes": [],
    }
    return DailyReview(**(base | changes))


def signal_review(signal_id: str, why: str = "why") -> SignalReview:
    return SignalReview(
        signal_id=signal_id,
        went_well="",
        went_badly="",
        why=why,
        factors=["false_breakout", "late_entry", "false_breakout"],
        knowable_at_decision="partly",
        b_process="not_applicable",
        b_note="",
    )


def test_a_review_is_stored_part_by_part_for_its_targets_only(conn: sqlite3.Connection) -> None:
    answer = review(
        signal_reviews=[
            signal_review("20261013-WIN-orb5", "first"),
            signal_review("20261013-WIN-orb5", "second"),  # a second review of WIN: dropped
            signal_review("20261013-OTHER-orb5"),  # not a target: dropped
        ],
        lesson_proposals=[
            LessonProposal(
                statement="Gaps over 10% fade", rationale="r",
                evidence_query=EvidenceQuery(filters=[EvidenceFilter(field="gap_pct", op="gte",
                                                                     value=10.0)]),
                claimed_direction="subset_worse", suggested_use="veto_hint",
            )
        ],
    )  # fmt: skip
    persist_review(conn, answer, day=DAY, attempt_id="20261013-review-01-1", now=at("16:16"))
    row = conn.execute("SELECT * FROM daily_reviews").fetchone()
    assert (row["date"], row["status"], row["summary"], row["llm_call_id"], row["failure"]) == (
        "2026-10-13", "ok", "A day.", "20261013-review-01-1", None
    )  # fmt: skip
    assert json.loads(row["targets_json"]) == ["20261013-WIN-orb5", "20261013-LOSE-orb5"]
    reviews = conn.execute("SELECT signal_id, why, factors_json FROM signal_reviews").fetchall()
    assert [tuple(r) for r in reviews] == [
        ("20261013-WIN-orb5", "first", '["false_breakout", "late_entry"]')
    ]
    lesson = conn.execute("SELECT id, status, source_date, source_call_id, evidence_query_json "
                          "FROM lessons").fetchone()  # fmt: skip
    assert tuple(lesson)[:4] == ("L-001", "proposed", "2026-10-13", "20261013-review-01-1")
    assert json.loads(lesson[4])["filters"] == [{"field": "gap_pct", "op": "gte", "value": 10.0}]


def test_linking_bumps_an_open_item_once_and_keeps_its_first_text(
    conn: sqlite3.Connection,
) -> None:
    answer = review(
        process_issues=[
            ProcessIssue(existing_issue_id=1, severity="error", component="vet",
                         description="today's text"),
            ProcessIssue(existing_issue_id=1, severity="warn", component="vet",
                         description="again"),  # the same id twice in one review: once
            ProcessIssue(existing_issue_id=2, severity="info", component="scan",
                         description="came back"),  # closed: a new issue
            ProcessIssue(existing_issue_id=99, severity="info", component="x",
                         description="unknown id"),  # unknown: a new issue
            ProcessIssue(severity="warn", component="report", description="brand new"),
        ],
        human_notes=[HumanNote(existing_note_id=1, note="raise it now"),
                     HumanNote(existing_note_id=7, note="unknown note id")],
    )  # fmt: skip
    persist_review(conn, answer, day=DAY, attempt_id="20261013-review-01-1", now=at("16:16"))
    issues = conn.execute("SELECT id, first_seen, last_seen, occurrences, description, status "
                          "FROM process_issues ORDER BY id").fetchall()  # fmt: skip
    assert [tuple(r) for r in issues] == [
        (1, "2026-10-01", "2026-10-13", 4, "the first text", "open"),
        (2, "2026-09-20", "2026-09-22", 2, "closed", "fixed"),
        (3, "2026-10-13", "2026-10-13", 1, "came back", "open"),
        (4, "2026-10-13", "2026-10-13", 1, "unknown id", "open"),
        (5, "2026-10-13", "2026-10-13", 1, "brand new", "open"),
    ]
    notes = conn.execute("SELECT id, last_raised, times_raised, note FROM review_notes "
                         "ORDER BY id").fetchall()  # fmt: skip
    assert [tuple(r) for r in notes] == [
        (1, "2026-10-13", 3, "raise the floor?"), (2, "2026-10-13", 1, "unknown note id"),
    ]  # fmt: skip
    mentions = conn.execute("SELECT kind, item_id, severity, text FROM review_mentions "
                            "ORDER BY id").fetchall()  # fmt: skip
    assert [tuple(r) for r in mentions] == [
        ("issue", 1, "error", "today's text"), ("issue", 3, "info", "came back"),
        ("issue", 4, "info", "unknown id"), ("issue", 5, "warn", "brand new"),
        ("note", 1, None, "raise it now"), ("note", 2, None, "unknown note id"),
    ]  # fmt: skip
    assert (
        conn.execute("SELECT last_llm_call_id FROM process_issues WHERE id = 1").fetchone()[0]
        == "20261013-review-01-1"
    )


def test_a_review_that_did_not_come_is_recorded(conn: sqlite3.Connection) -> None:
    record_missing(conn, day=DAY, status="missing", failure="timeout",
                   call_id="20261013-review-01-1", now=at("16:29"))  # fmt: skip
    row = conn.execute("SELECT status, summary, failure, llm_call_id, targets_json "
                       "FROM daily_reviews").fetchone()  # fmt: skip
    assert tuple(row) == ("missing", None, "timeout", "20261013-review-01-1",
                          '["20261013-WIN-orb5", "20261013-LOSE-orb5"]')  # fmt: skip
    record_missing(conn, day=DAY, status="missing", failure="no time before 16:29", call_id=None,
                   now=at("16:28"))  # fmt: skip
    row = conn.execute("SELECT failure, llm_call_id, targets_json FROM daily_reviews").fetchone()
    assert tuple(row) == ("no time before 16:29", None, None)


async def test_an_answer_invalid_after_its_repair_is_an_invalid_review(tmp_path: Path) -> None:
    """S§6.1: ``invalid`` when an answer came and didn't hold up, ``missing`` when none came."""
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    await journal.execute(
        "INSERT INTO llm_calls (id, date, role, logical_id, attempt, final, ok, purpose, "
        "candidates_json) VALUES ('20261013-review-01-2', '2026-10-13', 'review', "
        "'20261013-review-01', 2, 1, 0, 'trading', '[\"20261013-WIN-orb5\"]')"
    )
    work = DailyReviewWork(journal=journal, clock=SimClock(at("16:16")), settings=SETTINGS,
                           day=DAY)  # fmt: skip
    failed: LLMResult[Any] = LLMResult(
        False,
        None,
        "20261013-review-01-2",
        900,
        "summary: Field required",
        failure="schema_invalid",
    )
    await work.not_reviewed(why="schema_invalid", result=failed)
    row = await journal.fetch_one("SELECT status, failure, llm_call_id FROM daily_reviews")
    assert row is not None
    assert tuple(row) == ("invalid", "schema_invalid", "20261013-review-01-2")
    timed_out: LLMResult[Any] = LLMResult(False, None, "20261013-review-01-2", 600_000, "late",
                                          failure="timeout")  # fmt: skip
    await work.not_reviewed(why="timeout", result=timed_out)
    row = await journal.fetch_one("SELECT status FROM daily_reviews")
    assert row is not None
    assert row["status"] == "missing"
    await journal.aclose()


# ---------------------------------------------------------------- history (S§6.6)


def test_history_reads_the_last_days_the_epoch_s_factors_and_the_open_items(
    conn: sqlite3.Connection,
) -> None:
    days = [DAY - timedelta(days=k) for k in range(1, 14)]
    for k, d in enumerate(days):
        insert_signals(conn, [sig("PAST", 1, day=d)])
        status = "missing" if k == 2 else "ok"
        conn.execute("INSERT INTO daily_reviews (date, status, summary, eval_epoch) "
                     "VALUES (?, ?, ?, 0)", (d.isoformat(), status, f"summary {k}"))  # fmt: skip
        r = 0.0 if k == 4 else 1.0 if k % 2 else -1.0  # a flat one is neither win nor loss
        conn.execute("INSERT INTO shadow_outcomes (signal_id, shadow_r) VALUES (?, ?)",
                     (f"{d:%Y%m%d}-PAST-orb5", r))  # fmt: skip
        conn.execute(
            "INSERT INTO signal_reviews (signal_id, date, why, factors_json, knowable_at_decision, "
            "b_process, b_note, eval_epoch) VALUES (?, ?, ?, ?, ?, ?, '', ?)",
            (f"{d:%Y%m%d}-PAST-orb5", d.isoformat(), f"why {k}",
             json.dumps(["false_breakout"] + (["late_entry"] if k < 3 else [])),
             {0: "yes", 1: "partly"}.get(k, "no"),
             "bad_process" if k in (0, 11) else "good_process",
             1 if k == 12 else 0),  # the oldest is from another epoch
        )  # fmt: skip
    conn.execute("INSERT INTO process_issues (first_seen, last_seen, occurrences, severity, "
                 "component, description, status) VALUES ('2026-10-10', '2026-10-12', 1, 'info', "
                 "'c', 'newer', 'open')")  # fmt: skip
    h = read_history(conn, DAY, days=10)
    assert [d for d, _ in h.summaries] == [d for k, d in enumerate(days[:11]) if k != 2]
    assert [b.why for b in h.bad_process] == ["why 0"]  # day 11 is before the ten summaries
    tally = {f.factor: (f.targets, f.wins, f.losses, f.mean_shadow_r, f.knowable_yes)
             for f in h.factor_tally}  # fmt: skip
    # 12 in this epoch: six +1, five -1 and one flat; "partly" isn't "yes"
    assert tally == {"false_breakout": (12, 6, 5, 1 / 12, 1), "late_entry": (3, 1, 2, -1 / 3, 1)}
    assert [f.factor for f in h.factor_tally] == ["false_breakout", "late_entry"]
    assert [(i.id, i.description) for i in h.open_issues] == [(3, "newer"), (1, "the first text")]
    assert [(n.id, n.times_raised) for n in h.open_notes] == [(1, 2)]


def test_epoch_stats_are_plain_running_numbers(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "INSERT INTO trades (id, book, signal_id, symbol, qty, entry_ts, entry_price, r_multiple, "
        "eval_epoch) VALUES ('A-1', 'A', '20261013-WIN-orb5', 'WIN', 10, "
        "'2026-10-13T09:41:00-04:00', '12.74', 2.0, 0), ('A-2', 'A', '20261013-LOSE-orb5', 'LOSE', "
        "10, '2026-10-13T09:42:00-04:00', '12.74', -1.0, 0);"
        "INSERT INTO shadow_outcomes (signal_id, shadow_r) VALUES ('20261013-WIN-orb5', 2.2);"
    )
    stats = epoch_stats(conn, 0, DAY)
    assert (stats["a_trades"], stats["a_mean_r"], stats["a_win_rate"]) == (2, 0.5, 0.5)
    assert (stats["b_trades"], stats["b_mean_r"], stats["shadow_longs"]) == (0, None, 1)


# ---------------------------------------------------------------- follow-ups


def test_only_the_human_closes_an_issue_or_a_note(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO process_issues (id, first_seen, last_seen, occurrences, severity, "
                 "component, description, status) VALUES (5, '2026-10-01', '2026-10-02', 7, "
                 "'info', 'c', 'as old, more often', 'open'), (6, '2026-09-28', '2026-09-28', 1, "
                 "'info', 'c', 'oldest', 'open')")  # fmt: skip
    listed = followups(conn)  # the oldest first, then the most recurring
    assert [(f.kind, f.id, f.status) for f in listed] == [
        ("issue", 6, "open"), ("issue", 5, "open"), ("issue", 1, "open"), ("note", 1, "open"),
    ]  # fmt: skip
    assert len(followups(conn, include_closed=True)) == 5
    for item in (5, 6):
        set_followup(conn, "issue", item, "wontfix", "", at("16:59"))
    set_followup(conn, "issue", 1, "fixed", "fixed in abc123", at("17:00"))
    set_followup(conn, "note", 1, "rejected", "the floor stays", at("17:01"))
    assert followups(conn) == []
    issue = conn.execute("SELECT status, resolution, resolved_at FROM process_issues "
                         "WHERE id = 1").fetchone()  # fmt: skip
    assert tuple(issue) == ("fixed", "fixed in abc123", at("17:00").isoformat())
    with pytest.raises(FollowupError, match="an issue's status is one of fixed, wontfix"):
        set_followup(conn, "issue", 1, "accepted", "", at("17:02"))
    with pytest.raises(FollowupError, match="a note's status is one of"):
        set_followup(conn, "note", 1, "fixed", "", at("17:02"))
    with pytest.raises(FollowupError, match="no issue 42"):
        set_followup(conn, "issue", 42, "wontfix", "", at("17:02"))


def test_trader_followups_lists_and_closes_them(repo: Path) -> None:
    db = open_database(repo / "runtime" / "trader.db")
    db.executescript(
        "INSERT INTO process_issues (first_seen, last_seen, occurrences, severity, component, "
        "description, status) VALUES ('2026-10-01', '2026-10-09', 3, 'warn', 'vet', "
        "'slow vet' || char(27) || '[31m', 'open');"
        "INSERT INTO review_notes (first_raised, last_raised, times_raised, note, status) "
        "VALUES ('2026-10-05', '2026-10-08', 2, 'raise the floor?', 'open');"
    )
    db.close()

    def run(*args: str) -> Any:
        return CliRunner().invoke(app, ["--root", str(repo), "followups", *args])

    listed = run("list")
    assert listed.exit_code == 0, listed.output
    assert listed.output.splitlines() == [
        "issue    1  open     warn   2026-10-01..2026-10-09  x3  vet: slow vet [31m",  # no ESC
        "note     1  open            2026-10-05..2026-10-08  x2  raise the floor?",
    ]
    done = run("set", "issue", "1", "--status", "fixed", "--response", "fixed in abc123")
    assert (done.exit_code, done.output.strip()) == (0, "issue 1: fixed")
    wrong = run("set", "note", "1", "--status", "fixed")
    assert wrong.exit_code == 1
    assert "a note's status is one of accepted, rejected, done" in wrong.output
    assert run("set", "lesson", "1", "--status", "done").exit_code == 1
    assert run("list").output.splitlines() == [
        "note     1  open            2026-10-05..2026-10-08  x2  raise the floor?"
    ]
    assert "[fixed in abc123]" in run("list", "--all").output
