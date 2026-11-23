"""Where Book B's packs come from (A§8; S§5.2-5.3, S§6.1; the Phase 4 plan's decisions 10 and 12;
D-063, D-069).

- **``LivePacks``** builds them from the day:
  - the 09:00 pre-market scan: the day's eligible universe, one snapshot each (the live feed's
    latest trade), and the symbols with news in the brief's window, through
    ``candidates.select_premarket``; then EDGAR prefetched for the picks, within its budget;
  - the brief's pack at 09:05, the vet's at arming, the review's from ``ReviewWork`` (P4.10);
  - the common sections: the session, today's events, the index ETFs (one snapshot of the
    regime symbols, against the close of the bar dated the previous session: the pre-market
    gap before the open, then the opening gap and the change since that close; ``index_market``),
    the previous session's regime tags, Book B's equity and recent R, the experiment's numbers,
    and whether the news could be read (``news_unavailable``: an empty list then means unknown,
    not none).

  Building runs off the event loop beside a threaded journal: the sanitizer can take seconds on
  an adversarial pack (D-063). Beside an inline journal (simdrive) it runs inline, so a SimClock
  never moves in the middle of one, as with the runner's file work (D-064).
- **``RecordingPacks``** records every pack a job built, sent or not, and every build that
  failed, into ``llm.jsonl.gz``.
- **``ReplayPacks``** serves the recorded packs back, each at the time it was ready live: a replay
  builds none, and needs no news or EDGAR (decision 10, D-063).
"""

import asyncio
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

import structlog

from trader.config import BookName, RiskConfig, Settings
from trader.core.calendar import DayTimes, Session
from trader.core.clock import Clock, ensure_et, fmt_iso, parse_et_iso
from trader.core.events import Event
from trader.core.models import Bar, Signal, Snapshot, Trade
from trader.data import indicators
from trader.data.conditions import counts_for_price
from trader.data.replay import ReplayMissError
from trader.llm.candidates import Listing, PremarketCandidate, premarket_gap, select_premarket
from trader.llm.context import (
    RECENT_TRADES,
    BookB,
    Common,
    EncodedPack,
    Market,
    Pack,
    PlaybookEntry,
    Role,
    Trimmed,
    VetCandidate,
    decode,
    experiment_section,
    premarket_pack,
    role_limits,
    vet_pack,
)
from trader.memory.journal import Journal
from trader.memory.playbook import matching, playbook, vet_features
from trader.memory.regime import RegimeTags
from trader.orchestrator.evidence import EvidenceDesk, evidence_for
from trader.orchestrator.ports import DayMarket, PackSource, ReviewWork

log = structlog.get_logger(__name__)

T = TypeVar("T")
Sink = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class PackContext:
    """What a pack source needs from the day, known once boot has run."""

    journal: Journal
    clock: Clock
    market: DayMarket
    settings: Settings
    risk: RiskConfig
    session: Session
    previous_session: date  # the index ETFs' gaps are against its close
    times: DayTimes
    events: tuple[Event, ...]
    regime: RegimeTags | None
    encode: Callable[[Pack], EncodedPack]  # LlmCalls.encode: trims to llm.max_pack_bytes


PackFactory = Callable[[PackContext], PackSource]


async def recent_r(journal: Journal, book: BookName, day: date) -> list[float]:
    """R of the book's latest trades before ``day``, newest first (A§8's ``book_b.recent``)."""
    rows = await journal.fetch_all(
        "SELECT r_multiple FROM trades WHERE book = ? AND r_multiple IS NOT NULL "
        "AND substr(entry_ts, 1, 10) < ? ORDER BY entry_ts DESC, id DESC LIMIT ?",
        (book, day.isoformat(), RECENT_TRADES),
    )
    return [float(r["r_multiple"]) for r in rows]


def index_market(
    snapshots: Mapping[str, Snapshot],
    symbols: Sequence[str],
    *,
    previous_session: date,
    session: Session,
    as_of: datetime,
) -> tuple[dict[str, float | None], dict[str, float | None] | None]:
    """The index ETFs' gaps, and from the open their changes, in percent against the close of the
    daily bar dated ``previous_session`` (``Market``; D-073).

    Before the open Alpaca's ``dailyBar`` is the previous session's and ``prevDailyBar`` the one
    before; after it ``dailyBar`` is today's and ``prevDailyBar`` the previous session's
    (D-024). So a bar is picked by its date, not by its name.
    - **Before the open** (the brief), the gap is the last print since 04:00 against that close
      (``premarket_gap``), and there are no changes.
    - **From the open** (the vet, the review), the gap is today's opening gap: today's daily
      bar's open against that close. The change runs to the last regular-hours print, else to
      today's bar's close: a 16:15 snapshot's latest trade can be an after-hours print.

    Unknown (None) when the bar needed is missing."""
    opened = ensure_et(as_of) >= session.open
    gaps: dict[str, float | None] = {}
    changes: dict[str, float | None] = {}
    for symbol in symbols:
        snap = snapshots.get(symbol)
        bars = () if snap is None else (snap.daily_bar, snap.prev_daily_bar)
        prev = _dated(bars, previous_session)
        last = snap.latest_trade if snap is not None else None
        if not opened:
            gaps[symbol] = (
                None if prev is None else premarket_gap(prev.close, last, day=session.date)
            )
            continue
        today = _dated(bars, session.date)
        if prev is None or prev.close <= 0 or today is None:
            gaps[symbol] = changes[symbol] = None
            continue
        price = last.price if last is not None and _regular(last, session) else today.close
        gaps[symbol] = indicators.gap_pct(today.open, prev.close)
        changes[symbol] = indicators.gap_pct(price, prev.close)
    return gaps, (changes if opened else None)


