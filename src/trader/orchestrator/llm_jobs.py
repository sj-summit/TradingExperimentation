"""Book B's LLM jobs (A§3; S§5.2-5.4, S§6.1; RISK.md §2, §11; the Phase 4 plan's decisions 4, 5, 8
and 9; D-064, D-068, D-069).

``BookBJobs`` runs Book B's part of the day around its ``VerdictStore``:

- **Boot:** close the attempts a crash left open (``close_abandoned``), note the LLM setup's
  fingerprint (a P3 when it changed, D-065), and rebuild the store from the journal
  (``VerdictStore.load``). The news feed starts later, in the background, once the market-data
  stream is connected (``start_news``): its day of backfill never delays the books' boot, and
  the stock stream gets the first connection if the plan allows only one (Q2). A start that
  fails is tried again every minute.
- **Disabled** (``disable``) when Book B's boot failed: no brief, vet or review, and the day
  halts Book B; its account is still reconciled, protected and flattened.
- **Morning:** the 09:00 pre-market scan, then the 09:05 brief, its deadline 09:28. A brief that
  already ended today (a restart) isn't asked again. One that can't start in time, or a job that
  fails, leaves the default day mode (normal) and every signal to the vet (S§5.2).
- **The vet, at arming:** ``start_vet`` marks the armed longs without a standing verdict pending
  (``expect_vet``) before any of their triggers can reach Book B, then runs the call in the
  background: the pack (EDGAR within ``VET_EDGAR_S``), ``vet_started``, the runner's call with
  the last entry as its deadline, ``vet_done``. A cancelled call is ``vet_abandoned``; a job that
  fails ends as a failed call, so its signals are ``llm_unavailable`` rather than waiting out
  their deadline.
- **The review, after the shadow job:** at 16:15, the cutoff a minute before the report, only
  with ``ReviewWork`` (P4.10). It runs once a day: its step is marked in ``day_runs``, in the
  same transaction that stores a good answer, so a crash can't make a rerun ask again.

After each call:
- a P3 ``llm_call_failed`` for each failed attempt; a P2 ``llm_refused`` for one refused for a
  cause a human must fix (a dirty sandbox, a missing prompt, a pack over its cap; D-064); a P3
  ``llm_call_refused`` for a call refused before any attempt (no time, the day's cap);
- once the day's failed attempts reach ``llm_failures_degrade_after``: Book B halted with
  ``llm_degraded`` until the next open, and one P2 ``llm_degraded``. Book B doesn't wait for the
  halt: it re-reads the count before every decision (D-068). Not after the review: the day's
  entries are over, and the alert would only be noise.

No job raises into the day: Book A never depends on the LLM (S§5.4).
"""

import asyncio
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

import structlog
from pydantic import BaseModel

from trader.books.verdicts import Rebuilt, VerdictStore, VetJob
from trader.config import RiskConfig, Settings
from trader.core.clock import Clock, fmt_iso
from trader.core.models import Signal
from trader.llm.budget import ABANDONED, PERSISTENT_REFUSALS
from trader.llm.client import LlmCalls, LLMResult
from trader.llm.context import BookB
from trader.llm.epoch import Fingerprint, note_fingerprint
from trader.memory.incidents import Severity, record_incident
from trader.memory.journal import Journal
from trader.orchestrator.eod import mark_step, step_done
from trader.orchestrator.packs import PackContext, PackFactory, recent_r
from trader.orchestrator.plan import DayPlan
from trader.orchestrator.ports import NewsFeed, PackSource, ReviewWork
from trader.risk.breakers import Halts

log = structlog.get_logger(__name__)

VET_EDGAR_S = 10.0  # the vet asks EDGAR for this long at most: its pending triggers wait on it
BRIEF_EDGAR_MIN_S = 30.0  # a brief started late still gives EDGAR this long, time allowing
NEWS_RETRY_S = 60.0  # a news feed that didn't start is tried again this often
REVIEW_STEP = "review"
MESSAGE_CHARS = 300


@dataclass(frozen=True)
class LlmDeps:
    """What run-day, a simulated day or a replay gives the day for Book B."""

    calls: LlmCalls
    packs: PackFactory
    news: NewsFeed | None = None  # started once the stock stream is up, stopped at the end
    review: ReviewWork | None = None  # P4.10's memory/reviews.py
    fingerprint: Fingerprint | None = None  # noted at boot (D-065); a replay passes none
    setup: Mapping[str, Any] = field(default_factory=dict)  # boot.json's "llm" section


