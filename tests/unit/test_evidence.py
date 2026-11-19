"""News and EDGAR for Book B's packs, with EDGAR's time budget (P4.8; D-063, D-069)."""

from collections.abc import Iterable
from datetime import date, datetime, timedelta

import pytest

from trader.core.clock import ET, SimClock
from trader.core.models import Filing, NewsItem
from trader.data.edgar import EdgarBlockedError, EdgarError
from trader.orchestrator.evidence import EvidenceDesk, evidence_for
from trader.orchestrator.simdrive import drive

T0 = datetime(2026, 10, 13, 9, 0, tzinfo=ET)


def item(n: int, *symbols: str, at: datetime = T0) -> NewsItem:
    return NewsItem(id=n, headline=f"h{n}", summary="", author="", source="benzinga", url=None,
                    symbols=symbols, created_at=at, updated_at=at)  # fmt: skip


def filing(symbol: str) -> Filing:
    return Filing(accession=f"0001-{symbol}", cik="0000000001", symbol=symbol, form="424B5",
                  filing_date=date(2026, 10, 12), filed_at=None, items="", url="u")  # fmt: skip


class News:
    def __init__(self, items: list[NewsItem], fail: bool = False) -> None:
        self.items, self.fail, self.asked = items, fail, []  # type: ignore[var-annotated]

    async def recent(self, symbols: Iterable[str], since: datetime) -> list[NewsItem]:
        wanted = set(symbols)
        self.asked.append((sorted(wanted), since))
        if self.fail:
            raise RuntimeError("database is locked")
        return [i for i in self.items if i.created_at >= since and wanted & set(i.symbols)]


class Edgar:
    """Answers each symbol after ``slow`` seconds on the clock, or raises what ``errors`` says."""

    def __init__(self, clock: SimClock, *, slow: float = 0.0, errors: dict[str, Exception]) -> None:
        self.clock, self.slow, self.errors = clock, slow, errors
        self.asked: list[str] = []

    async def recent_filings(self, symbol: str, days: int) -> list[Filing]:
        self.asked.append(symbol)
        await self.clock.sleep_until(self.clock.now() + timedelta(seconds=self.slow))
        if symbol in self.errors:
            raise self.errors[symbol]
        return [filing(symbol)] if symbol.startswith("F") else []


def desk(clock: SimClock, edgar: Edgar | None, news: News | None = None) -> EvidenceDesk:
    return EvidenceDesk(news=news, filings=edgar, clock=clock, filings_days=5)


async def test_news_goes_to_every_symbol_it_names() -> None:
    clock = SimClock(T0)
    news = News([item(1, "AAA", "BBB"), item(2, "AAA", "AAA"), item(3, "CCC"),
                 item(4, "AAA", at=T0 - timedelta(hours=30))])  # fmt: skip
    got = await desk(clock, None, news).news(["AAA", "bbb"], since=T0 - timedelta(hours=18))
    assert {s: [i.id for i in items] for s, items in got.items()} == {"AAA": [1, 2], "BBB": [1]}
    assert news.asked == [(["AAA", "BBB"], T0 - timedelta(hours=18))]


async def test_news_that_cannot_be_read_is_none_not_an_error() -> None:
    clock = SimClock(T0)
    assert await desk(clock, None, News([], fail=True)).news(["AAA"], since=T0) == {}
    assert await desk(clock, None, None).news(["AAA"], since=T0) == {}


async def test_edgar_answers_are_kept_for_the_day() -> None:
    clock = SimClock(T0)
    edgar = Edgar(clock, errors={})
    d = desk(clock, edgar)
    first = await d.filings(["FOO", "BAR"], until=T0 + timedelta(minutes=5))
    assert first == {"FOO": (filing("FOO"),), "BAR": ()}  # () is "none", never "unknown"
    again = await d.filings(["BAR", "FOO", "NEW"], until=T0)  # no budget left: only cached
    assert again == {"BAR": (), "FOO": (filing("FOO"),), "NEW": None}
    assert edgar.asked == ["FOO", "BAR"]


