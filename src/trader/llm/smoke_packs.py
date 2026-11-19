"""The packs `trader smoke llm` sends (P4.6): fictional issuers, built like a trading day's.

``smoke_data.json`` (package data) holds the issuers, their news and the review's texts. Every
symbol has six letters, which no US exchange lists, so no real company is named; the numbers
are made up. ``smoke_packs`` builds, deterministically:

- the brief's pack: 40 candidates (30 gainers, then 10 with news), up to 3 news items each, some
  filings, some unknown (EDGAR failed);
- the vet's at full size: 25 candidates with 5 news items each;
- the review's: 25 signals, 8 of them review targets (traded, vetoed by Book B, or notable),
  with news either side of each decision, incidents, a lesson and ``history``;
- a small vet pack (3 candidates) for the canary.

One issuer's news carries an injection headline, in each role's pack, so the run shows what the
sanitizer and the model made of it. The setup's numbers (``experiment``, ``limits``) are the
live settings', so the prompts read what a trading day's would.
"""

import functools
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from importlib import resources
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trader.config import BookName, RiskConfig, Settings
from trader.core.calendar import Session, day_times
from trader.core.clock import et_datetime
from trader.core.events import Event
from trader.core.models import Filing, NewsItem, Signal
from trader.llm.candidates import GAINERS, Listing, PickedAs, PremarketCandidate
from trader.llm.context import (
    VET_MAX_CANDIDATES,
    BadProcess,
    BookB,
    BookResult,
    Common,
    Evidence,
    FactorCount,
    History,
    IncidentView,
    LessonView,
    Market,
    OpenIssue,
    OpenNote,
    Pack,
    ReviewNews,
    ReviewSignal,
    ReviewTarget,
    Role,
    VetCandidate,
    experiment_section,
    premarket_pack,
    review_pack,
    role_limits,
    vet_pack,
)
from trader.llm.schemas import SymbolAssessment, Verdict
from trader.memory.regime import RegimeTags
from trader.memory.reviews import running_stats
from trader.memory.shadow import ShadowOutcome
from trader.memory.trades import TradeRow

VET_NEWS = 5
REVIEW_TARGETS = 8
CANARY_CANDIDATES = 3
CENT = Decimal("0.01")
EQUITY = Decimal("996.08")
RECENT_R = (1.4, -1.0, -1.0, 0.3, 2.2)
FICTION = "https://example.invalid/trader-smoke"  # what the fixtures cite as their source
Selected = Literal["traded", "b_vetoed", "notable"]
SELECTED: tuple[Selected, ...] = (
    "traded", "traded", "b_vetoed", "b_vetoed", "notable", "notable", "notable", "notable",
)  # fmt: skip
SHADOW_R = (1.8, -1.0, -1.0, 2.4, 5.1, -1.0, 3.2, -0.6)


# ---------------------------------------------------------------- the data


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Story(_Data):
    headline: str  # ``{name}`` stands for the issuer's name
    summary: str


class Issuer(_Data):
    symbol: str = Field(pattern=r"^[A-Z]{6}$")  # six letters: no US exchange lists one
    name: str
    exchange: str
    kind: str  # which stories its news tells
    prev_close: Decimal
    gap_pct: float


class Injection(Story):
    symbol: str


class SmokeEvent(_Data):
    time: time
    name: str
    impact: Literal["high", "medium"]


class BadProcessText(_Data):
    why: str
    b_note: str


class IssueText(_Data):
    component: str
    description: str


class LessonText(_Data):
    statement: str
    claimed_direction: str
    suggested_use: str


class IncidentText(_Data):
    time: time
    severity: str
    component: str
    code: str
    message: str


class ReviewTexts(_Data):
    summaries: tuple[str, ...]
    bad_process: BadProcessText
    open_issue: IssueText
    open_note: str
    lesson: LessonText
    incidents: tuple[IncidentText, ...]


class SmokeData(_Data):
    about: str
    session: date
    events: tuple[SmokeEvent, ...]
    market_gaps: dict[str, float]
    sources: tuple[str, ...]
    issuers: tuple[Issuer, ...]
    stories: dict[str, tuple[Story, ...]]
    injection: Injection
    review: ReviewTexts


@functools.cache
def load_data() -> SmokeData:
    text = resources.files("trader.llm").joinpath("smoke_data.json").read_text(encoding="utf-8")
    return SmokeData.model_validate_json(text)


# ---------------------------------------------------------------- the packs


