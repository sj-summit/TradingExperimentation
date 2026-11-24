"""`trader run-day` wiring (P3.5; DECISIONS D-048), offline: no market data, no orders."""

import asyncio
import hashlib
from contextlib import AsyncExitStack
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fixtures import nyse
from tests.helpers import FAKE_SECRETS, set_yaml, write_env
from tests.unit.test_llm_context import SESSION, TIMES
from trader.cli import app
from trader.config import load_config
from trader.core.clock import ET, SimClock
from trader.data.news import NewsHub
from trader.data.recorder import Recorder, read_jsonl
from trader.data.ws import StreamError
from trader.execution.sim_broker import SimBroker
from trader.llm.claude_cli import ClaudeBinaryError, ClaudeCliTransport
from trader.llm.replay import LLM_FILE, RecordingTransport
from trader.memory.incidents import record_incident
from trader.memory.journal import Journal
from trader.memory.reviews import DailyReviewWork
from trader.orchestrator import live
from trader.orchestrator import replay as replay_module
from trader.orchestrator.day import DayOutcome
from trader.orchestrator.live import BookChoiceError, RunOptions, folders, parse_books, run_day
from trader.orchestrator.lock import RunLock, RunLockError
from trader.orchestrator.packs import PackContext, RecordingPacks
from trader.orchestrator.provenance import source_sha256
from trader.orchestrator.simdrive import drive

D = Decimal
MORNING = datetime(2026, 10, 13, 8, 31, tzinfo=ET)


class FakeRest:
    def __init__(self, sim: SimClock) -> None:
        self._sim = sim

    async def calendar(self, start: date, end: date) -> list[dict[str, str]]:
        return nyse.calendar_rows(max(start, nyse.FIRST), min(end, nyse.LAST))

    async def clock(self) -> datetime:
        return self._sim.now()

    async def __aenter__(self) -> "FakeRest":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture(autouse=True)
def roomy_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """run-day raises disk_low under 2 GiB free (D-084): these tests never read the real disk."""
    monkeypatch.setattr(live, "free_bytes", lambda _path: 100 * 1024**3)


@pytest.fixture
def offline(repo: Path, monkeypatch: pytest.MonkeyPatch) -> SimClock:
    clock = SimClock(MORNING)
    rest = FakeRest(clock)
    monkeypatch.setattr(live.AlpacaRest, "from_config", lambda _cfg, _clock: rest)
    monkeypatch.setattr(
        live.AlpacaBroker,
        "from_config",
        lambda _cfg, book, _clock: SimBroker(clock=clock, book=book),
    )
    # The CLI passes no clock, so run_day would read the PC's: on a weekend or a holiday the
    # day would have "nothing to do". Its fallback is the fixture's morning too.
    monkeypatch.setattr(live, "RealClock", lambda: clock)
    return clock


def test_books_are_a_b_or_both(repo: Path) -> None:
    assert parse_books(" a ") == ("A",)
    assert parse_books("b,A") == parse_books("A,B") == ("A", "B")
    assert parse_books("B") == ("B",)
    for wrong in ("C", "A,C", " , "):
        with pytest.raises(BookChoiceError, match="--books takes A, B or A,B"):
            parse_books(wrong)


def test_book_b_is_off_with_no_llm_or_the_settings(repo: Path) -> None:
    """Plan decision 9: --no-llm, llm.enabled false (or books.B.enabled false) runs Book A
    alone, and says why."""
    cfg = load_config(repo)
    both = RunOptions(books=("A", "B"))
    assert live.book_b_off(cfg, both) is None
    assert live.book_b_off(cfg, RunOptions(books=("A",), no_llm=True)) is None  # not asked
    assert live.book_b_off(cfg, RunOptions(books=("A", "B"), no_llm=True)) == "--no-llm"
    set_yaml(repo / "config" / "settings.yaml", "llm.enabled", False)
    assert live.book_b_off(load_config(repo), both) == "llm.enabled is false"
    set_yaml(repo / "config" / "settings.yaml", "llm.enabled", True)
    set_yaml(repo / "config" / "settings.yaml", "books.B.enabled", False)
    assert live.book_b_off(load_config(repo), both) == "books.B.enabled is false"


