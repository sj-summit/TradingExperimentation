"""The evaluation memo (P5.5; S§8; DECISIONS D-079): the minimum data, each hypothesis and
criterion as pass, fail or insufficient data, the outcome with the pre-registered inconclusive
row, Spearman, rendering, and the CLI."""

import itertools
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fixtures.exec_harness import SETTINGS
from tests.fixtures.journal_days import (
    add_book_day,
    add_long,
    add_session,
    add_trade,
    journal,
    sessions,
)
from trader.cli import app
from trader.memory.journal import open_database
from trader.reports.evaluation import (
    Memo,
    build_memo,
    counts,
    render_html,
    render_markdown,
    spearman,
)

CFG = SETTINGS.lessons


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = journal(tmp_path / "trader.db")
    yield c
    c.close()


def epoch_days(
    conn: sqlite3.Connection,
    n: int,
    *,
    allowed: Sequence[float] = (1.0, 0.8, 1.2),
    vetoed: Sequence[float] = (-1.0,),
    alternate: bool = False,
    b_trade: tuple[float, str] = (1.0, "2.00"),
    a_vetoed: tuple[float, str] = (-1.0, "-1.20"),
    a_day: str = "0.50",
    b_day: str = "0.60",
    modes: Sequence[str] | None = None,
) -> list[date]:
    """``n`` sessions of triggered longs, ``allowed`` (ranks 1 up) and ``vetoed`` (ranks 16 up)
    by Book B, their shadow Rs negated on every other day when ``alternate``.

    Both books trade the first allowed long at ``b_trade``'s R and P&L; Book A also trades the
    first vetoed one, at ``a_vetoed``'s. The books' days move by ``a_day`` and ``b_day``."""
    days = sessions(date(2026, 1, 5), n)
    equity = {"A": Decimal(1000), "B": Decimal(1000)}
    for k, day in enumerate(days):
        sign = -1.0 if alternate and k % 2 else 1.0
        add_session(conn, day)
        for j, r in enumerate(allowed):
            add_long(conn, day, f"AL{j}", shadow_r=sign * r, verdict="allow", rank=j + 1)
        for j, r in enumerate(vetoed):
            add_long(conn, day, f"VE{j}", shadow_r=sign * r, verdict="veto", rank=16 + j)
        if allowed:
            for book in ("A", "B"):
                add_trade(conn, day, book, "AL0", r=b_trade[0], pnl=b_trade[1])
        if vetoed:
            add_trade(conn, day, "A", "VE0", r=a_vetoed[0], pnl=a_vetoed[1])
        for book, step in (("A", a_day), ("B", b_day)):
            start = equity[book]
            equity[book] = start + Decimal(step)
            add_book_day(conn, day, book, start=str(start), end=str(equity[book]))
        if modes is not None:
            conn.execute(
                "INSERT INTO day_modes (date, book, mode, source, multiplier, eval_epoch) "
                "VALUES (?, 'B', ?, 'premarket', '1', 0)",
                (day.isoformat(), modes[k % len(modes)]),
            )
    return days


def verdicts(m: Memo) -> dict[str, str]:
    return {x.name: x.verdict for x in (*m.minimum, *m.hypotheses, *m.criteria)}


# ---------------------------------------------------------------- the minimum data and outcomes


def test_a_short_epoch_is_insufficient_data_and_keeps_running(conn: sqlite3.Connection) -> None:
    days = epoch_days(conn, 10)
    m = build_memo(conn, through=days[-1], cfg=CFG)
    v = verdicts(m)
    assert (m.days, m.triggered, m.vetoes, m.minimum_met) == (10, 40, 10, False)
    assert (v["days"], v["triggered"], v["vetoes"]) == ("insufficient data",) * 3
    assert (v["H1"], v["H2"], v["1"], v["2"], v["3"]) == ("insufficient data",) * 5
    assert (v["4"], v["5"]) == ("insufficient data", "for the human")
    assert m.outcome == "keep running"
    assert m.next_step.startswith("The minimum data isn't reached yet")