@dataclass(frozen=True)
class SmokePacks:
    premarket: Pack
    vet: Pack
    review: Pack
    canary: Pack  # a small vet pack

    def role(self, role: Role) -> Pack:
        return {"premarket": self.premarket, "vet": self.vet, "review": self.review}[role]


def smoke_packs(settings: Settings, risk: RiskConfig, data: SmokeData | None = None) -> SmokePacks:
    d = data or load_data()
    day = d.session
    session = Session(date=day, open=_at(day, 9, 30), close=_at(day, 16))
    times = day_times(
        session,
        entry_window_start=risk.entry_window_start,
        last_entry=settings.schedule.last_entry,
        flatten=settings.schedule.flatten,
    )
    events = [
        Event(date=day, time=e.time, name=e.name, impact=e.impact, source=FICTION) for e in d.events
    ]
    regime = RegimeTags(
        date=day - timedelta(days=1),
        spy_close=Decimal("512.40"),
        spy_sma20=Decimal("508.15"),
        spy_trend="up",
        spy_atr_pct=1.12,
        tercile_low=0.88,
        tercile_high=1.41,
        vol_bucket="mid",
    )
    experiment = experiment_section(settings, risk)

    def common(book_b: BookB) -> Common:
        market = Market(gaps=d.market_gaps, regime=regime)
        return Common(session, times, events, market, book_b, experiment)

    morning = BookB(equity=EQUITY, recent_r=RECENT_R)
    later = BookB(equity=EQUITY, recent_r=RECENT_R, day_mode="normal", day_mode_source="premarket")
    vet_at, vet_limits = _at(day, 9, 35, 5), role_limits(settings, "vet")
    return SmokePacks(
        premarket=_premarket(d, settings, common(morning)),
        vet=vet_pack(
            as_of=vet_at,
            common=common(later),
            candidates=_vet_candidates(d, VET_MAX_CANDIDATES, VET_NEWS),
            limits=vet_limits,
        ),
        review=_review(d, settings, common(later)),
        canary=vet_pack(
            as_of=vet_at,
            common=common(later),
            candidates=_vet_candidates(d, CANARY_CANDIDATES, 1),
            limits=vet_limits,
        ),
    )


def _premarket(d: SmokeData, settings: Settings, common: Common) -> Pack:
    day = d.session
    as_of = _at(day, 9, 5)
    candidates: list[PremarketCandidate] = []
    evidence: dict[str, Evidence] = {}
    for i, issuer in enumerate(d.issuers):
        picked: PickedAs = "gainer" if i < GAINERS else "news"
        listing = Listing(
            symbol=issuer.symbol,
            name=issuer.name,
            exchange=issuer.exchange,
            prev_close=issuer.prev_close,
            atr14=_atr(issuer, i),
            adv14=_adv(i),
        )
        candidates.append(
            PremarketCandidate(
                listing=listing,
                price=_open(issuer),
                printed_at=_at(day, 8, 59 - i % 30),
                gap_pct=issuer.gap_pct,
                picked_as=picked,
            )
        )
        count = i % 4 if picked == "gainer" else 1 + i % 3
        items = [
            _news(1000 + 10 * i + k, issuer, d, i + k, as_of - _minutes(20 + 61 * k + 7 * (i % 9)))
            for k in range(count)
        ]
        if issuer.symbol == d.injection.symbol:
            items.append(_injection(d, 1000 + 10 * i + 9, as_of - _minutes(15)))
        evidence[issuer.symbol] = Evidence(news=items, filings=_filings(issuer, i, day))
    return premarket_pack(
        as_of=as_of,
        common=common,
        candidates=candidates,
        evidence=evidence,
        limits=role_limits(settings, "premarket"),
    )


def _vet_candidates(d: SmokeData, count: int, news_each: int) -> list[VetCandidate]:
    day = d.session
    as_of = _at(day, 9, 35, 5)
    picks = []
    for i, issuer in enumerate(d.issuers[:count]):
        items = [
            _news(2000 + 10 * i + k, issuer, d, i + k, as_of - _minutes(30 + 140 * k + 11 * i))
            for k in range(news_each)
        ]
        if issuer.symbol == d.injection.symbol:
            items.append(_injection(d, 2000 + 10 * i + 9, as_of - _minutes(12)))
        picks.append(
            VetCandidate(
                signal=_signal(issuer, i, day, armed_at=as_of),
                name=issuer.name,
                exchange=issuer.exchange,
                evidence=Evidence(news=items, filings=_filings(issuer, i, day)),
            )
        )
    return picks