async def test_no_llm_dry_runs_book_a_alone(repo: Path, offline: SimClock) -> None:
    lines: list[str] = []
    options = RunOptions(books=("A", "B"), dry_run=True, no_llm=True)
    await run_day(repo, options, lines.append, clock=offline)
    assert "Book B is off today: --no-llm" in lines
    assert [line for line in lines if line.startswith("Book ")] == [
        "Book B is off today: --no-llm",
        lines[-1],
    ]
    assert lines[-1].startswith("Book A: cash 1000")
    assert "  09:05:00  Claude's pre-market brief (Book B)" not in lines


async def test_book_b_alone_and_off_is_refused(repo: Path, offline: SimClock) -> None:
    with pytest.raises(BookChoiceError, match=r"Book B is off \(--no-llm\) and no other book"):
        await run_day(repo, RunOptions(books=("B",), no_llm=True), print, clock=offline)


async def test_a_dry_run_with_book_b_shows_its_times_and_its_llm(
    repo: Path, offline: SimClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(_name: str) -> Path:
        raise ClaudeBinaryError("claude is not on PATH (PLAN.md H0.2)")

    monkeypatch.setattr(live, "resolve_claude", missing)
    lines: list[str] = []
    await run_day(repo, RunOptions(books=("A", "B"), dry_run=True), lines.append, clock=offline)
    assert lines[0] == (
        "Book B's LLM: UNAVAILABLE (claude is not on PATH (PLAN.md H0.2)): run-day would run "
        "Book A alone"
    )
    assert "  09:05:00  Claude's pre-market brief (Book B)" in lines
    assert "  16:29:00  the review's cutoff" in lines
    assert [line[:7] for line in lines if line.startswith("Book ") and ": cash" in line] == [
        "Book A:", "Book B:",
    ]  # fmt: skip
    monkeypatch.setattr(live, "resolve_claude", lambda _name: Path("C:/bin/claude.exe"))
    lines.clear()
    await run_day(repo, RunOptions(books=("A", "B"), dry_run=True), lines.append, clock=offline)
    assert lines[0].startswith("Book B's LLM: claude_cli, ")
    assert lines[0].endswith("claude.exe; credential set")


def test_the_cli_takes_no_llm(repo: Path, offline: SimClock) -> None:
    result = CliRunner().invoke(
        app, ["--root", str(repo), "run-day", "--books", "A,B", "--no-llm", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Book B is off today: --no-llm" in result.output


async def test_book_b_s_llm_for_a_simulated_day(tmp_path: Path, repo: Path) -> None:
    """--sim: a scripted model, an environment with no credential, the sim's own folders, and
    the answers and packs recorded (D-064, D-069)."""
    cfg = load_config(repo)
    clock = SimClock(MORNING)
    where = folders(cfg, MORNING.date(), sim=True)
    journal = await Journal.open(where.db, inline=True)
    recorder = Recorder(where.recordings, MORNING.date())
    async with AsyncExitStack() as stack:
        deps = await live._book_b_llm(cfg, RunOptions(books=("A", "B"), sim=True), clock,
                                      journal, FakeRest(clock), recorder, where, stack)  # type: ignore[arg-type]  # fmt: skip
        calls = deps.calls
        assert isinstance(calls._transport, RecordingTransport)
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in calls._env
        assert (calls._sandbox, calls._artifacts) == (where.llm_sandbox, where.llm_artifacts)
        assert where.llm_sandbox.parent == where.db.parent  # runtime/sim/<date>/
        assert deps.setup["transport"] == "scripted"
        assert deps.setup["edgar"] == "on"
        assert deps.fingerprint is not None
    await journal.aclose()


class Orchestrators:
    """Stands in for DayOrchestrator: keeps the day's deps, runs nothing."""

    def __init__(self) -> None:
        self.deps: list[Any] = []

    def __call__(self, deps: Any) -> Any:
        self.deps.append(deps)
        made = self

        class Day:
            async def run(self) -> DayOutcome:
                return DayOutcome(deps.clock.now().date(), "DONE")

            async def dry_run(self) -> list[str]:
                return []

        _ = made
        return Day()


async def test_a_missing_cli_runs_book_a_alone_with_a_p2(
    repo: Path, offline: SimClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(_name: str) -> Path:
        raise ClaudeBinaryError("claude is not on PATH (PLAN.md H0.2)")

    days = Orchestrators()
    monkeypatch.setattr(live, "resolve_claude", missing)
    monkeypatch.setattr(live, "DayOrchestrator", days)
    lines: list[str] = []
    await run_day(repo, RunOptions(books=("A", "B")), lines.append, clock=offline)
    (deps,) = days.deps
    assert sorted(deps.brokers) == ["A"]
    assert deps.llm is None
    assert deps.book_b_off == "the LLM is unavailable: claude is not on PATH (PLAN.md H0.2)"
    assert f"Book B is off today: {deps.book_b_off}" in lines
    journal = await Journal.open(repo / "runtime" / "trader.db")
    rows = await journal.fetch_all("SELECT severity, code FROM incidents")
    await journal.aclose()
    assert [(r["severity"], r["code"]) for r in rows] == [("P2", "llm_unavailable")]
    with pytest.raises(BookChoiceError, match="no other book was asked for"):
        await run_day(repo, RunOptions(books=("B",)), lines.append, clock=offline)


async def test_paper_wires_the_real_cli_recorded_with_its_news_and_edgar(
    repo: Path, offline: SimClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_yaml(repo / "config" / "settings.yaml", "llm.max_tested_cli_version", "2.1.296")
    set_yaml(repo / "config" / "settings.yaml", "llm.kill_grace_s", 7.5)  # not the default 5
    cfg = load_config(repo)
    monkeypatch.setattr(live, "resolve_claude", lambda _name: Path("C:/bin/claude.exe"))
    monkeypatch.setattr(live, "cli_version", lambda *_args: "2.1.300 (Claude Code)")
    where = folders(cfg, MORNING.date(), sim=False)
    journal = await Journal.open(where.db, inline=True)
    recorder = Recorder(where.recordings, MORNING.date())
    async with AsyncExitStack() as stack:
        deps = await live._book_b_llm(cfg, RunOptions(books=("A", "B")), offline, journal,
                                      FakeRest(offline), recorder, where, stack)  # type: ignore[arg-type]  # fmt: skip
        calls = deps.calls
        transport = calls._transport
        assert isinstance(transport, RecordingTransport)
        assert isinstance(transport._inner, ClaudeCliTransport)
        assert transport._inner._grace == 7.5
        assert calls._command == (str(Path("C:/bin/claude.exe")),)
        assert (calls._sandbox, calls._artifacts) == (
            repo / "runtime" / "llm_sandbox", repo / "runtime" / "llm",
        )  # fmt: skip
        assert calls._env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_SECRETS["CLAUDE_CODE_OAUTH_TOKEN"]
        assert deps.setup["cli_version"] == "2.1.300"
        news = deps.news
        assert isinstance(news, NewsHub)
        assert news._on_item == recorder.record_news  # every article recorded
        await drive(offline, news._sleep(5))  # its sleeps pass on the day's clock
        assert offline.now() == MORNING + timedelta(seconds=5)
        ctx = PackContext(journal=journal, clock=offline, market=FakeRest(offline),  # type: ignore[arg-type]
                          settings=cfg.settings, risk=cfg.risk, session=SESSION,
                          previous_session=date(2026, 11, 2), times=TIMES, events=(), regime=None,
                          encode=calls.encode)  # fmt: skip
        assert isinstance(deps.packs(ctx), RecordingPacks)  # every pack it builds recorded
        assert isinstance(deps.review, DailyReviewWork)  # the 16:15 review's content (P4.10)
        transport._sink({"kind": "probe"})  # each LLM record is readable at once
        assert list(read_jsonl(where.recordings / "2026-10-13" / LLM_FILE)) == [{"kind": "probe"}]
    rows = await journal.fetch_all("SELECT severity, code FROM incidents")
    assert [(r["severity"], r["code"]) for r in rows] == [("P3", "llm_cli_untested")]
    recorder.close()
    await journal.aclose()


async def test_no_edgar_user_agent_leaves_filings_unknown_with_a_p3(
    repo: Path, offline: SimClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {k: v for k, v in FAKE_SECRETS.items() if k != "SEC_EDGAR_USER_AGENT"}
    write_env(repo, values)
    cfg = load_config(repo)
    monkeypatch.setattr(live, "resolve_claude", lambda _name: Path("C:/bin/claude.exe"))
    monkeypatch.setattr(live, "cli_version", lambda *_args: "")
    where = folders(cfg, MORNING.date(), sim=False)
    journal = await Journal.open(where.db, inline=True)
    async with AsyncExitStack() as stack:
        deps = await live._book_b_llm(cfg, RunOptions(books=("A", "B")), offline, journal,
                                      FakeRest(offline), None, where, stack)  # type: ignore[arg-type]  # fmt: skip
    assert str(deps.setup["edgar"]).startswith("off: set SEC_EDGAR_USER_AGENT")
    assert deps.setup["cli_version"] is None
    rows = await journal.fetch_all("SELECT severity, code FROM incidents")
    assert [(r["severity"], r["code"]) for r in rows] == [("P3", "edgar_unavailable")]
    await journal.aclose()


async def test_a_book_kept_off_must_be_flat(
    repo: Path, offline: SimClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-069: nothing watches a book that doesn't run today; a position there is a P2."""
    held = SimBroker(clock=offline, book="B")
    held._positions["XYZ"] = (5, D("50"))
    monkeypatch.setattr(live.AlpacaBroker, "from_config", lambda _cfg, _book, _clock: held)
    cfg = load_config(repo)
    journal = await Journal.open(repo / "runtime" / "trader.db", inline=True)
    lines: list[str] = []
    await live._check_idle_book(cfg, "B", offline, journal, lines.append)
    assert lines == [
        "Book B is off today, but its account holds {'XYZ': 5} and 0 open orders, which nothing "
        "watches: flatten it (`trader flatten --book B`) or run it"
    ]
    rows = await journal.fetch_all("SELECT severity, code FROM incidents")
    assert [(r["severity"], r["code"]) for r in rows] == [("P2", "idle_book_not_flat")]
    flat = SimBroker(clock=offline, book="B")
    monkeypatch.setattr(live.AlpacaBroker, "from_config", lambda _cfg, _book, _clock: flat)
    lines.clear()
    await live._check_idle_book(cfg, "B", offline, journal, lines.append)
    assert lines == []
    await journal.aclose()


def test_a_dry_run_of_book_b_alone_says_run_day_would_refuse(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(_name: str) -> Path:
        raise ClaudeBinaryError("claude is not on PATH")

    monkeypatch.setattr(live, "resolve_claude", missing)
    cfg = load_config(repo)
    assert live._llm_ready(cfg, RunOptions(books=("B",)), ("B",)).endswith(
        "run-day would refuse to run: no other book was asked for"
    )


async def test_a_dry_run_checks_boot_and_prints_the_day(repo: Path, offline: SimClock) -> None:
    lines: list[str] = []
    await run_day(repo, RunOptions(books=("A",), dry_run=True), lines.append, clock=offline)
    assert lines[0] == "session 2026-10-13"
    assert "  09:35:03  opening-range scan" in lines
    assert "clock skew +0.00 s (ok)" in lines
    assert "universe: not built yet (run-day builds it at boot)" in lines
    assert lines[-1].startswith("Book A: cash 1000, equity 1000, 0 positions")
    assert "ledger not seeded (seeds at boot); halts none" in lines[-1]
    assert not (repo / "runtime" / "recordings").exists()  # a dry run records nothing


async def test_a_second_engine_is_refused(repo: Path, offline: SimClock) -> None:
    held = RunLock(repo / "runtime" / "run-day.lock")
    held.acquire()
    try:
        with pytest.raises(RunLockError):
            await run_day(repo, RunOptions(books=("A",)), print, clock=offline)
    finally:
        held.release()


async def test_a_holiday_exits_quietly(repo: Path, offline: SimClock) -> None:
    holiday = SimClock(datetime(2026, 11, 26, 8, 30, tzinfo=ET))
    lines: list[str] = []
    await run_day(repo, RunOptions(books=("A",), dry_run=True), lines.append, clock=holiday)
    assert lines == ["2026-11-26 is not a trading day: nothing to do"]


def test_a_simulated_day_has_its_own_folder(repo: Path) -> None:
    cfg = load_config(repo)
    sim = folders(cfg, date(2026, 10, 13), sim=True)
    real = folders(cfg, date(2026, 10, 13), sim=False)
    assert sim.db == repo / "runtime" / "sim" / "2026-10-13" / "trader.db"
    assert sim.lock.parent == sim.db.parent
    assert (real.db, real.lock) == (
        repo / "runtime" / "trader.db",
        repo / "runtime" / "run-day.lock",
    )


async def test_a_simulated_day_starts_from_the_main_universe(tmp_path: Path) -> None:
    main = await Journal.open(tmp_path / "main.db")
    await main.execute(
        "INSERT INTO universe_daily (date, symbol, name, exchange, prev_close, atr14, adv14, "
        "or_vol_avg14, eligible, exclusion_reason) VALUES "
        "('2026-10-13', 'XYZ', 'X', 'NASDAQ', '12', '1', 2000000, 900.0, 1, NULL), "
        "('2026-10-12', 'OLD', 'O', 'NASDAQ', '12', '1', 2000000, 900.0, 1, NULL)"
    )
    await main.aclose()
    sim = await Journal.open(tmp_path / "sim.db", inline=True)
    await live._copy_universe(tmp_path / "main.db", tmp_path / "sim.db", date(2026, 10, 13), sim)
    rows = await sim.fetch_all("SELECT date, symbol FROM universe_daily")
    assert [(r["date"], r["symbol"]) for r in rows] == [("2026-10-13", "XYZ")]
    await live._copy_universe(tmp_path / "absent.db", tmp_path / "sim.db", date(2026, 10, 13), sim)
    await sim.aclose()


async def test_stream_errors_become_incidents(tmp_path: Path) -> None:
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    sink = live._IncidentSink(journal, SimClock(MORNING))
    sink.stream_error(StreamError(405, "symbol limit exceeded"))
    await asyncio.gather(*sink._pending)
    rows = await journal.fetch_all("SELECT severity, code, message FROM incidents")
    assert [(r["severity"], r["code"]) for r in rows] == [("P2", "subscription_refused")]
    assert "405" in rows[0]["message"]
    await journal.aclose()


def test_the_boot_record_names_the_commit_without_running_git(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert live._git_sha(tmp_path) is None
    (git / "packed-refs").write_text("abc123 refs/heads/main\n", encoding="utf-8")
    assert live._git_sha(tmp_path) == "abc123"
    (git / "refs" / "heads" / "main").write_text("def456\n", encoding="utf-8")
    assert live._git_sha(tmp_path) == "def456"
    info: dict[str, Any] = dict(live._boot_info(tmp_path, RunOptions(books=("A",), sim=True)))
    assert (info["git_sha"], info["sim"]) == ("def456", True)
    assert info["source_sha256"] is None  # no source tree here


def test_the_source_hash_names_the_code_whatever_the_line_endings(tmp_path: Path) -> None:
    """An uncommitted tree still has a name in boot.json (D-055)."""
    module = tmp_path / "src" / "trader" / "x.py"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"a = 1\nb = 2\n")
    first = source_sha256(tmp_path)
    module.write_bytes(b"a = 1\r\nb = 2\r\n")  # the same file checked out on Windows
    assert source_sha256(tmp_path) == first
    module.write_bytes(b"a = 1\nb = 3\n")
    assert source_sha256(tmp_path) not in (None, first)


def test_the_source_hash_takes_files_in_posix_path_order(tmp_path: Path) -> None:
    """Windows paths compare without case, and by part: a hash in that order would differ
    from one made on Linux for the same tree (review, D-055)."""
    names = ["src/trader/B.py", "src/trader/a.py", "src/trader/a/b.py"]  # POSIX string order
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(name.encode())
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b"\0" + name.encode() + b"\0")
    assert source_sha256(tmp_path) == digest.hexdigest()


def test_a_replay_says_when_the_code_changed_since_the_recording(tmp_path: Path) -> None:
    module = tmp_path / "src" / "trader" / "x.py"
    module.parent.mkdir(parents=True)
    module.write_text("a = 1\n", encoding="utf-8")
    recorded = {"source_sha256": source_sha256(tmp_path)}
    assert replay_module._notes(tmp_path, recorded, tmp_path) == []
    module.write_text("a = 2\n", encoding="utf-8")
    notes = replay_module._notes(tmp_path, recorded, tmp_path)
    assert notes == ["the code changed since the recording"]
    assert replay_module._notes(tmp_path, {}, tmp_path) == []  # recorded before D-055


# ---------------------------------------------------------------- who started it (P6.1b, D-081)


async def _starts(repo: Path) -> list[tuple[str, str, str | None]]:
    journal = await Journal.open(repo / "runtime" / "trader.db")
    rows = await journal.fetch_all(
        "SELECT kind, name, detail FROM day_runs WHERE kind IN ('note', 'human') ORDER BY id"
    )
    await journal.aclose()
    return [(r["kind"], r["name"], r["detail"]) for r in rows]


async def test_the_session_records_who_started_it(
    repo: Path, offline: SimClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "DayOrchestrator", Orchestrators())
    await run_day(repo, RunOptions(books=("A",), scheduled=True), print, clock=offline)
    await run_day(repo, RunOptions(books=("A",)), print, clock=offline)  # by hand
    await run_day(repo, RunOptions(books=("A",), dry_run=True), print, clock=offline)
    assert await _starts(repo) == [
        ("note", "scheduled", "A"),
        ("human", "trader run-day", None),
    ]
    journal = await Journal.open(repo / "runtime" / "trader.db")
    await journal.execute(
        "UPDATE trading_days SET run_status = 'DONE' WHERE date = ?", (MORNING.date().isoformat(),)
    )
    await journal.aclose()
    await run_day(repo, RunOptions(books=("A",)), print, clock=offline)  # a finished day: nothing
    await run_day(repo, RunOptions(books=("A",), scheduled=True), print, clock=offline)
    assert len(await _starts(repo)) == 2


async def test_run_day_keeps_the_pc_awake_while_the_day_runs(
    repo: Path,
    offline: SimClock,
    monkeypatch: pytest.MonkeyPatch,
    power_calls: list[int],
) -> None:
    monkeypatch.setattr(live, "DayOrchestrator", Orchestrators())
    await run_day(repo, RunOptions(books=("A",), dry_run=True), print, clock=offline)
    assert power_calls == []  # a dry run only looks
    await run_day(repo, RunOptions(books=("A",)), print, clock=offline)
    assert power_calls == [0x80000001, 0x80000000]  # held, then cleared


class FinishedDays:
    """Stands in for DayOrchestrator: a day that ends in ``state`` after raising a P2, as a
    backup that fails after DONE does."""

    def __init__(self, state: str = "DONE") -> None:
        self.state = state

    def __call__(self, deps: Any) -> Any:
        state = self.state

        class Day:
            async def run(self) -> DayOutcome:
                await record_incident(deps.journal, at=deps.clock.now(), severity="P2",
                                      component="orchestrator", code="backup_failed",
                                      message="the database backup failed (test)")  # fmt: skip
                return DayOutcome(deps.clock.now().date(), state)

        return Day()


def make_files(root: Path, *paths: str) -> None:
    for rel in paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")


async def _alerts(repo: Path) -> list[tuple[str, bool]]:
    journal = await Journal.open(repo / "runtime" / "trader.db")
    rows = await journal.fetch_all("SELECT code, alerted_at FROM incidents ORDER BY id")
    await journal.aclose()
    return [(r["code"], r["alerted_at"] is not None) for r in rows]


async def test_after_done_retention_runs_then_alerts_go_out(
    repo: Path, offline: SimClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Friday's backup_failed, raised after DONE, doesn't wait for Monday's engine to push it
    (D-084)."""
    monkeypatch.setattr(live, "DayOrchestrator", FinishedDays())
    make_files(
        repo / "runtime",
        "logs/2026-08-13.jsonl",  # 61 days before 2026-10-13
        "logs/2026-08-14.jsonl",  # 60
        "recordings/2026-08-01/boot.json",  # epoch 0 keeps it
        "llm/2026-08-01/20260801-vet-01-1.input.json",  # and these
    )
    lines: list[str] = []
    await run_day(repo, RunOptions(books=("A",)), lines.append, clock=offline)
    assert not (repo / "runtime/logs/2026-08-13.jsonl").exists()
    assert (repo / "runtime/logs/2026-08-14.jsonl").is_file()
    assert (repo / "runtime/recordings/2026-08-01/boot.json").is_file()
    assert (repo / "runtime/llm/2026-08-01/20260801-vet-01-1.input.json").is_file()
    assert "retention: deleted 1 (1 B)" in lines
    assert await _alerts(repo) == [("backup_failed", True)]


async def test_a_day_that_did_not_finish_keeps_everything_but_still_alerts(
    repo: Path, offline: SimClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "DayOrchestrator", FinishedDays("REPORT"))
    make_files(repo / "runtime", "logs/2026-08-13.jsonl")
    lines: list[str] = []
    await run_day(repo, RunOptions(books=("A",)), lines.append, clock=offline)
    assert (repo / "runtime/logs/2026-08-13.jsonl").is_file()
    assert not any(line.startswith("retention") for line in lines)
    assert await _alerts(repo) == [("backup_failed", True)]


async def test_a_retention_that_fails_is_a_p3(
    repo: Path, offline: SimClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trader.ops import retention

    def stuck(_entry: object) -> None:
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(live, "DayOrchestrator", Orchestrators())
    monkeypatch.setattr(retention, "_remove", stuck)
    make_files(repo / "runtime", "logs/2026-08-13.jsonl")
    lines: list[str] = []
    await run_day(repo, RunOptions(books=("A",)), lines.append, clock=offline)
    journal = await Journal.open(repo / "runtime" / "trader.db")
    rows = await journal.fetch_all("SELECT severity, code, message FROM incidents")
    await journal.aclose()
    assert [(r["severity"], r["code"]) for r in rows] == [("P3", "retention_failed")]
    assert "runtime/logs/2026-08-13.jsonl" in rows[0]["message"]
    assert "retention: 1 could not be deleted (see the log)" in lines


async def test_a_low_disk_is_a_p2_before_the_engine_starts(
    repo: Path, offline: SimClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    days = Orchestrators()
    monkeypatch.setattr(live, "DayOrchestrator", days)
    monkeypatch.setattr(live, "free_bytes", lambda _path: 2 * 1024**3)
    lines: list[str] = []
    await run_day(repo, RunOptions(books=("A",)), lines.append, clock=offline)
    assert await _alerts(repo) == []  # 2 GiB is enough
    monkeypatch.setattr(live, "free_bytes", lambda _path: 1024**3 + 1024**3 // 2)
    await run_day(repo, RunOptions(books=("A",), dry_run=True), lines.append, clock=offline)
    assert await _alerts(repo) == []  # a dry run only looks
    await run_day(repo, RunOptions(books=("A",)), lines.append, clock=offline)
    await run_day(repo, RunOptions(books=("A",)), lines.append, clock=offline)  # once a day
    journal = await Journal.open(repo / "runtime" / "trader.db")
    rows = await journal.fetch_all("SELECT severity, code, message FROM incidents")
    await journal.aclose()
    assert [(r["severity"], r["code"]) for r in rows] == [("P2", "disk_low")]
    assert rows[0]["message"].startswith("only 1.5 GiB free on the disk holding the journal")
    assert str(repo) not in rows[0]["message"]  # pushed: no local paths
    assert any("1.5 GiB free" in line for line in lines)


async def test_paper_names_the_backup_copy_folder_and_a_simulated_day_never_does(
    repo: Path, offline: SimClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    days = Orchestrators()
    monkeypatch.setattr(live, "DayOrchestrator", days)
    await run_day(repo, RunOptions(books=("A",)), print, clock=offline)
    assert days.deps[-1].paths.backup_copy is None  # not set
    copies = repo / "onedrive" / "trader"
    set_yaml(repo / "config" / "settings.yaml", "storage.backup_copy_dir", str(copies))
    await run_day(repo, RunOptions(books=("A",)), print, clock=offline)
    assert days.deps[-1].paths.backup_copy == copies
    make_files(repo / "runtime", "logs/2026-08-13.jsonl")
    await run_day(repo, RunOptions(books=("A",), sim=True), print, clock=offline)
    assert days.deps[-1].paths.backup_copy is None  # its own day's copy would take the name
    assert (repo / "runtime/logs/2026-08-13.jsonl").is_file()  # nor does it apply retention


def test_the_engine_reads_the_watchdog_once_the_schedule_runs_one(repo: Path) -> None:
    """D-083: no watchdog_silent before G6a installs a watchdog task."""
    import json

    cfg = load_config(repo)
    where = folders(cfg, MORNING.date(), sim=False)
    runtime = repo / "runtime"
    runtime.mkdir(exist_ok=True)
    assert live._watchdog_status(repo, where) is None  # no schedule.json
    record = runtime / "schedule.json"
    record.write_text(json.dumps({"tasks": {r"\ai-day-trader\run-day": "h"}}), encoding="utf-8")
    assert live._watchdog_status(repo, where) is None  # a schedule without a watchdog
    record.write_text(json.dumps({"tasks": {r"\ai-day-trader\watchdog": "h"}}), encoding="utf-8")
    last_run = live._watchdog_status(repo, where)
    assert last_run is not None
    assert last_run() is None  # never ran
    (runtime / "watchdog.json").write_text('{"ts": "2026-10-13T10:01:00-04:00"}', "utf-8")
    assert last_run() == datetime(2026, 10, 13, 10, 1, tzinfo=ET)
    (runtime / "watchdog.json").write_text("{", "utf-8")  # mid-write: unreadable reads as never
    assert last_run() is None