def _dated(bars: Iterable[Bar | None], day: date) -> Bar | None:
    return next((b for b in bars if b is not None and ensure_et(b.ts).date() == day), None)


def _regular(print_: Trade, session: Session) -> bool:
    """A print from the session's regular hours that may set a price."""
    at = ensure_et(print_.ts)
    return session.open <= at < session.close and print_.price > 0 and counts_for_price(print_)


class LivePacks:
    """Packs built from the day (the module's docstring)."""

    def __init__(
        self,
        ctx: PackContext,
        *,
        evidence: EvidenceDesk,
        review: ReviewWork | None = None,
    ) -> None:
        self._ctx, self._evidence, self._review = ctx, evidence, review
        self._picks: list[PremarketCandidate] = []
        self._threaded = not ctx.journal.inline

    # ------------------------------------------------------------ the jobs' calls

    async def premarket_scan(self, *, edgar_until: datetime) -> None:
        """S§5.2's candidates (decision 12, D-063), then EDGAR for them within the budget."""
        ctx, now = self._ctx, self._ctx.clock.now()
        listings = await self._listings()
        symbols = [x.symbol for x in listings]
        snapshots = (
            await ctx.market.snapshots(symbols, feed=ctx.settings.data.feed) if symbols else {}
        )
        prints = {s: snap.latest_trade for s, snap in snapshots.items() if snap.latest_trade}
        news = await self._evidence.news(symbols, since=now - self._lookback("premarket"))
        self._picks = select_premarket(
            listings,
            prints,
            set(news),
            day=ctx.session.date,
            min_price=ctx.settings.strategy.selection.min_open_price,
            max_candidates=ctx.settings.llm.roles.premarket.max_candidates,
        )
        log.info("pre-market scan", eligible=len(listings), printed=len(prints),
                 with_news=len(news), candidates=len(self._picks))  # fmt: skip
        await self._evidence.filings([c.symbol for c in self._picks], until=edgar_until)

    async def brief(self, *, book_b: BookB) -> EncodedPack:
        """The brief's pack, as of now, on the 09:00 picks; their filings as prefetched."""
        ctx, now = self._ctx, self._ctx.clock.now()
        picks = list(self._picks)
        symbols = [c.symbol for c in picks]
        news = await self._evidence.news(symbols, since=now - self._lookback("premarket"))
        filings = await self._evidence.filings(symbols, until=now)  # the prefetch's answers only
        common = await self._common(now, book_b)
        limits = role_limits(ctx.settings, "premarket")
        evidence = evidence_for(symbols, news, filings)
        return await self._build(
            lambda: ctx.encode(
                premarket_pack(
                    as_of=now, common=common, candidates=picks, evidence=evidence, limits=limits
                )
            )
        )

    async def vet(
        self, signals: Sequence[Signal], *, book_b: BookB, edgar_until: datetime
    ) -> EncodedPack:
        """The vet's pack: ``signals`` by rank, with news and filings. EDGAR is asked within
        the budget, by rank: first the symbols the prefetch didn't answer, then the prefetched
        ones again, which keep their 09:00 answer if the budget runs out first."""
        ctx = self._ctx
        ranked = sorted(signals, key=lambda s: (s.rel_vol_rank, s.symbol))
        symbols = [s.symbol for s in ranked]
        filings = await self._evidence.filings(symbols, until=edgar_until, refresh=True)
        now = ctx.clock.now()  # after EDGAR: the pack's news runs up to its own time
        news = await self._evidence.news(symbols, since=now - self._lookback("vet"))
        names = await self._names(symbols)
        evidence = evidence_for(symbols, news, filings)
        lessons = await self._playbook()
        candidates = [
            VetCandidate(
                signal=s,
                name=names.get(s.symbol, ("", ""))[0],
                exchange=names.get(s.symbol, ("", ""))[1],
                evidence=evidence[s.symbol],
                lessons=tuple(matching(lessons, vet_features(s))),
            )
            for s in ranked
        ]
        common = await self._common(now, book_b, playbook=lessons)
        limits = role_limits(ctx.settings, "vet")
        return await self._build(
            lambda: ctx.encode(
                vet_pack(as_of=now, common=common, candidates=candidates, limits=limits)
            )
        )

    async def review(self, *, book_b: BookB) -> EncodedPack | None:
        if self._review is None:
            return None
        now = self._ctx.clock.now()
        pack = await self._review.pack(as_of=now, common=await self._common(now, book_b))
        if pack is None:
            return None
        encode = self._ctx.encode
        return await self._build(lambda: encode(pack))

    # ------------------------------------------------------------ helpers

    def _lookback(self, role: Role) -> timedelta:
        news = self._ctx.settings.news
        hours = news.premarket_lookback_hours if role == "premarket" else news.vet_lookback_hours
        return timedelta(hours=hours)

    async def _build(self, make: Callable[[], T]) -> T:
        if self._threaded:
            return await asyncio.to_thread(make)
        return make()

    async def _common(
        self, as_of: datetime, book_b: BookB, *, playbook: Sequence[PlaybookEntry] | None = None
    ) -> Common:
        """The common sections. Call it after the role's news read: ``news_available`` says
        whether that read, and the feed behind it, could be trusted."""
        ctx = self._ctx
        return Common(
            session=ctx.session,
            times=ctx.times,
            events=ctx.events,
            market=await self._market(as_of),
            book_b=book_b,
            experiment=experiment_section(ctx.settings, ctx.risk),
            news_available=self._evidence.news_ok,
            playbook=tuple(await self._playbook() if playbook is None else playbook),
        )

    async def _playbook(self) -> list[PlaybookEntry]:
        """The promoted lessons, ranked (``memory/playbook.py``; D-077)."""
        cfg = self._ctx.settings.lessons
        return await self._ctx.journal.read(lambda conn: playbook(conn, cfg))

    async def _market(self, as_of: datetime) -> Market:
        """The index ETFs from one snapshot of the regime symbols (``index_market``); unknown
        when the snapshot fails."""
        ctx = self._ctx
        symbols = list(ctx.settings.data.regime_symbols)
        try:
            snaps = await ctx.market.snapshots(symbols, feed=ctx.settings.data.feed)
        except Exception as exc:
            log.warning("index gaps unknown", error=f"{type(exc).__name__}: {exc}")
            snaps = {}
        gaps, changes = index_market(
            snaps, symbols, previous_session=ctx.previous_session, session=ctx.session, as_of=as_of
        )
        return Market(gaps=gaps, changes=changes, regime=ctx.regime)

    async def _listings(self) -> list[Listing]:
        rows = await self._ctx.journal.fetch_all(
            "SELECT symbol, name, exchange, prev_close, atr14, adv14 FROM universe_daily "
            "WHERE date = ? AND eligible = 1 ORDER BY symbol",
            (self._ctx.session.date.isoformat(),),
        )
        out = []
        for r in rows:
            if r["prev_close"] is None or r["atr14"] is None or r["adv14"] is None:
                continue
            out.append(
                Listing(
                    symbol=r["symbol"],
                    name=r["name"] or "",
                    exchange=r["exchange"] or "",
                    prev_close=Decimal(r["prev_close"]),
                    atr14=Decimal(r["atr14"]),
                    adv14=int(r["adv14"]),
                )
            )
        return out

    async def _names(self, symbols: Iterable[str]) -> dict[str, tuple[str, str]]:
        wanted = sorted(set(symbols))
        if not wanted:
            return {}
        marks = ", ".join("?" for _ in wanted)
        rows = await self._ctx.journal.fetch_all(
            f"SELECT symbol, name, exchange FROM universe_daily WHERE date = ? "
            f"AND symbol IN ({marks})",
            (self._ctx.session.date.isoformat(), *wanted),
        )
        return {r["symbol"]: (r["name"] or "", r["exchange"] or "") for r in rows}