class BookBJobs:
    def __init__(
        self,
        *,
        deps: LlmDeps,
        journal: Journal,
        clock: Clock,
        halts: Halts,
        plan: DayPlan,
        settings: Settings,
        risk: RiskConfig,
        equity: Callable[[], Decimal | None],
    ) -> None:
        self._deps, self._calls = deps, deps.calls
        self._journal, self._clock, self._halts, self._plan = journal, clock, halts, plan
        self._settings, self._risk, self._equity = settings, risk, equity
        vet = settings.llm.roles.vet
        self.store = VerdictStore(
            journal,
            clock,
            day=plan.day,
            max_wait_s=vet.max_wait_s,
            revet_premarket=vet.revet_premarket,
            degrade_after=risk.llm_failures_degrade_after,
            last_entry=plan.times.last_entry,
        )
        self.rebuilt = Rebuilt(brief="none", revet=())
        self._packs: PackSource | None = None
        self._vets: list[asyncio.Task[None]] = []
        self._news_task: asyncio.Task[None] | None = None
        self._news_started = False
        self.disabled: str | None = None  # why Book B's jobs don't run today

    @property
    def packs(self) -> PackSource:
        if self._packs is None:
            raise RuntimeError("Book B's jobs have no pack source before boot")
        return self._packs

    # ------------------------------------------------------------ boot

    async def boot(self, ctx: PackContext) -> Rebuilt:
        closed = await self._calls.close_abandoned()
        if closed:
            log.info("llm attempts a crash left open closed", attempts=closed)
        if self._deps.fingerprint is not None:
            await note_fingerprint(self._journal, self._deps.fingerprint, at=self._clock.now())
        self.rebuilt = await self.store.load()
        self._packs = self._deps.packs(ctx)
        await self._check_degraded()  # a restart after the failure but before its halt
        log.info("book B ready", brief=self.rebuilt.brief, revet=len(self.rebuilt.revet),
                 failures=self.store.failures)  # fmt: skip
        return self.rebuilt

    @property
    def reviews(self) -> bool:
        """Whether today has a review: its content is wired (P4.10) and Book B is on."""
        return self._deps.review is not None and not self.disabled

    def disable(self, why: str) -> None:
        """No brief, vet or review today (Book B's boot failed; the day halts the book)."""
        self.disabled = why

    def start_news(self) -> None:
        """Start the news feed in the background: after the books' boot and the stock stream
        (the module's docstring). A start that fails is tried again every ``NEWS_RETRY_S``."""
        if self._deps.news is None or self.disabled or self._news_task is not None:
            return
        self._news_task = asyncio.create_task(self._news(), name="news-start")

    async def _news(self) -> None:
        news = self._deps.news
        assert news is not None
        hours = max(self._settings.news.premarket_lookback_hours,
                    self._settings.news.vet_lookback_hours)  # fmt: skip
        alerted = False
        while True:
            try:
                backfilled = await news.start(timedelta(hours=hours))
            except Exception as exc:
                log.warning("news did not start", error=f"{type(exc).__name__}: {exc}")
                if not alerted:
                    alerted = True
                    await self._incident("P3", "news_unavailable",
                                         f"news did not start ({type(exc).__name__}): Book B's "
                                         "packs say so until it does")  # fmt: skip
                now = self._clock.now()
                await self._clock.sleep_until(now + timedelta(seconds=NEWS_RETRY_S))
                continue
            self._news_started = True
            log.info("news started", backfilled=backfilled)
            return

    # ------------------------------------------------------------ the brief

    async def morning(self) -> None:
        """The 09:00 scan and the 09:05 brief (the module's docstring). Never raises."""
        try:
            await self._morning()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("the brief's job failed")
            await self._incident("P2", "llm_job_failed",
                                 f"the brief's job failed ({type(exc).__name__}): day mode normal, "
                                 "the vet judges every signal")  # fmt: skip
            await self.store.brief_missed()

    async def _morning(self) -> None:
        if self.disabled or self.store.mode_known:
            return  # the brief ended, or was missed, before a restart: never asked twice
        plan, clock = self._plan, self._clock
        latest = plan.brief_deadline - timedelta(
            seconds=self._settings.llm.roles.premarket.min_attempt_s
        )
        if clock.now() >= latest:
            await self._brief_missed(f"started too late for the brief (by {latest:%H:%M:%S})")
            return
        await clock.sleep_until(plan.premarket_scan_at)
        edgar_until = min(
            max(plan.brief_at, clock.now() + timedelta(seconds=BRIEF_EDGAR_MIN_S)), latest
        )
        try:
            await self.packs.premarket_scan(edgar_until=edgar_until)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("pre-market scan failed")
            await self._incident("P3", "premarket_scan_failed",
                                 f"the pre-market scan failed ({type(exc).__name__}): the brief "
                                 "goes without candidates")  # fmt: skip
        await clock.sleep_until(plan.brief_at)
        if clock.now() >= latest:
            await self._brief_missed(f"the pre-market scan ran past {latest:%H:%M:%S}")
            return
        pack = await self.packs.brief(book_b=await self._book_b(with_mode=False))
        result = await self._calls.run(
            "premarket", pack, deadline=plan.brief_deadline, persist=self.store.persist_brief
        )
        await self.store.brief_done(result)
        mode = self.store.day_mode()
        log.info("brief done", ok=result.ok, failure=result.failure, day_mode=mode.mode,
                 source=mode.source, candidates=len(pack.kept))  # fmt: skip
        await self._after("premarket", result)

    async def _brief_missed(self, why: str) -> None:
        await self.store.brief_missed()
        log.warning("no brief today", why=why)
        await self._incident("P3", "brief_missed", f"no brief today: {why}; day mode normal, "
                             "the vet judges every signal")  # fmt: skip

    # ------------------------------------------------------------ the vet

    def start_vet(self, signals: Sequence[Signal]) -> VetJob | None:
        """At arming, before any trigger reaches Book B: the vet's signals are pending from now,
        and its call runs in the background. None when nothing needs it, or when the job
        couldn't start: its signals then have no call covering them (``llm_unavailable``)."""
        if self.disabled:
            return None
        try:
            targets = self.store.vet_targets(signals)
            if not targets:
                return None
            job = self.store.expect_vet(targets, at=self._clock.now())
            task = asyncio.create_task(self._vet(job, targets), name=f"vet-{job.number}")
        except Exception as exc:
            log.exception("the vet's job did not start")
            message = f"the vet's job did not start ({type(exc).__name__})"
            self._vets.append(asyncio.create_task(self._incident("P2", "llm_job_failed", message)))
            return None
        self._vets.append(task)
        log.info("vet expected", job=job.number, symbols=list(job.symbols))
        return job

    async def _vet(self, job: VetJob, targets: Sequence[Signal]) -> None:
        store, clock = self.store, self._clock
        try:
            pack = await self.packs.vet(
                targets,
                book_b=await self._book_b(with_mode=True),
                edgar_until=clock.now() + timedelta(seconds=VET_EDGAR_S),
            )
            store.vet_started(job, at=clock.now())
            result = await self._calls.run(
                "vet", pack, deadline=self._plan.times.last_entry, persist=store.persist_vet
            )
            await store.vet_done(job, result, sent=pack.kept)
            log.info("vet done", job=job.number, ok=result.ok, failure=result.failure,
                     candidates=len(pack.kept))  # fmt: skip
            await self._after("vet", result)
        except asyncio.CancelledError:
            store.vet_abandoned(job, at=clock.now())
            raise
        except Exception as exc:
            log.exception("the vet's job failed", job=job.number)
            await self._incident("P2", "llm_job_failed",
                                 f"the vet's job failed ({type(exc).__name__}): its signals are "
                                 "llm_unavailable")  # fmt: skip
            failed: LLMResult[Any] = LLMResult(
                False, None, None, 0, f"internal_error: {type(exc).__name__}",
                failure="internal_error",
            )  # fmt: skip
            await store.vet_done(job, failed, sent=job.symbols)

    # ------------------------------------------------------------ the review

    async def review(self) -> None:
        """At 16:15, once a day, with ``ReviewWork`` (P4.10). Never raises."""
        try:
            await self._review()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("the review's job failed")
            await self._incident("P2", "llm_job_failed",
                                 f"the review's job failed ({type(exc).__name__})")  # fmt: skip

    async def _review(self) -> None:
        work = self._deps.review
        if work is None:
            log.info("no review today: memory/reviews.py arrives with P4.10")
            return
        if self.disabled:
            return
        day, clock, plan = self._plan.day, self._clock, self._plan
        if await step_done(self._journal, day, REVIEW_STEP):
            return
        await clock.sleep_until(plan.review_at)
        latest = plan.review_cutoff - timedelta(
            seconds=self._settings.llm.roles.review.min_attempt_s
        )
        if clock.now() >= latest:
            await work.not_reviewed(why=f"no time before {plan.review_cutoff:%H:%M}", result=None)
            await mark_step(self._journal, day, REVIEW_STEP, clock.now(), "no time")
            return
        pack = await self.packs.review(book_b=await self._book_b(with_mode=True))
        if pack is None:
            await mark_step(self._journal, day, REVIEW_STEP, clock.now(), "nothing to review")
            return

        def persist(conn: sqlite3.Connection, output: BaseModel, attempt_id: str) -> None:
            """The answer and the day's review marker commit together: a crash in between can't
            make a rerun ask again (plan decision 11)."""
            work.persist(conn, output, attempt_id)
            conn.execute(
                "INSERT OR IGNORE INTO day_runs (date, kind, name, at, detail) "
                "VALUES (?, 'step', ?, ?, 'ok')",
                (day.isoformat(), REVIEW_STEP, fmt_iso(clock.now())),
            )

        result = await self._calls.run("review", pack, deadline=plan.review_cutoff,
                                       persist=persist)  # fmt: skip
        if not result.ok:
            await work.not_reviewed(why=result.failure or "failed", result=result)
            await mark_step(self._journal, day, REVIEW_STEP, clock.now(),
                            result.failure or "failed")  # fmt: skip
        await self._after("review", result, degrade=False)

    # ------------------------------------------------------------ the end

    async def vets_finished(self) -> None:
        """Wait for the vet calls under way to end (a test's, or a replay's, end of the vet)."""
        await asyncio.gather(*self._vets, return_exceptions=True)

    async def close(self) -> None:
        """Cancel a vet still running (its symbols are vetted again after a restart) and a news
        start still trying, and stop the news feed."""
        tasks = [*self._vets, *([self._news_task] if self._news_task is not None else [])]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Book B's task failed")
        self._vets, self._news_task = [], None
        if self._news_started and self._deps.news is not None:
            self._news_started = False
            try:
                await self._deps.news.stop()
            except Exception:
                log.exception("news did not stop cleanly")

    # ------------------------------------------------------------ after a call

    async def _after(self, role: str, result: LLMResult[Any], *, degrade: bool = True) -> None:
        """The incidents a call's attempts call for, then degradation (not after the review:
        the day's entries are over)."""
        if result.attempts:
            marks = ", ".join("?" for _ in result.attempts)
            rows = await self._journal.fetch_all(
                f"SELECT id, ok, failure, error FROM llm_calls WHERE id IN ({marks}) ORDER BY id",
                result.attempts,
            )
            for r in rows:
                if r["ok"] or r["failure"] == ABANDONED:
                    continue
                failure = r["failure"] or "failed"
                persistent = failure in PERSISTENT_REFUSALS
                text = (r["error"] or failure)[:MESSAGE_CHARS]
                await self._incident(
                    "P2" if persistent else "P3",
                    "llm_refused" if persistent else "llm_call_failed",
                    f"{role} attempt {r['id']}: {text}"
                    + ("; every call refuses until a human fixes it" if persistent else ""),
                    {"attempt": r["id"], "failure": failure},
                )
        elif result.failure:
            await self._incident("P3", "llm_call_refused",
                                 f"{role} call refused: {result.failure}",
                                 {"failure": result.failure})  # fmt: skip
        if degrade:
            await self._check_degraded()

    async def _check_degraded(self) -> None:
        store = self.store
        await store.refresh_failures()
        if not store.degraded:
            return
        if any(h.reason == "llm_degraded" for h in await self._halts.active("B")):
            return
        why = (f"{store.failures} failed LLM attempts today (llm_failures_degrade_after "
               f"{store.degrade_after})")  # fmt: skip
        await self._halts.halt("B", "llm_degraded", detail=why, expires_at=self._plan.next_open)
        await self._incident("P2", "llm_degraded", f"Book B: {why}: no new Book B entries today")

    async def _book_b(self, *, with_mode: bool) -> BookB:
        mode = self.store.day_mode() if with_mode else None
        return BookB(
            equity=self._equity(),
            recent_r=await recent_r(self._journal, "B", self._plan.day),
            day_mode=mode.mode if mode is not None else None,
            day_mode_source=mode.source if mode is not None else None,
        )

    async def _incident(
        self,
        severity: Severity,
        code: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        await record_incident(
            self._journal,
            at=self._clock.now(),
            severity=severity,
            component="llm",
            code=code,
            message=message,
            detail=detail,
        )