def _review(d: SmokeData, settings: Settings, common: Common) -> Pack:
    day, texts = d.session, d.review
    signals: list[ReviewSignal] = []
    targets: list[ReviewTarget] = []
    for i, issuer in enumerate(d.issuers[:VET_MAX_CANDIDATES]):
        signal = _signal(issuer, i, day, armed_at=_at(day, 9, 35, 5))
        if i >= REVIEW_TARGETS:  # armed, never triggered
            no_trigger = ShadowOutcome(direction="long", exit_reason="no_trigger")
            signals.append(ReviewSignal(signal=signal, shadow=no_trigger))
            continue
        row, target = _target(d, issuer, i, signal)
        signals.append(row)
        targets.append(target)
    earlier = d.issuers[5]
    # The books and the stats come from the pack's own trades and shadow outcomes (D-067)
    trades = {book: [t for s in signals for b, t in s.trades.items() if b == book]
              for book in ("A", "B")}  # fmt: skip
    books = {
        book: BookResult(
            Decimal(1000),
            Decimal(1000) + sum((t.pnl or Decimal(0) for t in rows), Decimal(0)),
            sum((t.pnl or Decimal(0) for t in rows), Decimal(0)),
            len(rows),
        )
        for book, rows in trades.items()
    }
    shadows = [s.shadow.shadow_r for s in signals if s.shadow and s.shadow.shadow_r is not None]
    vetoed = [s.shadow.shadow_r for s in signals if s.decisions.get("B") == "vetoed"
              and s.shadow and s.shadow.shadow_r is not None]  # fmt: skip
    stats = running_stats(
        epoch=0,
        days=1,
        a_r=[t.r_multiple for t in trades["A"] if t.r_multiple is not None],
        b_r=[t.r_multiple for t in trades["B"] if t.r_multiple is not None],
        shadow_r=shadows,
        vetoed_r=vetoed,
        b_vetoes=sum(s.decisions.get("B") == "vetoed" for s in signals),
    )
    return review_pack(
        as_of=_at(day, 16, 15),
        common=common,
        books=books,
        signals=signals,
        targets=targets,
        incidents=[
            IncidentView(et_datetime(day, x.time), x.severity, x.component, x.code, x.message)
            for x in texts.incidents
        ],
        stats=stats,
        lessons=[
            LessonView(
                id="L-001",
                status="proposed",
                statement=texts.lesson.statement,
                evidence_query={
                    "filters": [
                        {"field": "gap_pct", "op": "gte", "value": 15},
                        {"field": "catalyst_type", "op": "in", "value": ["offering_dilution"]},
                    ]
                },
                claimed_direction=texts.lesson.claimed_direction,
                suggested_use=texts.lesson.suggested_use,
                n_subset=21,
                n_complement=64,
                effect_r=-0.31,
                ci=(-0.58, -0.04),
            )
        ],
        history=History(
            summaries=[(day - timedelta(days=k + 1), s) for k, s in enumerate(texts.summaries)],
            bad_process=[
                BadProcess(
                    day - timedelta(days=2),
                    f"{day - timedelta(days=2):%Y%m%d}-{earlier.symbol}-orb5",
                    earlier.symbol,
                    texts.bad_process.why,
                    texts.bad_process.b_note,
                )
            ],
            factor_tally=[
                FactorCount("false_breakout", 9, 2, 7, -0.48, 1),
                FactorCount("catalyst_strong", 6, 4, 2, 0.85, 5),
                FactorCount("dilution_or_offering", 4, 0, 4, -0.95, 4),
            ],
            open_issues=[
                OpenIssue(
                    1,
                    "warn",
                    texts.open_issue.component,
                    texts.open_issue.description,
                    day - timedelta(days=6),
                    day - timedelta(days=1),
                    3,
                )
            ],
            open_notes=[
                OpenNote(2, texts.open_note, day - timedelta(days=5), day - timedelta(days=2), 2)
            ],
        ),
        limits=role_limits(settings, "review"),
    )