def live_packs(evidence: EvidenceDesk, *, review: ReviewWork | None = None) -> PackFactory:
    def make(ctx: PackContext) -> PackSource:
        return LivePacks(ctx, evidence=evidence, review=review)

    return make


# ---------------------------------------------------------------- recording and replay


class RecordingPacks:
    """A pack source, with every pack it builds recorded: when its build began and ended, its
    id, its trims and its bytes; for the vet, the symbols it was asked about. A build that
    failed is recorded too, by its error, so a replay fails it the same way."""

    def __init__(self, inner: PackSource, clock: Clock, sink: Sink) -> None:
        self._inner, self._clock, self._sink = inner, clock, sink

    async def premarket_scan(self, *, edgar_until: datetime) -> None:
        await self._inner.premarket_scan(edgar_until=edgar_until)

    async def brief(self, *, book_b: BookB) -> EncodedPack:
        started = self._clock.now()
        try:
            pack = await self._inner.brief(book_b=book_b)
        except Exception as exc:
            self._failed("premarket", exc, started)
            raise
        self._record("premarket", pack, started)
        return pack

    async def vet(
        self, signals: Sequence[Signal], *, book_b: BookB, edgar_until: datetime
    ) -> EncodedPack:
        started = self._clock.now()
        asked = [s.symbol for s in signals]
        try:
            pack = await self._inner.vet(signals, book_b=book_b, edgar_until=edgar_until)
        except Exception as exc:
            self._failed("vet", exc, started, asked=asked)
            raise
        self._record("vet", pack, started, asked=asked)
        return pack

    async def review(self, *, book_b: BookB) -> EncodedPack | None:
        started = self._clock.now()
        try:
            pack = await self._inner.review(book_b=book_b)
        except Exception as exc:
            self._failed("review", exc, started)
            raise
        if pack is not None:
            self._record("review", pack, started)
        return pack

    def _record(
        self, role: Role, pack: EncodedPack, started: datetime, *, asked: list[str] | None = None
    ) -> None:
        record: dict[str, Any] = {
            "kind": "pack",
            "role": role,
            "started_rx": fmt_iso(started),
            "ready_rx": fmt_iso(self._clock.now()),
            "pack_id": pack.pack_id,
            "trimmed": asdict(pack.trimmed),
            "data": pack.data.decode("utf-8"),  # encode() writes UTF-8
        }
        if asked is not None:
            record["asked"] = asked
        self._sink(record)

    def _failed(
        self, role: Role, exc: Exception, started: datetime, *, asked: list[str] | None = None
    ) -> None:
        record: dict[str, Any] = {
            "kind": "pack",
            "role": role,
            "started_rx": fmt_iso(started),
            "ready_rx": fmt_iso(self._clock.now()),
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }
        if asked is not None:
            record["asked"] = asked
        self._sink(record)


