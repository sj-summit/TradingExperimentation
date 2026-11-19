"""Book B's jobs (orchestrator/llm_jobs.py; P4.8; D-064, D-068, D-069): the brief, the vet and the
review, each with its deadline and fallback; the incidents a call leaves; degradation.

The calls go through the real runner (``LlmCalls``) on a scripted transport whose delays pass on
the SimClock, and Book B's real ``VerdictStore``; the packs come from a stand-in source that
builds real packs from the context-pack tests' inputs. Every job runs under ``simdrive.drive``.
"""

from collections.abc import Awaitable, Sequence
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.exec_harness import CALENDAR, RISK, SETTINGS
from tests.helpers import REPO
from tests.unit.test_llm_context import DAY, PREMARKET, REVIEW, VET, candidate, common, signal
from trader.config import Settings
from trader.core.clock import SimClock, et_datetime, fmt_iso
from trader.core.models import Signal
from trader.llm.budget import Budget
from trader.llm.client import LlmCalls, LLMResult
from trader.llm.context import (
    BookB,
    EncodedPack,
    History,
    Pack,
    VetCandidate,
    premarket_pack,
    review_pack,
    vet_pack,
)
from trader.llm.epoch import Fingerprint
from trader.llm.fake import Reply, ScriptedTransport, Seen, answer_for, envelope
from trader.llm.roles import role_specs
from trader.memory.journal import Journal
from trader.orchestrator.llm_jobs import BRIEF_EDGAR_MIN_S, VET_EDGAR_S, BookBJobs, LlmDeps
from trader.orchestrator.plan import day_plan
from trader.orchestrator.simdrive import drive
from trader.risk.breakers import Halts
from trader.strategy.orb_sip import save_signals

PLAN = day_plan(CALENDAR, DAY, SETTINGS, RISK)


def at(h: int, m: int, s: int = 0) -> datetime:
    return et_datetime(DAY, time(h, m, s))


AAA, BBB, CCC = signal("AAA", 1), signal("BBB", 2), signal("CCC", 3)
START = at(8, 50)  # the day's jobs boot before the 09:00 scan


class Packs:
    """A pack source building real packs: the brief's on ``brief_symbols``, the vet's on the
    signals it is given, the review's from ``review`` (P4.10's stand-in)."""

    def __init__(self, calls: LlmCalls, clock: SimClock) -> None:
        self.calls, self.clock = calls, clock
        self.brief_symbols: list[str] = ["AAA", "BBB"]
        self.scans: list[tuple[datetime, datetime]] = []  # (when, edgar_until)
        self.vets: list[tuple[datetime, list[str], datetime]] = []
        self.books: list[BookB] = []
        self.fail: dict[str, Exception] = {}
        self.vet_takes = timedelta(0)
        self.reviews: Reviews | None = None

    async def premarket_scan(self, *, edgar_until: datetime) -> None:
        self.scans.append((self.clock.now(), edgar_until))
        if "scan" in self.fail:
            raise self.fail["scan"]

    async def brief(self, *, book_b: BookB) -> EncodedPack:
        self.books.append(book_b)
        if "brief" in self.fail:
            raise self.fail["brief"]
        return self.calls.encode(
            premarket_pack(as_of=self.clock.now(), common=common(book_b=book_b),
                           candidates=[candidate(s) for s in self.brief_symbols], evidence={},
                           limits=PREMARKET)
        )  # fmt: skip

    async def vet(
        self, signals: Sequence[Signal], *, book_b: BookB, edgar_until: datetime
    ) -> EncodedPack:
        self.vets.append((self.clock.now(), [s.symbol for s in signals], edgar_until))
        self.books.append(book_b)
        await self.clock.sleep_until(self.clock.now() + self.vet_takes)
        if "vet" in self.fail:
            raise self.fail["vet"]
        candidates = [VetCandidate(signal=s, name=f"{s.symbol} Corp", exchange="NASDAQ")
                      for s in signals]  # fmt: skip
        return self.calls.encode(
            vet_pack(as_of=self.clock.now(), common=common(book_b=book_b), candidates=candidates,
                     limits=VET)
        )  # fmt: skip

    async def review(self, *, book_b: BookB) -> EncodedPack | None:
        if self.reviews is None:
            return None
        pack = await self.reviews.pack(as_of=self.clock.now(), common=common(book_b=book_b))
        return None if pack is None else self.calls.encode(pack)