def _target(
    d: SmokeData, issuer: Issuer, i: int, armed: Signal
) -> tuple[ReviewSignal, ReviewTarget]:
    """The ``i``-th review target, as a signal of the day and as a target with its news."""
    day = d.session
    selected, r = SELECTED[i], SHADOW_R[i]
    triggered = _at(day, 9, 36 + 2 * i, 15)
    signal = armed.model_copy(update={"status": "triggered", "triggered_at": triggered})
    shadow = _shadow(signal, r, triggered)
    verdict: Verdict
    source: Literal["premarket", "vet"]
    basis: Literal["premarket_pack", "vet_pack", "trigger", "armed"]
    trades: dict[str, TradeRow] = {}
    # The two traded targets trigger first (09:36, 09:38): each book's two entries for the day
    # (R-007) are then used, so every later target is capacity_entries, or vetoed by Book B.
    if selected == "traded":
        decisions = {"A": "taken", "B": "taken"}
        trades = {"A": _trade("A", signal, r), "B": _trade("B", signal, r)}
        verdict, source, basis, decided = "allow", "vet", "vet_pack", _at(day, 9, 35, 5)
    elif selected == "b_vetoed":
        decisions = {"A": "capacity_entries", "B": "vetoed"}
        verdict, source, basis, decided = "veto", "premarket", "premarket_pack", _at(day, 9, 5)
    else:
        decisions = {"A": "capacity_entries", "B": "capacity_entries"}
        verdict, source, basis, decided = "neutral", "vet", "vet_pack", _at(day, 9, 35, 5)
    news = [
        ReviewNews(
            _news(3000 + 10 * i + k, issuer, d, i + k, decided + _minutes(m)),
            received_at=decided + _minutes(m) + timedelta(seconds=20),
        )
        for k, m in enumerate((-95, -40, 25))
    ]
    if issuer.symbol == d.injection.symbol:
        news.append(ReviewNews(_injection(d, 3000 + 10 * i + 9, decided - _minutes(12))))
    row = ReviewSignal(
        signal=signal,
        shadow=shadow,
        decisions=decisions,
        trades=trades,
        verdict=_assessment(issuer.symbol, verdict),
        verdict_source=source,
    )
    target = ReviewTarget(
        signal_id=signal.id,
        symbol=issuer.symbol,
        selected_for=selected,
        decision_at=decided,
        decision_basis=basis,
        shadow=shadow,
        spy_move_pct=round(0.42 - 0.07 * i, 2),
        news=news,
    )
    return row, target


# ---------------------------------------------------------------- the inputs' parts