def recording_packs(factory: PackFactory, sink: Sink) -> PackFactory:
    def make(ctx: PackContext) -> PackSource:
        return RecordingPacks(factory(ctx), ctx.clock, sink)

    return make


class RecordedBuildError(RuntimeError):
    """A pack whose build failed live fails the same way in the replay."""


class ReplayPacks:
    """The recorded packs, matched by role and order, each served at the time it was ready live.
    A build that failed live fails again, at the same time. A brief's or a vet's pack the
    recording lacks is a divergence; a review with none recorded had none live."""

    def __init__(self, records: Iterable[Mapping[str, Any]], clock: Clock) -> None:
        self._clock = clock
        self._packs: dict[str, deque[Mapping[str, Any]]] = defaultdict(deque)
        for record in records:
            if record.get("kind") == "pack":
                self._packs[str(record["role"])].append(record)
        self.divergences: list[str] = []

    async def premarket_scan(self, *, edgar_until: datetime) -> None:
        return None  # the recorded brief's pack holds what the scan found

    async def brief(self, *, book_b: BookB) -> EncodedPack:
        return await self._required("premarket", "brief")

    async def vet(
        self, signals: Sequence[Signal], *, book_b: BookB, edgar_until: datetime
    ) -> EncodedPack:
        asked = [s.symbol for s in signals]
        record = self._peek("vet")
        recorded = None if record is None else record.get("asked")
        if recorded is not None and list(recorded) != asked:
            self.divergences.append(
                f"llm: the vet was to judge {', '.join(asked) or 'nothing'}; the recorded one "
                f"judged {', '.join(recorded) or 'nothing'}"
            )
        return await self._required("vet", "vet")

    async def review(self, *, book_b: BookB) -> EncodedPack | None:
        return await self._serve("review")

    async def _required(self, role: str, what: str) -> EncodedPack:
        pack = await self._serve(role)
        if pack is None:
            self.divergences.append(f"llm: the recording has no {what}'s pack")
            raise ReplayMissError(f"the recording has no {what}'s pack")
        return pack

    def _peek(self, role: str) -> Mapping[str, Any] | None:
        queue = self._packs.get(role)
        return queue[0] if queue else None

    async def _serve(self, role: str) -> EncodedPack | None:
        queue = self._packs.get(role)
        if not queue:
            return None
        record = queue.popleft()
        ready = parse_et_iso(str(record["ready_rx"]))
        await self._clock.sleep_until(max(self._clock.now(), ready))
        if "error" in record:
            raise RecordedBuildError(str(record["error"]))
        trimmed = Trimmed(**record.get("trimmed") or {})
        return decode(str(record["data"]).encode("utf-8"), trimmed)

    @property
    def unused(self) -> int:
        return sum(len(q) for q in self._packs.values())
