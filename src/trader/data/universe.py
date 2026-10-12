"""Daily universe and opening-range baselines (STRATEGY.md §3.1, ARCHITECTURE.md §4.2).

The universe for session t is computed before the open from prior sessions only:
split-adjusted SIP daily bars give ATR14, ADV14 and the previous close, and the live feed's
09:30-09:35 bars give the opening-range volume baseline, cached in ``or_bars``.
"""

import datetime as dt
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal
from typing import Protocol

import structlog
from pydantic import BaseModel, ConfigDict

from trader.config import UniverseConfig
from trader.core.calendar import TradingCalendar
from trader.core.clock import REGULAR_OPEN, et_datetime
from trader.core.ids import is_ticker
from trader.core.models import Asset, Bar, Split
from trader.data.alpaca_rest import Adjustment, Feed, Timeframe
from trader.data.indicators import PERIOD, adv, atr, or_vol_avg
from trader.memory.journal import Journal, dec_to_db

log = structlog.get_logger(__name__)

OR_MINUTES = 5
# Corporate actions can be listed late and the endpoint's date filter is undocumented, so the
# split query looks a week past both ends of the window (DECISIONS D-026).
SPLIT_LOOKAROUND = dt.timedelta(days=7)

# The first failing reason is recorded, in this order (D-026).
REASONS = (
    "split",
    "stale_prev_close",
    "insufficient_history",
    "adv",
    "atr",
    "insufficient_or_history",
)