class Reviews:
    """P4.10's review work, stood in: a pack without targets, and what it was told."""

    def __init__(self) -> None:
        self.persisted: list[str] = []
        self.missing: list[tuple[str, str | None]] = []
        self.none = False

    async def pack(self, *, as_of: datetime, common: Any) -> Pack | None:
        if self.none:
            return None
        return review_pack(
            as_of=as_of,
            common=common,
            books={},
            signals=[],
            targets=[],
            incidents=[],
            stats={},
            lessons=[],
            history=History(),
            limits=REVIEW,
        )

    def persist(self, conn: Any, output: Any, attempt_id: str) -> None:
        self.persisted.append(attempt_id)

    async def not_reviewed(self, *, why: str, result: LLMResult[Any] | None) -> None:
        self.missing.append((why, None if result is None else result.call_id))


class Day:
    """A journal, a SimClock, the runner on a scripted transport, and Book B's jobs."""

    def __init__(self, root: Path, journal: Journal, *, start: datetime = START) -> None:
        self.root, self.journal = root, journal
        self.clock = SimClock(start)
        self.verdicts: dict[str, str] = {}
        self.day_mode = "normal"
        self.replies: dict[str, list[Reply]] = {}  # role -> replies taken first
        self.delay = 2.0
        self.transport = ScriptedTransport(self._reply, sleep=self._sleep)
        self.calls = self._calls()
        self.packs = Packs(self.calls, self.clock)
        self.reviews: Reviews | None = None
        self.halts = Halts(journal, self.clock)
        self.news: Any = None
        self.fingerprint: Fingerprint | None = None
        self.jobs = self.new_jobs()

    def _calls(self, settings: Settings = SETTINGS) -> LlmCalls:
        return LlmCalls(
            journal=self.journal, clock=self.clock, transport=self.transport,
            command=("fake",), roles=role_specs(settings), env={},
            prompts_dir=REPO / "prompts", sandbox_dir=self.root / "sandbox",
            artifacts_dir=self.root / "llm",
            budget=Budget(self.journal, max_calls_per_day=12, max_concurrent=1),
        )  # fmt: skip

    def new_jobs(self) -> BookBJobs:
        """Book B's jobs, as a (re)start builds them."""
        packs = self.packs
        deps = LlmDeps(calls=self.calls, packs=lambda ctx: packs, news=self.news,
                       review=self.reviews, fingerprint=self.fingerprint)  # fmt: skip
        self.jobs = BookBJobs(deps=deps, journal=self.journal, clock=self.clock, halts=self.halts,
                              plan=PLAN, settings=SETTINGS, risk=RISK,
                              equity=lambda: Decimal("1000"))  # fmt: skip
        return self.jobs

    async def _sleep(self, seconds: float) -> None:
        await self.clock.sleep_until(self.clock.now() + timedelta(seconds=seconds))

    def _reply(self, seen: Seen) -> Reply:
        role = seen.pack()["role"]
        queued = self.replies.get(role)
        if queued:
            return queued.pop(0)
        out = answer_for(seen.pack())
        if "assessments" in out:
            out["assessments"] = [{**a, "verdict": self.verdicts.get(a["symbol"], "neutral")}
                                  for a in out["assessments"]]  # fmt: skip
        if "day_mode" in out:
            out["day_mode"] = self.day_mode
        return Reply(envelope=envelope(out), delay_s=self.delay)

    async def run[T](self, job: Awaitable[T]) -> T:
        return await drive(self.clock, job, watched=[self.journal])

    async def boot(self) -> None:
        await self.run(self.jobs.boot(None))  # type: ignore[arg-type]  # the stand-in takes none

    async def signals(self, *signals: Signal) -> None:
        await save_signals(self.journal, list(signals))

    async def rows(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        return [tuple(r) for r in await self.journal.fetch_all(sql, params)]

    async def incidents(self) -> list[tuple[str, str]]:
        return [
            (s, c) for s, c in await self.rows("SELECT severity, code FROM incidents ORDER BY id")
        ]

    async def calls_made(self) -> list[tuple[str, str, int]]:
        return [(r, i, ok) for r, i, ok in await self.rows(
            "SELECT role, id, ok FROM llm_calls ORDER BY id")]  # fmt: skip


@pytest.fixture
async def day(tmp_path: Path) -> Any:
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    yield Day(tmp_path, journal)
    await journal.aclose()


def invalid() -> Reply:
    return Reply(stdout=b"not json", delay_s=1.0)


# ---------------------------------------------------------------- the brief


async def test_the_brief_comes_after_the_0900_scan_with_its_0928_deadline(day: Day) -> None:
    day.day_mode = "reduced"
    day.verdicts = {"AAA": "veto"}
    await day.boot()
    await day.run(day.jobs.morning())
    assert day.packs.scans == [(at(9, 0), at(9, 5))]  # EDGAR's budget: until the brief
    assert await day.rows("SELECT role, started_at, deadline FROM llm_calls") == [
        ("premarket", fmt_iso(at(9, 5)), fmt_iso(at(9, 28))),
    ]
    mode = day.jobs.store.day_mode()
    assert (mode.mode, mode.source, mode.multiplier) == ("reduced", "premarket", Decimal("0.5"))
    assert day.jobs.store.standing(AAA).verdict.verdict == "veto"  # type: ignore[union-attr]
    assert day.packs.books == [BookB(equity=Decimal("1000"), recent_r=[])]  # no mode in the brief
    assert await day.incidents() == []


async def test_a_restart_never_asks_for_the_brief_again(day: Day) -> None:
    await day.boot()
    await day.run(day.jobs.morning())
    day.new_jobs()
    await day.boot()
    await day.run(day.jobs.morning())
    assert [role for role, _, _ in await day.calls_made()] == ["premarket"]


async def test_a_brief_that_failed_is_not_asked_again_after_a_restart(day: Day) -> None:
    day.replies["premarket"] = [invalid(), invalid()]
    await day.boot()
    await day.run(day.jobs.morning())
    day.new_jobs()
    await day.boot()
    await day.run(day.jobs.morning())
    assert len(await day.calls_made()) == 2  # the attempt and its repair, once
    assert day.jobs.store.day_mode().source == "default"


async def test_a_start_too_late_for_the_brief_leaves_the_default_mode(tmp_path: Path) -> None:
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    late = Day(tmp_path, journal, start=at(9, 27, 1))  # 60 s before 09:28 is the latest start
    await late.boot()
    await late.run(late.jobs.morning())
    assert await late.calls_made() == []
    assert await late.rows("SELECT mode, source, llm_call_id FROM day_modes") == [
        ("normal", "default", None)
    ]
    assert await late.incidents() == [("P3", "brief_missed")]
    late.new_jobs()  # a restart: the default stands and nothing new is raised
    await late.boot()
    await late.run(late.jobs.morning())
    assert await late.incidents() == [("P3", "brief_missed")]
    await journal.aclose()


async def test_a_late_start_scans_at_once_and_gives_edgar_its_minimum(tmp_path: Path) -> None:
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    late = Day(tmp_path, journal, start=at(9, 10))
    await late.boot()
    await late.run(late.jobs.morning())
    assert late.packs.scans == [(at(9, 10), at(9, 10) + timedelta(seconds=BRIEF_EDGAR_MIN_S))]
    assert await late.rows("SELECT started_at FROM llm_calls") == [(fmt_iso(at(9, 10)),)]
    await journal.aclose()


async def test_edgar_never_eats_the_time_the_brief_needs(tmp_path: Path) -> None:
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    late = Day(tmp_path, journal, start=at(9, 26, 45))
    await late.boot()
    await late.run(late.jobs.morning())
    assert late.packs.scans == [(at(9, 26, 45), at(9, 27))]  # capped at 09:28 - 60 s
    await journal.aclose()


async def test_a_failed_scan_is_a_p3_and_the_brief_still_runs(day: Day) -> None:
    day.packs.fail["scan"] = RuntimeError("snapshots failed")
    await day.boot()
    await day.run(day.jobs.morning())
    assert await day.incidents() == [("P3", "premarket_scan_failed")]
    assert [role for role, _, _ in await day.calls_made()] == ["premarket"]


async def test_a_brief_job_that_raises_is_a_p2_with_the_default_mode(day: Day) -> None:
    day.packs.fail["brief"] = ValueError("a bug")
    await day.boot()
    await day.run(day.jobs.morning())
    assert await day.incidents() == [("P2", "llm_job_failed")]
    assert await day.rows("SELECT mode, source FROM day_modes") == [("normal", "default")]
    assert await day.calls_made() == []


async def test_a_brief_that_times_out_leaves_normal_and_is_a_p3(day: Day) -> None:
    day.replies["premarket"] = [Reply(envelope=envelope({}), delay_s=10_000)]
    await day.boot()
    await day.run(day.jobs.morning())
    assert await day.rows("SELECT failure, final FROM llm_calls") == [("timeout", 1)]
    assert day.clock.now() == at(9, 5) + timedelta(seconds=600)  # the role's own timeout
    assert await day.rows("SELECT mode, source, reason FROM day_modes") == [
        ("normal", "default", "the brief failed: timeout")
    ]
    assert await day.incidents() == [("P3", "llm_call_failed")]


# ---------------------------------------------------------------- the vet


async def test_the_vet_holds_its_signals_pending_from_arming_until_it_answers(day: Day) -> None:
    await day.signals(AAA, BBB)
    day.verdicts = {"BBB": "veto"}
    await day.boot()
    await day.run(day.clock.sleep_until(at(9, 35, 5)))
    job = day.jobs.start_vet([AAA, BBB])
    assert job is not None
    assert job.symbols == ("AAA", "BBB")
    assert day.jobs.store.standing(AAA).kind == "pending"  # before the call even starts
    await day.run(day.jobs.vets_finished())
    assert day.packs.vets == [
        (at(9, 35, 5), ["AAA", "BBB"], at(9, 35, 5) + timedelta(seconds=VET_EDGAR_S))
    ]
    assert await day.rows("SELECT role, deadline FROM llm_calls") == [
        ("vet", fmt_iso(PLAN.times.last_entry))
    ]
    standing = day.jobs.store.standing(BBB)
    assert standing.verdict is not None
    assert standing.verdict.verdict == "veto"
    assert await day.rows("SELECT symbol, signal_id FROM verdicts ORDER BY symbol") == [
        ("AAA", AAA.id), ("BBB", BBB.id),
    ]  # fmt: skip
    mode = day.packs.books[-1]
    assert (mode.day_mode, mode.day_mode_source) == ("normal", "default")  # no brief ran


async def test_a_standing_brief_verdict_is_not_vetted_again(day: Day) -> None:
    await day.signals(AAA, BBB, CCC)
    day.packs.brief_symbols = ["AAA"]
    await day.boot()
    await day.run(day.jobs.morning())
    await day.run(day.clock.sleep_until(at(9, 35, 5)))
    job = day.jobs.start_vet([AAA, BBB, CCC])
    assert job is not None
    assert job.symbols == ("BBB", "CCC")
    await day.run(day.jobs.vets_finished())
    assert day.jobs.start_vet([AAA, BBB, CCC]) is None  # judged: nothing more to vet


async def test_a_vet_job_that_raises_leaves_its_signals_unavailable_at_once(day: Day) -> None:
    await day.signals(AAA)
    day.packs.fail["vet"] = KeyError("name")
    await day.boot()
    day.jobs.start_vet([AAA])
    await day.run(day.jobs.vets_finished())
    standing = day.jobs.store.standing(AAA)
    assert (standing.kind, standing.detail) == ("unavailable", "the vet failed: internal_error")
    assert await day.incidents() == [("P2", "llm_job_failed")]
    assert await day.calls_made() == []


async def test_a_vet_cancelled_mid_call_is_vetted_again(day: Day) -> None:
    await day.signals(AAA)
    day.delay = 50.0
    await day.boot()
    await day.run(day.clock.sleep_until(at(9, 35, 5)))
    day.jobs.start_vet([AAA])
    await day.run(day.clock.sleep_until(at(9, 35, 20)))  # the call is under way
    await day.run(day.jobs.close())
    standing = day.jobs.store.standing(AAA)
    assert standing.kind == "pending"  # abandoned: to be vetted again, from now
    assert standing.deadline == at(9, 35, 20) + timedelta(seconds=60)
    assert await day.rows("SELECT failure FROM llm_calls") == [("abandoned",)]
    assert await day.incidents() == []  # an abandoned attempt isn't a failure


async def test_nothing_to_vet_makes_no_call(day: Day) -> None:
    await day.boot()
    assert day.jobs.start_vet([]) is None
    short = signal("SSS", 4, direction="shadow_short")
    assert day.jobs.start_vet([short]) is None


# ---------------------------------------------------------------- incidents and degradation


async def test_each_failed_attempt_is_a_p3(day: Day) -> None:
    await day.signals(AAA)
    day.replies["vet"] = [invalid(), invalid()]
    await day.boot()
    day.jobs.start_vet([AAA])
    await day.run(day.jobs.vets_finished())
    assert await day.incidents() == [("P3", "llm_call_failed"), ("P3", "llm_call_failed")]
    messages = [m for (m,) in await day.rows("SELECT message FROM incidents ORDER BY id")]
    assert messages[0].startswith("vet attempt 20261103-vet-01-1: invalid_json")
    assert day.jobs.store.standing(AAA).detail.startswith("the vet failed: invalid_json")


async def test_a_refusal_a_human_must_fix_is_a_p2(day: Day) -> None:
    await day.signals(AAA)
    (day.root / "sandbox").mkdir()
    (day.root / "sandbox" / "CLAUDE.md").write_text("stray", encoding="utf-8")
    await day.boot()
    day.jobs.start_vet([AAA])
    await day.run(day.jobs.vets_finished())
    assert await day.incidents() == [("P2", "llm_refused")]
    (message,) = await day.rows("SELECT message FROM incidents")
    assert "sandbox_dirty" in message[0]
    assert "until a human fixes it" in message[0]


async def test_a_call_refused_before_any_attempt_is_a_p3(day: Day) -> None:
    await day.signals(AAA)
    await day.boot()
    rows = [(f"20261103-x-{i:02d}-1", fmt_iso(at(8, 0))) for i in range(12)]
    await day.journal.write(lambda conn: conn.executemany(
        "INSERT INTO llm_calls (id, date, role, logical_id, attempt, final, ok, purpose, "
        "started_at, ended_at) VALUES (?, '2026-11-03', 'vet', ?, 1, 1, 1, 'trading', ?, ?)",
        [(i, i[:-2], t, t) for i, t in rows]))  # fmt: skip
    day.jobs.start_vet([AAA])
    await day.run(day.jobs.vets_finished())
    assert await day.incidents() == [("P3", "llm_call_refused")]
    assert day.jobs.store.standing(AAA).detail == "the vet failed: budget_exhausted"


async def test_degradation_halts_book_b_until_the_next_open_with_one_p2(day: Day) -> None:
    await day.signals(AAA, BBB)
    day.replies["premarket"] = [invalid(), invalid()]
    day.replies["vet"] = [invalid(), invalid()]
    await day.boot()
    await day.run(day.jobs.morning())
    assert day.jobs.store.failures == 2
    assert await day.rows("SELECT reason FROM halts") == []
    day.jobs.start_vet([AAA, BBB])
    await day.run(day.jobs.vets_finished())
    assert day.jobs.store.degraded
    assert await day.rows("SELECT book, reason, scope, expires_at FROM halts") == [
        ("B", "llm_degraded", "session", fmt_iso(PLAN.next_open))
    ]
    assert [c for c in await day.incidents() if c[0] == "P2"] == [("P2", "llm_degraded")]
    day.new_jobs()  # a restart doesn't raise it again
    await day.boot()
    assert [c for c in await day.incidents() if c[0] == "P2"] == [("P2", "llm_degraded")]


async def test_a_restart_halts_a_book_degraded_before_its_halt(day: Day) -> None:
    await day.signals(AAA)
    for i in range(3):  # three failed attempts, journaled before a crash
        await day.journal.execute(
            "INSERT INTO llm_calls (id, date, role, logical_id, attempt, final, ok, failure, "
            "purpose, started_at, ended_at) VALUES (?, '2026-11-03', 'vet', ?, 1, 1, 0, "
            "'timeout', 'trading', ?, ?)",
            (f"20261103-vet-{i:02d}-1", f"20261103-vet-{i:02d}", fmt_iso(at(9, 36)),
             fmt_iso(at(9, 37))),
        )  # fmt: skip
    await day.boot()
    assert await day.rows("SELECT reason FROM halts") == [("llm_degraded",)]
    assert await day.incidents() == [("P2", "llm_degraded")]


# ---------------------------------------------------------------- boot


async def test_boot_closes_open_attempts_and_notes_the_setup(tmp_path: Path) -> None:
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    await journal.execute(
        "INSERT INTO llm_calls (id, date, role, logical_id, attempt, final, purpose, started_at) "
        "VALUES ('20261103-vet-01-1', '2026-11-03', 'vet', '20261103-vet-01', 1, 0, 'trading', "
        "'2026-11-03T09:35:06-05:00')"
    )
    first = Day(tmp_path, journal)
    first.fingerprint = Fingerprint("aaa", {"roles": {"vet": {"model": "sonnet"}}})
    first.new_jobs()
    await first.boot()
    assert await first.rows("SELECT failure, final, ok FROM llm_calls") == [("abandoned", 1, 0)]
    assert await first.rows("SELECT value FROM meta WHERE key = 'llm_fingerprint'") == [("aaa",)]
    second = Day(tmp_path, journal)
    second.fingerprint = Fingerprint("bbb", {"roles": {"vet": {"model": "haiku"}}})
    second.new_jobs()
    await second.boot()
    assert await second.incidents() == [("P3", "llm_fingerprint_changed")]
    await journal.aclose()


class News:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.started, self.stopped = fail, [], 0  # type: ignore[var-annotated]

    async def start(self, lookback: timedelta) -> int:
        self.started.append(lookback)
        if self.fail:
            raise OSError("no network")
        return 7

    async def stop(self) -> None:
        self.stopped += 1

    async def recent(self, symbols: Any, since: datetime) -> list[Any]:
        return []


async def test_news_starts_in_the_background_with_a_day_of_backfill(day: Day) -> None:
    """D-069: boot never waits for the backfill; the day starts the feed once the stock
    stream is up."""
    day.news = News()
    day.new_jobs()
    await day.boot()
    assert day.news.started == []  # not at boot
    day.jobs.start_news()
    day.jobs.start_news()  # once only
    await day.run(day.clock.sleep_until(day.clock.now() + timedelta(seconds=1)))
    assert day.news.started == [timedelta(hours=24)]  # the vet's 24 h window, the longer one
    await day.run(day.jobs.close())
    assert day.news.stopped == 1


async def test_news_that_does_not_start_is_a_p3_once_and_tried_every_minute(day: Day) -> None:
    day.news = News(fail=True)
    day.new_jobs()
    await day.boot()
    day.jobs.start_news()
    await day.run(day.clock.sleep_until(START + timedelta(seconds=150)))
    assert len(day.news.started) == 3  # at 0, 60 and 120 s
    assert await day.incidents() == [("P3", "news_unavailable")]
    day.news.fail = False
    await day.run(day.clock.sleep_until(START + timedelta(seconds=200)))
    assert len(day.news.started) == 4
    await day.run(day.jobs.close())
    assert day.news.stopped == 1


async def test_closing_stops_a_news_start_still_trying(day: Day) -> None:
    day.news = News(fail=True)
    day.new_jobs()
    await day.boot()
    day.jobs.start_news()
    await day.run(day.clock.sleep_until(START + timedelta(seconds=30)))
    await day.run(day.jobs.close())
    assert day.news.stopped == 0  # it never started
    assert day.clock.next_wakeup() is None  # no retry is left waiting


async def test_disabled_jobs_ask_nothing(day: Day) -> None:
    """Book B's boot failed: the day halts it, and its jobs make no call (D-069)."""
    await day.signals(AAA)
    day.reviews = Reviews()
    day.packs.reviews = day.reviews
    day.news = News()
    day.new_jobs()
    await day.boot()
    day.jobs.disable("Book B's boot failed (OSError)")
    await day.run(day.jobs.morning())
    assert day.jobs.start_vet([AAA]) is None
    day.jobs.start_news()
    await day.run(day.jobs.review())
    assert (await day.calls_made(), day.packs.scans, day.news.started) == ([], [], [])


# ---------------------------------------------------------------- boundaries and timing


async def test_the_brief_s_latest_start_is_09_27_exactly(tmp_path: Path) -> None:
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    edge = Day(tmp_path, journal, start=at(9, 27))
    await edge.boot()
    await edge.run(edge.jobs.morning())
    assert await edge.calls_made() == []
    assert edge.packs.scans == []  # not even the scan: its snapshots would be for nothing
    assert [m for (m,) in await edge.rows("SELECT message FROM incidents")] == [
        "no brief today: started too late for the brief (by 09:27:00); day mode normal, the vet "
        "judges every signal"
    ]
    await journal.aclose()


async def test_a_scan_that_runs_past_the_latest_start_misses_the_brief(day: Day) -> None:
    async def slow_scan(*, edgar_until: datetime) -> None:
        await day.clock.sleep_until(at(9, 27, 30))

    day.packs.premarket_scan = slow_scan  # type: ignore[method-assign]
    await day.boot()
    await day.run(day.jobs.morning())
    assert await day.calls_made() == []
    messages = [m for (m,) in await day.rows("SELECT message FROM incidents")]
    assert messages == [
        "no brief today: the pre-market scan ran past 09:27:00; day mode normal, the vet judges "
        "every signal"
    ]


async def test_the_review_s_latest_start_is_16_27_exactly(tmp_path: Path) -> None:
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    edge = Day(tmp_path, journal, start=at(16, 27))
    edge.reviews = Reviews()
    edge.packs.reviews = edge.reviews
    edge.new_jobs()
    await edge.boot()
    await edge.run(edge.jobs.review())
    assert edge.reviews.missing == [("no time before 16:29", None)]
    await journal.aclose()


async def test_the_vet_s_wait_counts_from_the_call_not_from_its_pack(day: Day) -> None:
    """Plan decision 8: max(armed_at, the covering call's start) + 60 s. The pack's EDGAR time
    comes before the call, so it doesn't eat into the wait."""
    await day.signals(AAA)
    day.packs.vet_takes = timedelta(seconds=8)
    day.delay = 30.0
    await day.boot()
    await day.run(day.clock.sleep_until(at(9, 35, 5)))
    day.jobs.start_vet([AAA])
    await day.run(day.clock.sleep_until(at(9, 35, 20)))  # the call is under way
    standing = day.jobs.store.standing(AAA)
    assert (standing.kind, standing.deadline) == ("pending", at(9, 35, 13) + timedelta(seconds=60))
    assert await day.rows("SELECT started_at FROM llm_calls") == [(fmt_iso(at(9, 35, 13)),)]
    await day.run(day.jobs.vets_finished())


async def test_a_candidate_the_vet_s_pack_had_no_room_for_is_not_sent(day: Day) -> None:
    """The store is told what the pack sent, not what the job asked: a symbol the pack left
    out is ``llm_unavailable`` "no room", not "the answer left it out"."""
    await day.signals(AAA, BBB)
    real = day.packs.vet

    async def without_bbb(signals: Sequence[Signal], **kw: Any) -> EncodedPack:
        return await real([s for s in signals if s.symbol != "BBB"], **kw)

    day.packs.vet = without_bbb  # type: ignore[method-assign]
    await day.boot()
    day.jobs.start_vet([AAA, BBB])
    await day.run(day.jobs.vets_finished())
    assert day.jobs.store.standing(BBB).detail == "the vet's pack had no room for it"
    assert day.jobs.store.standing(AAA).kind == "verdict"


async def test_a_vet_that_cannot_start_is_a_p2(day: Day, monkeypatch: pytest.MonkeyPatch) -> None:
    await day.boot()

    def broken(signals: Any) -> list[Signal]:
        raise RuntimeError("a bug")

    monkeypatch.setattr(day.jobs.store, "vet_targets", broken)
    assert day.jobs.start_vet([AAA]) is None
    await day.run(day.jobs.vets_finished())
    assert await day.incidents() == [("P2", "llm_job_failed")]


async def test_the_packs_get_book_b_s_own_recent_r(day: Day) -> None:
    rows = [("B", "2026-10-30T10:00:00-04:00", 0.5, "tb"), ("A", "2026-11-02T10:00:00-05:00", 9.0,
            "ta")]  # fmt: skip
    sql = "INSERT INTO trades (book, entry_ts, r_multiple, id) VALUES (?, ?, ?, ?)"
    await day.journal.write(lambda conn: conn.executemany(sql, rows))
    await day.boot()
    await day.run(day.jobs.morning())
    assert day.packs.books[0].recent_r == [0.5]


async def test_an_incident_names_the_failure_s_detail(day: Day) -> None:
    await day.signals(AAA)
    day.replies["vet"] = [invalid(), invalid()]
    await day.boot()
    day.jobs.start_vet([AAA])
    await day.run(day.jobs.vets_finished())
    (first,) = await day.rows("SELECT message FROM incidents ORDER BY id LIMIT 1")
    assert first[0] == (
        "vet attempt 20261103-vet-01-1: invalid_json: The output was not a JSON result."
    )


# ---------------------------------------------------------------- the review


async def test_without_review_work_there_is_no_review(day: Day) -> None:
    await day.boot()
    await day.run(day.jobs.review())
    assert await day.calls_made() == []
    assert await day.rows("SELECT name FROM day_runs") == []


async def test_the_review_runs_once_at_1615_with_the_report_as_cutoff(day: Day) -> None:
    day.reviews = Reviews()
    day.packs.reviews = day.reviews
    day.new_jobs()
    await day.boot()
    await day.run(day.jobs.review())
    assert await day.rows("SELECT role, started_at, deadline FROM llm_calls") == [
        ("review", fmt_iso(at(16, 15)), fmt_iso(at(16, 29)))
    ]
    assert day.reviews.persisted == ["20261103-review-01-1"]
    assert await day.rows("SELECT name, detail FROM day_runs") == [("review", "ok")]
    await day.run(day.jobs.review())  # a rerun of the day
    assert len(await day.calls_made()) == 1


async def test_a_review_too_late_to_start_is_recorded_as_not_reviewed(tmp_path: Path) -> None:
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    late = Day(tmp_path, journal, start=at(16, 27, 1))  # 120 s before 16:29
    late.reviews = Reviews()
    late.packs.reviews = late.reviews
    late.new_jobs()
    await late.boot()
    await late.run(late.jobs.review())
    assert await late.calls_made() == []
    assert late.reviews.missing == [("no time before 16:29", None)]
    assert await late.rows("SELECT name, detail FROM day_runs") == [("review", "no time")]
    await journal.aclose()


async def test_a_failed_review_is_recorded_as_not_reviewed(day: Day) -> None:
    day.reviews = Reviews()
    day.packs.reviews = day.reviews
    day.replies["review"] = [invalid(), invalid()]
    day.new_jobs()
    await day.boot()
    await day.run(day.jobs.review())
    assert day.reviews.missing == [("invalid_json", "20261103-review-01-2")]
    assert day.reviews.persisted == []
    assert await day.rows("SELECT name, detail FROM day_runs") == [("review", "invalid_json")]
    assert await day.incidents() == [("P3", "llm_call_failed"), ("P3", "llm_call_failed")]


async def test_a_failed_review_never_degrades_book_b_after_the_close(day: Day) -> None:
    """The day's entries are over: three failed attempts by then raise no halt and no P2."""
    await day.signals(AAA)
    day.reviews = Reviews()
    day.packs.reviews = day.reviews
    day.replies["vet"] = [Reply(stdout=b"x", delay_s=1.0, exit_code=1)]  # one failed attempt
    day.replies["review"] = [invalid(), invalid()]
    day.new_jobs()
    await day.boot()
    day.jobs.start_vet([AAA])
    await day.run(day.jobs.vets_finished())
    await day.run(day.jobs.review())
    assert day.jobs.store.failures == 1  # the review's attempts weren't counted into a halt
    assert await day.rows("SELECT reason FROM halts") == []
    assert ("P2", "llm_degraded") not in await day.incidents()


async def test_a_good_review_is_marked_in_its_own_transaction(
    day: Day, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan decision 11: a crash after the answer is stored can't make a rerun ask again."""
    day.reviews = Reviews()
    day.packs.reviews = day.reviews
    day.new_jobs()
    await day.boot()

    async def crash(*args: Any, **kw: Any) -> None:
        raise AssertionError("no separate marker write after a good review")

    monkeypatch.setattr("trader.orchestrator.llm_jobs.mark_step", crash)
    await day.run(day.jobs.review())
    assert await day.rows("SELECT name, detail, at FROM day_runs") == [
        ("review", "ok", fmt_iso(at(16, 15, 2)))
    ]


async def test_a_review_with_nothing_to_review_makes_no_call(day: Day) -> None:
    day.reviews = Reviews()
    day.reviews.none = True
    day.packs.reviews = day.reviews
    day.new_jobs()
    await day.boot()
    await day.run(day.jobs.review())
    assert await day.calls_made() == []
    assert await day.rows("SELECT name, detail FROM day_runs") == [("review", "nothing to review")]


def test_the_budgets_leave_room_inside_book_bs_wait() -> None:
    """The vet's EDGAR budget delays the call that every pending trigger waits on, so it must
    stay well inside the 60 s wait (S§5.4)."""
    assert SETTINGS.llm.roles.vet.max_wait_s / 4 >= VET_EDGAR_S
