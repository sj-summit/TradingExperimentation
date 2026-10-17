"""Pre-market candidates for the 09:05 brief (STRATEGY.md §5.2; the Phase 4 plan's decision 12;
DECISIONS D-063). Pure: the caller fetches the prints and the news.

- **Who qualifies:** an eligible symbol (S§3.1) whose last print came today at or after
  04:00 ET and may set a price (D-043's conditions), above S§3.2's price floor, with a gap
  that doesn't sit at a split ratio. The 09:35 scan drops the last two, so neither could
  become a signal; a split Alpaca hasn't listed would otherwise top the gainers.
- **The gap** is the last print against the universe's SIP previous close (``universe_daily``).
  This supersedes D-024's snapshot daily bar as the source.
- **The picks:** the top 30 gainers by gap, then up to 10 more symbols with news in the window,
  by the size of their gap, up or down; at most ``max_candidates`` (40) in all, gainers first.
  Ties go to the symbol, so the order never depends on the input's.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal

from trader.core.clock import et_datetime
from trader.core.models import Trade
from trader.data import indicators
from trader.data.conditions import counts_for_price
from trader.strategy.orb_sip import suspect_gap

PREMARKET_FROM = time(4, 0)  # the earliest print that counts as today's (decision 12)
GAINERS = 30
WITH_NEWS = 10

PickedAs = Literal["gainer", "news"]


@dataclass(frozen=True)
class Listing:
    """An eligible symbol from ``universe_daily`` (S§3.1). ``name`` is third-party text."""

    symbol: str
    name: str
    exchange: str
    prev_close: Decimal
    atr14: Decimal
    adv14: int


@dataclass(frozen=True)
class PremarketCandidate:
    listing: Listing
    price: Decimal  # the last print
    printed_at: datetime
    gap_pct: float  # the print against the previous close, in percent
    picked_as: PickedAs

    @property
    def symbol(self) -> str:
        return self.listing.symbol


def premarket_gap(prev_close: Decimal, print_: Trade | None, *, day: date) -> float | None:
    """The gap of ``day``'s last print, or None without a print since 04:00 ET that may set a
    price, or without a usable previous close. Index ETFs' gaps for the pack's ``market`` come
    from here too."""
    if print_ is None or prev_close <= 0 or print_.price <= 0 or not counts_for_price(print_):
        return None
    if print_.ts < et_datetime(day, PREMARKET_FROM):
        return None
    return indicators.gap_pct(print_.price, prev_close)


def select_premarket(
    listings: Iterable[Listing],
    prints: Mapping[str, Trade],
    news_symbols: Collection[str],
    *,
    day: date,
    min_price: Decimal,
    max_candidates: int,
    gainers: int = GAINERS,
    with_news: int = WITH_NEWS,
) -> list[PremarketCandidate]:
    """The brief's candidates, in rank order. ``prints`` holds each symbol's last trade (a
    snapshot's), ``news_symbols`` the symbols with news in the brief's window."""
    qualified: list[PremarketCandidate] = []
    for listing in listings:
        print_ = prints.get(listing.symbol)
        gap = premarket_gap(listing.prev_close, print_, day=day)
        if print_ is None or gap is None or not print_.price > min_price:
            continue
        if suspect_gap(print_.price, listing.prev_close):
            continue
        qualified.append(
            PremarketCandidate(
                listing=listing,
                price=print_.price,
                printed_at=print_.ts,
                gap_pct=gap,
                picked_as="gainer",
            )
        )
    rising = sorted((c for c in qualified if c.gap_pct > 0), key=lambda c: (-c.gap_pct, c.symbol))
    picked = rising[: max(gainers, 0)]
    taken = {c.symbol for c in picked}
    newsy = sorted(
        (c for c in qualified if c.symbol not in taken and c.symbol in news_symbols),
        key=lambda c: (-abs(c.gap_pct), c.symbol),
    )
    picked += [
        PremarketCandidate(
            listing=c.listing,
            price=c.price,
            printed_at=c.printed_at,
            gap_pct=c.gap_pct,
            picked_as="news",
        )
        for c in newsy[: max(with_news, 0)]
    ]
    return picked[: max(max_candidates, 0)]