_INSERT_OR_BAR = (
    "INSERT OR REPLACE INTO or_bars (date, symbol, feed, open, high, low, close, volume) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_UNIVERSE = (
    "INSERT INTO universe_daily (date, symbol, name, exchange, prev_close, atr14, adv14, "
    "or_vol_avg14, eligible, exclusion_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class UniverseData(Protocol):
    """The slice of AlpacaRest the builder uses."""

    async def assets(self) -> list[Asset]: ...
    async def bars(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: dt.datetime,
        end: dt.datetime | None = None,
        *,
        feed: Feed,
        adjustment: Adjustment = "raw",
    ) -> dict[str, list[Bar]]: ...
    async def splits(self, start: dt.date, end: dt.date) -> list[Split]: ...


class UniverseRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: dt.date
    symbol: str
    name: str
    exchange: str
    prev_close: Decimal | None
    atr14: Decimal | None
    adv14: int | None
    or_vol_avg14: float | None
    eligible: bool
    exclusion_reason: str | None


class UniverseReport(BaseModel):
    date: dt.date
    total_assets: int
    candidates: int
    eligible: int
    excluded: dict[str, int]

    def lines(self) -> list[str]:
        reasons = sorted(self.excluded.items(), key=lambda kv: (-kv[1], kv[0]))
        return [
            f"universe for {self.date}: {self.total_assets} assets, {self.candidates} candidates,"
            f" {self.eligible} eligible",
            "excluded: " + (", ".join(f"{reason} {n}" for reason, n in reasons) or "none"),
        ]


def default_target(calendar: TradingCalendar, now: dt.datetime) -> dt.date:
    """Today if its session hasn't opened yet, otherwise the next session."""
    today = now.date()
    if calendar.is_trading_day(today) and now < calendar.session(today).open:
        return today
    return calendar.next_session(today)


class UniverseBuilder:
    def __init__(
        self,
        *,
        data: UniverseData,
        journal: Journal,
        calendar: TradingCalendar,
        universe: UniverseConfig,
        feed: Feed,
        include_inactive: bool = False,
    ) -> None:
        """``include_inactive`` keeps delisted and untradable assets: the backtest needs them to
        avoid survivorship bias (DECISIONS D-042); the live universe never does."""
        self._data, self._journal, self._calendar = data, journal, calendar
        self._universe, self._feed = universe, feed
        self._include_inactive = include_inactive

    async def build(
        self, t: dt.date, *, persist: bool = True
    ) -> tuple[UniverseReport, list[UniverseRow]]:
        """Eligibility for session ``t`` (S§3.1). Writes ``universe_daily`` unless told not to."""
        self._calendar.session(t)  # raises NotATradingDayError
        sessions = self._calendar.last_sessions(t, PERIOD + 1)  # t-15 .. t-1, oldest first
        assets = await self._data.assets()
        excluded: Counter[str] = Counter()
        candidates: dict[str, Asset] = {}
        for asset in assets:
            reason = self._asset_exclusion(asset)
            if reason:
                excluded[reason] += 1
            else:
                candidates[asset.symbol] = asset
        symbols = sorted(candidates)

        start = et_datetime(sessions[0], dt.time(0))
        daily = await self._data.bars(symbols, "1Day", start, feed="sip", adjustment="split")
        split_days: dict[str, set[dt.date]] = {}
        for split in await self._data.splits(sessions[0] - SPLIT_LOOKAROUND, t + SPLIT_LOOKAROUND):
            split_days.setdefault(split.symbol, set()).add(split.ex_date)

        window = set(sessions)
        rows: dict[str, UniverseRow] = {}
        for symbol in symbols:
            bars = sorted(
                (b for b in daily.get(symbol, []) if b.ts.date() in window), key=lambda b: b.ts
            )
            ex_dates = split_days.get(symbol, set())
            rows[symbol] = self._features(t, sessions[-1], candidates[symbol], bars, ex_dates)

        passing = [s for s, row in rows.items() if row.exclusion_reason is None]
        split_in_window = {
            s for s in passing if any(sessions[0] <= d <= t for d in split_days.get(s, ()))
        }
        baselines = await self.or_baseline(t, passing, refresh=split_in_window)
        for symbol in passing:
            volumes = baselines.get(symbol)
            update: dict[str, object] = (
                {"exclusion_reason": "insufficient_or_history"}
                if volumes is None
                else {"or_vol_avg14": or_vol_avg(volumes), "eligible": True}
            )
            rows[symbol] = rows[symbol].model_copy(update=update)

        ordered = [rows[s] for s in symbols]
        excluded.update(row.exclusion_reason for row in ordered if row.exclusion_reason)
        report = UniverseReport(
            date=t,
            total_assets=len(assets),
            candidates=len(symbols),
            eligible=sum(row.eligible for row in ordered),
            excluded=dict(excluded),
        )
        if persist:
            await self._persist(t, ordered)
        log.info("universe built", **report.model_dump(mode="json"))
        return report, ordered

    async def or_baseline(
        self, t: dt.date, symbols: Sequence[str], *, refresh: Iterable[str] = ()
    ) -> dict[str, list[int]]:
        """Opening-range volumes of the 14 sessions before ``t`` per symbol, oldest first.

        Read from ``or_bars`` where cached; missing sessions, and every session of the
        ``refresh`` symbols, come from the live feed. A session without a live-feed bar had no
        prints in the window and stores volume 0 (D-026).
        """
        sessions = self._calendar.last_sessions(t, PERIOD)
        wanted, refreshed = set(symbols), set(refresh)
        volumes = await self._cached_or_volumes(sessions, wanted)
        for day in sessions:
            need = sorted(s for s in wanted if s in refreshed or (day, s) not in volumes)
            if not need:
                continue
            opens = et_datetime(day, REGULAR_OPEN)
            ends = opens + dt.timedelta(minutes=OR_MINUTES) - dt.timedelta(seconds=1)
            fetched = await self._data.bars(
                need, "5Min", opens, ends, feed=self._feed, adjustment="split"
            )
            rows = []
            for symbol in need:
                bar = next((b for b in fetched.get(symbol, []) if b.ts == opens), None)
                rows.append(_or_row(day, symbol, self._feed, bar))
                volumes[(day, symbol)] = bar.volume if bar else 0
            await self._journal.write(_executemany(_INSERT_OR_BAR, rows))
        return {
            s: [volumes[(day, s)] for day in sessions]
            for s in symbols
            if all((day, s) in volumes for day in sessions)
        }

    def _asset_exclusion(self, asset: Asset) -> str | None:
        if asset.asset_class != "us_equity":
            return "asset_class"
        if not self._include_inactive and (asset.status != "active" or not asset.tradable):
            return "not_tradable"
        if asset.exchange not in self._universe.exchanges:
            return "exchange"
        if any(ch in asset.symbol for ch in self._universe.exclude_symbol_chars):
            return "symbol_chars"
        if not is_ticker(asset.symbol):
            return "not_a_ticker"
        return None

    def _features(
        self,
        t: dt.date,
        prev_session: dt.date,
        asset: Asset,
        bars: Sequence[Bar],
        ex_dates: set[dt.date],
    ) -> UniverseRow:
        prev_close = bars[-1].close if bars and bars[-1].ts.date() == prev_session else None
        atr14 = atr(bars) if len(bars) >= PERIOD + 1 else None
        adv14 = adv(bars) if len(bars) >= PERIOD else None
        reason: str | None = None
        if ex_dates & {prev_session, t}:
            reason = "split"
        elif prev_close is None:
            reason = "stale_prev_close"
        elif atr14 is None or adv14 is None:
            reason = "insufficient_history"
        elif adv14 < self._universe.min_adv14:
            reason = "adv"
        elif atr14 <= self._universe.min_atr14:
            reason = "atr"
        return UniverseRow(
            date=t,
            symbol=asset.symbol,
            name=asset.name,
            exchange=asset.exchange,
            prev_close=prev_close,
            atr14=atr14,
            adv14=adv14,
            or_vol_avg14=None,
            eligible=False,
            exclusion_reason=reason,
        )

    async def _cached_or_volumes(
        self, sessions: Sequence[dt.date], symbols: set[str]
    ) -> dict[tuple[dt.date, str], int]:
        rows = await self._journal.fetch_all(
            "SELECT date, symbol, volume FROM or_bars WHERE feed = ? AND date BETWEEN ? AND ?",
            (self._feed, sessions[0].isoformat(), sessions[-1].isoformat()),
        )
        return {
            (dt.date.fromisoformat(row["date"]), row["symbol"]): int(row["volume"])
            for row in rows
            if row["symbol"] in symbols
        }

    async def _persist(self, t: dt.date, rows: Sequence[UniverseRow]) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM universe_daily WHERE date = ?", (t.isoformat(),))
            conn.executemany(
                _INSERT_UNIVERSE,
                [
                    (
                        row.date.isoformat(),
                        row.symbol,
                        row.name,
                        row.exchange,
                        dec_to_db(row.prev_close),
                        dec_to_db(row.atr14),
                        row.adv14,
                        row.or_vol_avg14,
                        int(row.eligible),
                        row.exclusion_reason,
                    )
                    for row in rows
                ],
            )

        await self._journal.write(write)


def _or_row(day: dt.date, symbol: str, feed: str, bar: Bar | None) -> tuple[object, ...]:
    if bar is None:
        return (day.isoformat(), symbol, feed, None, None, None, None, 0)
    prices = (dec_to_db(bar.open), dec_to_db(bar.high), dec_to_db(bar.low), dec_to_db(bar.close))
    return (day.isoformat(), symbol, feed, *prices, bar.volume)


def _executemany(sql: str, rows: list[tuple[object, ...]]) -> Callable[[sqlite3.Connection], None]:
    def write(conn: sqlite3.Connection) -> None:
        conn.executemany(sql, rows)

    return write