async def test_the_budget_drops_a_slow_answer_and_asks_no_more() -> None:
    clock = SimClock(T0)
    edgar = Edgar(clock, slow=4.0, errors={})
    d = desk(clock, edgar)
    got = await drive(clock, d.filings(["F1", "F2", "F3"], until=T0 + timedelta(seconds=6)))
    assert got == {"F1": (filing("F1"),), "F2": None, "F3": None}
    assert edgar.asked == ["F1", "F2"]  # F2 was cut off at 6 s, F3 never asked
    assert clock.now() == T0 + timedelta(seconds=6)


async def test_a_403_stops_edgar_and_any_other_failure_loses_one_symbol() -> None:
    clock = SimClock(T0)
    blocked = EdgarBlockedError("EDGAR answered 403; paused for 10 minutes")
    edgar = Edgar(clock, errors={"A": EdgarError("HTTP 500"), "B": blocked})
    got = await desk(clock, edgar).filings(["A", "F", "B", "C"], until=T0 + timedelta(minutes=1))
    assert got == {"A": None, "F": (filing("F"),), "B": None, "C": None}
    assert edgar.asked == ["A", "F", "B"]


async def test_no_edgar_means_unknown_filings() -> None:
    clock = SimClock(T0)
    got = await desk(clock, None).filings(["AAA"], until=T0 + timedelta(minutes=5))
    assert got == {"AAA": None}


async def test_a_bug_in_one_request_loses_only_its_symbol() -> None:
    clock = SimClock(T0)
    edgar = Edgar(clock, errors={"X": KeyError("cik")})
    got = await desk(clock, edgar).filings(["X", "F"], until=T0 + timedelta(minutes=1))
    assert got == {"X": None, "F": (filing("F"),)}


async def test_a_refresh_asks_the_answered_again_after_the_new_and_keeps_an_answer() -> None:
    clock = SimClock(T0)
    edgar = Edgar(clock, errors={})
    d = desk(clock, edgar)
    await d.filings(["F1", "F2"], until=T0 + timedelta(minutes=5))
    edgar.errors = {"F1": EdgarError("HTTP 500")}
    got = await d.filings(["F1", "NEW", "F2"], until=T0 + timedelta(minutes=5), refresh=True)
    assert edgar.asked == ["F1", "F2", "NEW", "F1", "F2"]  # the new one first, then the rest
    assert got == {"F1": (filing("F1"),), "NEW": (), "F2": (filing("F2"),)}  # F1 kept its own
    without = await d.filings(["F1"], until=T0 + timedelta(minutes=5))
    assert edgar.asked[-1] == "F2"  # no refresh: no ask
    assert without == {"F1": (filing("F1"),)}


async def test_news_ok_follows_the_last_read_and_the_feed() -> None:
    clock = SimClock(T0)
    news = News([item(1, "AAA")])
    up = [True]
    d = EvidenceDesk(news=news, filings=None, clock=clock, filings_days=5, news_up=lambda: up[0])
    assert d.news_ok
    news.fail = True
    await d.news(["AAA"], since=T0 - timedelta(hours=1))
    assert not d.news_ok  # the read failed: the packs say news is unknown
    news.fail = False
    await d.news(["AAA"], since=T0 - timedelta(hours=1))
    assert d.news_ok
    up[0] = False
    assert not d.news_ok  # the feed stopped
    assert not desk(clock, None, None).news_ok  # no news at all


def test_evidence_keeps_unknown_apart_from_none() -> None:
    ev = evidence_for(["A", "B", "C"], {"A": [item(1, "A")]}, {"A": (), "B": None})
    assert ev["A"].filings == ()
    assert [n.id for n in ev["A"].news] == [1]
    assert ev["B"].filings is None
    assert ev["C"].filings is None


@pytest.mark.parametrize("cancel_at", [1.0])
async def test_a_cancelled_fetch_leaves_nothing_running(cancel_at: float) -> None:
    clock = SimClock(T0)
    edgar = Edgar(clock, slow=10.0, errors={})
    d = desk(clock, edgar)

    async def job() -> None:
        import asyncio

        task = asyncio.ensure_future(d.filings(["F1"], until=T0 + timedelta(minutes=1)))
        await clock.sleep_until(T0 + timedelta(seconds=cancel_at))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await drive(clock, job())
    assert clock.next_wakeup() is None  # neither the fetch nor its timer is left waiting