def test_the_minimum_data_is_met_exactly_at_60_days_150_longs_and_20_vetoes(
    conn: sqlite3.Connection,
) -> None:
    days = epoch_days(conn, 60, allowed=(1.0, 0.8), vetoed=())  # 120 triggered, no vetoes
    for day in days[:20]:
        add_long(conn, day, "VE0", shadow_r=-1.0, verdict="veto", rank=16)
    for day in days[:10]:
        add_long(conn, day, "AL2", shadow_r=1.2, verdict="allow", rank=3)
    m = build_memo(conn, through=days[-1], cfg=CFG)
    assert (m.days, m.triggered, m.vetoes, m.minimum_met) == (60, 150, 20, True)
    assert verdicts(build_memo(conn, through=days[-2], cfg=CFG))["days"] == "insufficient data"

    one_long = f"{days[0]:%Y%m%d}-AL2-orb5"
    conn.execute("UPDATE shadow_outcomes SET shadow_r = NULL WHERE signal_id = ?", (one_long,))
    v = verdicts(build_memo(conn, through=days[-1], cfg=CFG))
    assert (v["triggered"], v["vetoes"]) == ("insufficient data", "pass")  # 149 and 20

    conn.execute("UPDATE shadow_outcomes SET shadow_r = 1.2 WHERE signal_id = ?", (one_long,))
    conn.execute(
        "UPDATE verdicts SET verdict = 'allow' WHERE signal_id = ?", (f"{days[0]:%Y%m%d}-VE0-orb5",)
    )
    v = verdicts(build_memo(conn, through=days[-1], cfg=CFG))
    assert (v["triggered"], v["vetoes"]) == ("pass", "insufficient data")  # 150 and 19


def test_enough_data_with_vetoes_that_help_passes_what_code_can_check(
    conn: sqlite3.Connection,
) -> None:
    days = epoch_days(conn, 62)
    m = build_memo(conn, through=days[-1], cfg=CFG)
    v = verdicts(m)
    assert (m.days, m.triggered, m.vetoes, m.minimum_met) == (62, 248, 62, True)
    assert (v["H1"], v["H2"], v["1"], v["2"], v["3"]) == ("pass",) * 5
    assert m.hypotheses[0].numbers.startswith("V +2.000 R, 90% interval [")
    assert m.hypotheses[1].numbers == (
        "B - A +1.000 R per trade, 90% interval [+1.000, +1.000]; B n 62, A n 124"
    )
    assert m.criteria[1].numbers == "+1.650 R over 62 trades"  # (2.00 - 0.02) / (10 x 0.12)
    assert m.criteria[2].numbers == "0.00%"
    assert m.outcome == "the criteria code can check pass"
    assert m.criteria[3].numbers.endswith("the longest P1-free run of sessions is 62")


@pytest.mark.parametrize(
    ("a_day", "outcome"),
    [("0.50", "H1 fails, Book A positive"), ("-0.50", "criteria not met")],
)
def test_vetoes_that_cost(conn: sqlite3.Connection, a_day: str, outcome: str) -> None:
    days = epoch_days(conn, 62, allowed=(-1.0, -0.8, -1.2), vetoed=(1.0,), a_day=a_day)
    m = build_memo(conn, through=days[-1], cfg=CFG)
    assert (verdicts(m)["H1"], verdicts(m)["1"]) == ("fail", "fail")
    assert m.outcome == outcome


def test_a_v_of_exactly_zero_fails_and_an_equal_book_b_passes_h2(
    conn: sqlite3.Connection,
) -> None:
    """V is 0 with a zero-width interval; Book A's trades average what Book B's do."""
    days = epoch_days(conn, 62, allowed=(-1.0, -1.0), vetoed=(-1.0,), a_vetoed=(1.0, "2.00"))
    m = build_memo(conn, through=days[-1], cfg=CFG)
    v = verdicts(m)
    assert m.hypotheses[0].numbers.startswith("V +0.000 R, 90% interval [+0.000, +0.000]")
    assert (v["H1"], v["1"], v["H2"]) == ("fail", "fail", "pass")
    assert m.hypotheses[1].numbers.startswith("B - A +0.000 R per trade")
    assert m.outcome == "H1 fails, Book A positive"  # a narrow interval at 0 is an answer


def test_criteria_not_met(conn: sqlite3.Connection) -> None:
    """Vetoes help, but Book B's trades lose and its equity falls 18.6%."""
    days = epoch_days(conn, 62, b_trade=(-1.0, "-1.20"), a_vetoed=(3.0, "3.60"), b_day="-3.00")
    m = build_memo(conn, through=days[-1], cfg=CFG)
    v = verdicts(m)
    assert (v["H1"], v["H2"], v["1"], v["2"], v["3"]) == ("pass", "fail", "pass", "fail", "fail")
    assert m.criteria[1].numbers == "-1.017 R over 62 trades"  # (-1.20 - 0.02) / 1.20
    assert m.criteria[2].numbers == "18.60%"
    assert m.outcome == "criteria not met"


def test_a_drawdown_of_exactly_15_percent_passes(conn: sqlite3.Connection) -> None:
    days = epoch_days(conn, 60, b_day="-2.50")  # 1,000 down to 850
    m = build_memo(conn, through=days[-1], cfg=CFG)
    assert (m.criteria[2].verdict, m.criteria[2].numbers) == ("pass", "15.00%")