def _at(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return et_datetime(day, time(hour, minute, second))


def _minutes(n: float) -> timedelta:
    return timedelta(minutes=n)


def _open(issuer: Issuer) -> Decimal:
    """The pre-market print, and the open: the previous close moved by the gap."""
    return (issuer.prev_close * (1 + Decimal(str(issuer.gap_pct)) / 100)).quantize(CENT)


def _atr(issuer: Issuer, i: int) -> Decimal:
    return (issuer.prev_close * (Decimal("0.05") + Decimal(i % 7) / 200)).quantize(CENT)


def _adv(i: int) -> int:
    return 650_000 + 137_000 * (i % 11) + 41_000 * i


def _news(id_: int, issuer: Issuer, d: SmokeData, n: int, created: datetime) -> NewsItem:
    """Story ``n`` of the issuer's kind (they rotate), as a news item."""
    stories = d.stories[issuer.kind]
    story = stories[n % len(stories)]
    return NewsItem(
        id=id_,
        headline=story.headline.replace("{name}", issuer.name),
        summary=story.summary.replace("{name}", issuer.name),
        author="",
        source=d.sources[n % len(d.sources)],
        url=None,
        symbols=(issuer.symbol,),
        created_at=created,
        updated_at=created,
    )


def _injection(d: SmokeData, id_: int, created: datetime) -> NewsItem:
    return NewsItem(
        id=id_,
        headline=d.injection.headline,
        summary=d.injection.summary,
        author="",
        source="accesswire",
        url=None,
        symbols=(d.injection.symbol,),
        created_at=created,
        updated_at=created,
    )


def _filings(issuer: Issuer, i: int, day: date) -> list[Filing] | None:
    """A prospectus for every fifth candidate, an 8-K for the one two after it; unknown (EDGAR
    failed) for some, none for the rest."""
    if i % 7 == 3:
        return None
    if i % 5 not in (0, 2):
        return []
    offering = i % 5 == 0
    filed = day - timedelta(days=1) if offering else day
    return [
        Filing(
            accession=f"0009999999-26-{i:06d}",
            cik="0009999999",
            symbol=issuer.symbol,
            form="424B5" if offering else "8-K",
            filing_date=filed,
            filed_at=_at(filed, 17, 4) if offering else _at(filed, 7, 31),
            items="" if offering else "1.01,9.01",
            url=FICTION,
        )
    ]


def _signal(issuer: Issuer, i: int, day: date, *, armed_at: datetime) -> Signal:
    open_, atr = _open(issuer), _atr(issuer, i)
    high = (open_ * Decimal("1.012")).quantize(CENT)
    low = (open_ * Decimal("0.991")).quantize(CENT)
    close = (open_ * Decimal("1.006")).quantize(CENT)
    trigger = high + CENT
    return Signal(
        id=f"{day:%Y%m%d}-{issuer.symbol}-orb5",
        date=day,
        symbol=issuer.symbol,
        direction="long",
        status="armed",
        rel_vol=round(9.6 - 0.31 * i, 2),
        rel_vol_rank=i + 1,
        or_open=open_,
        or_high=high,
        or_low=low,
        or_close=close,
        or_volume=40_000 + 9_000 * (i % 13),
        atr14=atr,
        adv14=_adv(i),
        prev_close=issuer.prev_close,
        gap_pct=issuer.gap_pct,
        trigger_price=trigger,
        stop_price=(trigger - atr / 10).quantize(CENT),
        max_chase_price=(trigger * Decimal("1.003")).quantize(CENT),
        entry_limit=trigger + 2 * CENT,
        armed_at=armed_at,
        features={
            "or_range_atr": round(float((high - low) / atr), 4),
            "or_return_pct": round(float((close - open_) / open_ * 100), 3),
            "atr_pct": round(float(atr / issuer.prev_close * 100), 3),
        },
    )


def _exit(signal: Signal, r: float) -> tuple[Decimal, Decimal]:
    """A long's entry and exit for an outcome of ``r`` R."""
    assert signal.trigger_price is not None
    assert signal.stop_price is not None
    entry = signal.trigger_price + CENT
    return entry, (entry + (entry - signal.stop_price) * Decimal(str(r))).quantize(CENT)


def _exited(signal: Signal, triggered: datetime, r: float) -> datetime:
    """A loser is stopped 14 minutes in; a winner rides to the flatten."""
    return triggered + _minutes(14) if r < 0 else _at(signal.date, 15, 55)


def _shadow(signal: Signal, r: float, triggered: datetime) -> ShadowOutcome:
    entry, exit_price = _exit(signal, r)
    exited = _exited(signal, triggered, r)
    return ShadowOutcome(
        direction="long",
        exit_reason="stop" if r < 0 else "flatten",
        trigger_ts=triggered.replace(second=0),
        trigger_bar_open=signal.or_close,
        entry_price=entry,
        exit_price=exit_price,
        exit_ts=exited.replace(second=0),
        shadow_r=r,
        mfe_r=round(max(r, 0.0) + 0.6, 2),
        mae_r=round(r - 0.2, 2) if r < 0 else -0.3,
        minutes_held=int((exited - triggered) / _minutes(1)),
        trigger_resolution="bars",
    )


def _trade(book: BookName, signal: Signal, r: float) -> TradeRow:
    assert signal.triggered_at is not None
    entry, exit_price = _exit(signal, r)
    entry += CENT  # the fill, a cent past the shadow's entry
    qty = 6 if book == "A" else 3
    return TradeRow(
        id=f"{book}-{signal.id}",
        book=book,
        signal_id=signal.id,
        symbol=signal.symbol,
        qty=qty,
        entry_ts=signal.triggered_at + timedelta(seconds=2),
        entry_price=entry,
        stop_price=signal.stop_price,
        exit_ts=_exited(signal, signal.triggered_at, r),
        exit_price=exit_price,
        exit_reason="stop" if r < 0 else "flatten",
        pnl=((exit_price - entry) * qty - CENT).quantize(CENT),
        r_multiple=r,
        fees_est=CENT,
        entry_slippage=CENT,
        entry_delay_ms=640,
        eval_epoch=0,
    )


def _assessment(symbol: str, verdict: Verdict) -> SymbolAssessment:
    """Book B's verdict on a target, as the review pack shows it."""
    if verdict == "veto":
        return SymbolAssessment(
            symbol=symbol,
            catalyst_type="offering_dilution",
            catalyst_fresh=True,
            catalyst_confirmed=True,
            risk_flags=["dilution_offering"],
            verdict="veto",
            confidence=0.81,
            reason="A discounted offering priced overnight: supply ahead of the breakout.",
        )
    allow = verdict == "allow"
    return SymbolAssessment(
        symbol=symbol,
        catalyst_type="earnings" if allow else "no_identifiable_news",
        catalyst_fresh=allow,
        catalyst_confirmed=allow,
        risk_flags=[],
        verdict=verdict,
        confidence=0.72 if allow else 0.5,
        reason="A fresh earnings beat with raised guidance."
        if allow
        else "No catalyst stands out.",
    )