def test_both_books_negative(conn: sqlite3.Connection) -> None:
    days = epoch_days(conn, 62, a_day="-0.50", b_day="-0.60")
    assert build_memo(conn, through=days[-1], cfg=CFG).outcome == "both books negative"


@pytest.mark.parametrize(
    ("allowed", "vetoed", "alternate", "minimum_met"),
    [
        ((1.0, 0.8, 1.2), (), False, False),
        ((), (-1.0,), False, False),
        ((3.0,), (-3.0,), True, True),  # V's interval is about 2 R wide around 0
    ],
    ids=["no vetoes, H1 untestable", "90 triggered longs", "V spans 0 widely"],
)
def test_ninety_days_without_an_answer_are_inconclusive(
    conn: sqlite3.Connection,
    allowed: Sequence[float],
    vetoed: Sequence[float],
    alternate: bool,
    minimum_met: bool,
) -> None:
    days = epoch_days(conn, 90, allowed=allowed, vetoed=vetoed, alternate=alternate)
    m = build_memo(conn, through=days[-1], cfg=CFG)
    assert (m.days, m.minimum_met, m.outcome) == (90, minimum_met, "inconclusive")
    assert m.next_step.startswith("Not evidence either way. The human chooses, once")
    day_89 = build_memo(conn, through=days[-2], cfg=CFG)
    assert day_89.outcome == "keep running"
    assert day_89.next_step.startswith("V's interval" if minimum_met else "The minimum data")


def test_a_narrow_interval_around_zero_after_90_days_is_an_answer(
    conn: sqlite3.Connection,
) -> None:
    days = epoch_days(conn, 90, allowed=(0.1,), vetoed=(-0.1,), alternate=True)
    m = build_memo(conn, through=days[-1], cfg=CFG)
    v = m.summary.veto.v_interval
    assert v is not None
    assert v.low < 0 < v.high
    assert v.high - v.low < 0.5
    assert verdicts(m)["H1"] == "fail"
    assert m.outcome == "H1 fails, Book A positive"


def test_a_p1_breaks_the_unattended_run(conn: sqlite3.Connection) -> None:
    days = epoch_days(conn, 30)
    conn.execute(
        "INSERT INTO incidents (ts, severity, component, code, message) VALUES (?, 'P1', 'x', "
        "'p1', 'm')",
        (f"{days[9].isoformat()}T10:00:00-05:00",),
    )
    m = build_memo(conn, through=days[-1], cfg=CFG)
    assert m.criteria[3].numbers.endswith("the longest P1-free run of sessions is 20")
    assert m.criteria[3].verdict == "insufficient data"  # never pass before Phase 6


# ---------------------------------------------------------------- H3 and H4


@pytest.mark.parametrize(
    ("low_day_rs", "verdict", "numbers"),
    [
        ((-1.0,), "pass", "reduced/no_trade - normal -1.900 R"),
        ((3.0,), "fail", "reduced/no_trade - normal +2.100 R"),
        ((-5.0, 5.0), "fail", "reduced/no_trade - normal -0.900 R"),  # its interval spans 0
    ],
    ids=["lower", "higher", "lower but uncertain"],
)
def test_h3_compares_reduced_and_no_trade_days_with_normal_ones(
    conn: sqlite3.Connection, low_day_rs: tuple[float, ...], verdict: str, numbers: str
) -> None:
    """Every other day is reduced or no_trade (20 longs a side). Their longs make
    ``low_day_rs``, a day at a time in turn; the normal days' make 0.9 R."""
    days = epoch_days(
        conn,
        20,
        allowed=(1.0, 0.8),
        vetoed=(),
        modes=("normal", "reduced", "normal", "no_trade"),
    )
    for day, r in zip(days[1::2], itertools.cycle(low_day_rs), strict=False):
        conn.execute(
            "UPDATE shadow_outcomes SET shadow_r = ? WHERE signal_id LIKE ?",
            (r, f"{day:%Y%m%d}-%"),
        )
    h3 = build_memo(conn, through=days[-1], cfg=CFG).hypotheses[2]
    assert h3.verdict == verdict
    assert h3.numbers.startswith(numbers)
    assert h3.numbers.endswith("n 20 and 20")
    few = build_memo(conn, through=days[5], cfg=CFG).hypotheses[2]
    assert few.verdict == "insufficient data"  # 6 a side, under 20


@pytest.mark.parametrize(("sign", "verdict"), [(1.0, "pass"), (-1.0, "fail")])
def test_h4_compares_ranks_1_to_5_with_16_to_20(
    conn: sqlite3.Connection, sign: float, verdict: str
) -> None:
    """Ranks 6 and 15 are in neither bucket: their outsized Rs must not move the difference."""
    days = epoch_days(conn, 10, allowed=(), vetoed=())
    for day in days:
        for rank, r in ((1, 1.0), (5, 1.0), (6, 9.0), (15, -9.0), (16, -1.0), (20, -1.0)):
            add_long(conn, day, f"R{rank}", shadow_r=sign * r, rank=rank)
    h4 = build_memo(conn, through=days[-1], cfg=CFG).hypotheses[3]
    assert h4.verdict == verdict
    assert h4.numbers.startswith(f"ranks 1-5 - 16-20 {sign * 2:+.3f} R")
    assert f"n 20 and 20; Spearman rank vs R {sign * -0.618:+.3f}" in h4.numbers
    few = build_memo(conn, through=days[8], cfg=CFG).hypotheses[3]
    assert few.verdict == "insufficient data"  # 18 a side, under 20


def test_h4_fails_when_its_interval_reaches_0(conn: sqlite3.Connection) -> None:
    """Rank 1 averages +1 R but swings between +9 and -7: ahead of rank 16, not surely."""
    days = epoch_days(conn, 20, allowed=(), vetoed=())
    for day, r in zip(days, itertools.cycle((9.0, -7.0)), strict=False):
        add_long(conn, day, "R1", shadow_r=r, rank=1)
        add_long(conn, day, "R16", shadow_r=-1.0, rank=16)
    h4 = build_memo(conn, through=days[-1], cfg=CFG).hypotheses[3]
    assert h4.numbers.startswith("ranks 1-5 - 16-20 +2.000 R")
    assert h4.verdict == "fail"


def test_spearman() -> None:
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 2, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.9486833)  # a tie, averaged
    assert spearman([1, 2], [2, 1]) is None
    assert spearman([1, 1, 1], [1, 2, 3]) is None


# ---------------------------------------------------------------- rendering and the CLI


def test_the_memo_renders(conn: sqlite3.Connection) -> None:
    days = epoch_days(conn, 10)
    m = build_memo(conn, through=days[-1], cfg=CFG)
    md = render_markdown(m)
    assert md.startswith(f"# Evaluation memo, epoch 0, through {days[-1]}")
    assert "- **Outcome: keep running.**" in md
    assert "- 10 trading days, 40 triggered longs, 10 Book B vetoes.\n" in md
    assert "| 4. 20 consecutive unattended sessions without a P1 | insufficient data |" in md
    dry_run = "this memo is a dry run, not an evaluation."
    assert dry_run in md  # epoch 0 is the shakedown (S§9)
    assert dry_run in render_html(m)
    later = render_markdown(build_memo(conn, through=days[-1], cfg=CFG, epoch=1))
    assert "- Judged by the rules pre-registered in STRATEGY.md section 8." in later
    assert dry_run not in later
    assert "## Recommend live trading only if all hold" in md
    assert "| 5. The human signs off | for the human | G7 |" in md
    assert "<h2>Hypotheses</h2>" in render_html(m)


def test_one_day_without_vetoes_reads_n_a_where_nothing_can_be_computed(
    conn: sqlite3.Connection,
) -> None:
    days = epoch_days(conn, 1, vetoed=())
    m = build_memo(conn, through=days[-1], cfg=CFG)
    assert counts(m) == "1 trading day, 3 triggered longs, 0 Book B vetoes"
    assert m.hypotheses[0].numbers == (
        "V n/a, no interval; allowed n 3, vetoed n 0, the vetoed's own mean n/a"
    )
    assert m.hypotheses[2].numbers == "reduced/no_trade - normal n/a, no interval; n 0 and 0"
    assert m.criteria[1].numbers == "+1.650 R over 1 trade"
    assert counts(build_memo(conn, through=days[-1] - timedelta(days=1), cfg=CFG)) == (
        "0 trading days, 0 triggered longs, 0 Book B vetoes"
    )


def test_trader_report_evaluation(repo: Path) -> None:
    db = open_database(repo / "runtime" / "trader.db")
    db.row_factory = sqlite3.Row
    days = epoch_days(db, 10)
    db.commit()
    db.close()
    result: Any = CliRunner().invoke(
        app, ["--root", str(repo), "report", "evaluation", "--through", days[-1].isoformat()]
    )
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == (
        f"epoch 0 through {days[-1]}: keep running (10 trading days, 40 triggered longs, "
        "10 Book B vetoes)"
    )
    assert (repo / "runtime" / "reports" / "evaluation-epoch-0.md").is_file()


def test_trader_report_evaluation_rejects_a_bad_date(repo: Path) -> None:
    result: Any = CliRunner().invoke(
        app, ["--root", str(repo), "report", "evaluation", "--through", "2026-11-31"]
    )
    assert result.exit_code == 1
    assert result.output == "--through takes YYYY-MM-DD, like 2026-11-20, not '2026-11-31'\n"
