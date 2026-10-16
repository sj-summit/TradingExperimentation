"""Historical inputs for the backtest (P3.9; DECISIONS D-042), fetched once and served as of
each simulated day.

The cache is its own SQLite database (the journal's schema plus ``bt_*`` tables), so a second
run over the same range makes no requests. ``BacktestData.at(t)`` returns a view that answers
the same calls the live universe build and scan make, but as the market looked before day t:

- **Daily bars** stop before t, and ``adjustment="split"`` adjusts only for splits whose
  ``ex_date`` is on or before t. Alpaca's own split adjustment uses today's share basis, so a
  later 1:10 reverse split would lift a $0.80 stock past the $5 filter (look-ahead).
- **Splits** are those with ``ex_date`` up to t.
- **Opening-range bars** exist only for sessions up to t.
- Tickers are queried with ``asof="-"`` (no symbol mapping): each day sees the tickers as they
  traded. Assets include inactive (delisted) ones, against survivorship bias.
"""

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Literal, Protocol

import structlog

from trader.core.calendar import TradingCalendar
from trader.core.clock import ET, et_datetime, fmt_iso, parse_et_iso
from trader.core.models import Asset, Bar, SipTrade, Split
from trader.data.alpaca_rest import Adjustment, AlpacaError, Feed, Timeframe
from trader.memory.journal import Journal, db_to_dec, dec_to_db

log = structlog.get_logger(__name__)
INVALID_SYMBOL = re.compile(r"invalid symbol: (\S+)")

OR_START = time(9, 30)
OR_MINUTES = 5
LOOKBACK_SESSIONS = 15  # ATR14 needs 15 daily bars before the first day (D-025)
SPLIT_MARGIN = timedelta(days=30)
DAILY_CHUNK = 400  # symbols per daily-bar fetch, so a chunk's bars fit comfortably in memory

CACHE_TABLES = (
    "CREATE TABLE IF NOT EXISTS bt_assets (symbol TEXT PRIMARY KEY, name TEXT, exchange TEXT, "
    "asset_class TEXT, status TEXT, tradable INTEGER)",
    "CREATE TABLE IF NOT EXISTS bt_daily (symbol TEXT, date TEXT, open TEXT, high TEXT, "
    "low TEXT, close TEXT, volume INTEGER, PRIMARY KEY (symbol, date))",
    "CREATE INDEX IF NOT EXISTS ix_bt_daily_date ON bt_daily(date)",
    "CREATE TABLE IF NOT EXISTS bt_splits (symbol TEXT, ex_date TEXT, old_rate TEXT, "
    "new_rate TEXT, reverse INTEGER, PRIMARY KEY (symbol, ex_date))",
    "CREATE TABLE IF NOT EXISTS bt_or_raw (symbol TEXT, date TEXT, feed TEXT, open TEXT, "
    "high TEXT, low TEXT, close TEXT, volume INTEGER, PRIMARY KEY (symbol, date, feed))",
    "CREATE TABLE IF NOT EXISTS bt_minute (symbol TEXT, date TEXT, bars_json TEXT, "
    "PRIMARY KEY (symbol, date))",
    "CREATE TABLE IF NOT EXISTS bt_fetched (kind TEXT, key TEXT, PRIMARY KEY (kind, key))",
    "CREATE TABLE IF NOT EXISTS bt_tape (symbol TEXT, minute TEXT, prints_json TEXT, "
    "PRIMARY KEY (symbol, minute))",
)


class HistorySource(Protocol):
    """The slice of AlpacaRest the backtest fetches through."""

    async def assets(
        self, *, status: Literal["active", "inactive"] | None = "active"
    ) -> list[Asset]: ...

    async def bars(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        *,
        feed: Feed,
        adjustment: Adjustment = "raw",
        asof: str | None = None,
    ) -> dict[str, list[Bar]]: ...

    async def splits(self, start: date, end: date) -> list[Split]: ...

    async def trades(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        *,
        feed: Feed,
        asof: str | None = None,
    ) -> dict[str, list[SipTrade]]: ...


class LookAheadError(RuntimeError):
    """The backtest asked for data after the simulated day."""


class BacktestData:
    """The cache plus the splits and assets of one backtest range."""

    def __init__(self, *, source: HistorySource, cache: Journal, calendar: TradingCalendar) -> None:
        self._source, self.cache, self._calendar = source, cache, calendar
        self.assets: list[Asset] = []
        self._splits: dict[str, list[Split]] = defaultdict(list)
        self.requests = 0  # fetches made through the source (a cached rerun makes none)

    # ---------------------------------------------------------------- preparation

    async def prepare(self, start: date, end: date, *, symbol_ok: Callable[[Asset], bool]) -> None:
        """Make sure the cache holds assets, daily bars and splits for [the first lookback
        session, end], fetching only what it lacks."""
        await self.cache.write(_create_tables)
        # The universe build caches OR baselines in or_bars, and a later split restates a
        # symbol's rows in the new share basis. A rerun reaching a day before that split would
        # read them: look-ahead. bt_or_raw keeps every raw bar, so rebuild them each run.
        await self.cache.execute("DELETE FROM or_bars")
        first = self._calendar.last_sessions(start, LOOKBACK_SESSIONS)[0]
        if not await self._fetched("assets", "all"):
            fetched = await self._source.assets(status=None)
            self.requests += 1
            await self.cache.write(lambda conn: _store_assets(conn, fetched))
            await self._mark("assets", "all")
        self.assets = await self._read_assets()
        symbols = sorted(a.symbol for a in self.assets if symbol_ok(a))
        span = f"{first}..{end}"
        if not await self._fetched("daily", span):
            for i in range(0, len(symbols), DAILY_CHUNK):
                daily = await self._daily_chunk(symbols[i : i + DAILY_CHUNK], first, end)
                await self.cache.write(_daily_writer(daily))
            await self._mark("daily", span)
        if not await self._fetched("splits", span):
            found = await self._source.splits(first - SPLIT_MARGIN, end + SPLIT_MARGIN)
            self.requests += 1
            await self.cache.write(lambda conn: _store_splits(conn, found))
            await self._mark("splits", span)
        self._splits = defaultdict(list)
        for split in await self._read_splits():
            self._splits[split.symbol].append(split)
        log.info(
            "backtest data ready",
            assets=len(self.assets),
            candidates=len(symbols),
            splits=sum(len(v) for v in self._splits.values()),
            requests=self.requests,
        )

    async def _daily_chunk(
        self, chunk: Sequence[str], first: date, end: date
    ) -> dict[str, list[Bar]]:
        """One chunk of raw daily bars. Alpaca refuses a whole request over one symbol it calls
        invalid; that symbol is dropped (and logged) and the rest asked again."""
        wanted = list(chunk)
        while wanted:
            try:
                found = await self._source.bars(
                    wanted,
                    "1Day",
                    et_datetime(first, time(0)),
                    et_datetime(end, time(23, 59)),
                    feed="sip",
                    adjustment="raw",
                    asof="-",
                )
            except AlpacaError as exc:
                bad = INVALID_SYMBOL.search(str(exc))
                if exc.status != 400 or bad is None or bad.group(1) not in wanted:
                    raise
                log.warning("symbol refused by the bars endpoint", symbol=bad.group(1))
                wanted.remove(bad.group(1))
                continue
            finally:
                self.requests += 1
            return found
        return {}

    def at(self, t: date) -> "AsOfView":
        return AsOfView(self, t)

    async def daily_closes(self, symbol: str, start: date, end: date) -> dict[date, Decimal]:
        """Raw closes over [start, end], e.g. SPY's for the buy-and-hold comparison."""
        rows = await self.cache.fetch_all(
            "SELECT date, close FROM bt_daily WHERE symbol = ? AND date BETWEEN ? AND ? "
            "ORDER BY date",
            (symbol, start.isoformat(), end.isoformat()),
        )
        return {date.fromisoformat(r["date"]): Decimal(r["close"]) for r in rows}

    async def minute_bars(self, symbols: Sequence[str], day: date) -> dict[str, list[Bar]]:
        """SIP 1-minute bars of ``day``'s regular session, raw (the same as of ``day``)."""
        wanted = sorted(set(symbols))
        have = await self._minute_cached(wanted, day)
        missing = [s for s in wanted if s not in have]
        if missing:
            session = self._calendar.session(day)
            fetched = await self._source.bars(
                missing,
                "1Min",
                session.open,
                session.close - timedelta(seconds=1),
                feed="sip",
                adjustment="raw",
                asof="-",
            )
            self.requests += 1
            rows = [(s, day.isoformat(), _bars_json(fetched.get(s, []))) for s in missing]
            await self.cache.write(
                lambda conn: conn.executemany(
                    "INSERT OR REPLACE INTO bt_minute (symbol, date, bars_json) VALUES (?, ?, ?)",
                    rows,
                )
            )
            have |= {s: fetched.get(s, []) for s in missing}
        return have

    async def tape(self, symbol: str, minute: datetime, *, fetch: bool) -> list[SipTrade] | None:
        """The SIP prints of one trigger minute (D-057): from the cache, or fetched and cached
        first when ``fetch`` (``trader backtest --tape``). None when not cached and not fetched,
        or when Alpaca refuses: the pessimistic fallback applies."""
        key = fmt_iso(minute)
        row = await self.cache.fetch_one(
            "SELECT prints_json FROM bt_tape WHERE symbol = ? AND minute = ?", (symbol, key)
        )
        if row is not None:
            return _prints_from_json(symbol, row["prints_json"])
        if not fetch:
            return None
        try:
            found = await self._source.trades(
                [symbol], minute, minute + timedelta(minutes=1), feed="sip", asof="-"
            )
        except AlpacaError as exc:
            log.warning("trigger minute tape refused", symbol=symbol, minute=key, error=str(exc))
            return None
        finally:
            self.requests += 1
        prints = found.get(symbol, [])
        text = _prints_json(prints)
        await self.cache.execute(
            "INSERT OR REPLACE INTO bt_tape (symbol, minute, prints_json) VALUES (?, ?, ?)",
            (symbol, key, text),
        )
        return prints

    # ---------------------------------------------------------------- serving the view

    async def daily_rows(self, start: date, before: date) -> dict[str, list[sqlite3.Row]]:
        rows = await self.cache.fetch_all(
            "SELECT symbol, date, open, high, low, close, volume FROM bt_daily "
            "WHERE date >= ? AND date < ? ORDER BY symbol, date",
            (start.isoformat(), before.isoformat()),
        )
        out: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            out[r["symbol"]].append(r)
        return out

    def split_factor(self, symbol: str, bar_day: date, t: date) -> Decimal:
        """Price multiplier that restates a ``bar_day`` price in day t's share basis."""
        factor = Decimal(1)
        for split in self._splits.get(symbol, []):
            if bar_day < split.ex_date <= t:
                factor *= split.old_rate / split.new_rate
        return factor

    def splits_between(self, start: date, end: date) -> list[Split]:
        return sorted(
            (s for splits in self._splits.values() for s in splits if start <= s.ex_date <= end),
            key=lambda s: (s.ex_date, s.symbol),
        )

    async def or_bars(self, symbols: Sequence[str], session: date, feed: Feed) -> dict[str, Bar]:
        """Raw 09:30 opening-range bars of ``session``, fetched once (absent: no prints)."""
        wanted = sorted(set(symbols))
        cached = await self._or_cached(wanted, session, feed)
        missing = [s for s in wanted if s not in cached]
        if missing:
            opens = et_datetime(session, OR_START)
            fetched = await self._source.bars(
                missing,
                "5Min",
                opens,
                opens + timedelta(minutes=OR_MINUTES) - timedelta(seconds=1),
                feed=feed,
                adjustment="raw",
                asof="-",
            )
            self.requests += 1
            found = {
                s: next((b for b in fetched.get(s, []) if b.ts == opens), None) for s in missing
            }
            await self.cache.write(lambda conn: _store_or(conn, session, feed, found))
            cached |= found
        return {s: bar for s, bar in cached.items() if bar is not None}

    # ---------------------------------------------------------------- cache reads

    async def _fetched(self, kind: str, key: str) -> bool:
        row = await self.cache.fetch_one(
            "SELECT 1 FROM bt_fetched WHERE kind = ? AND key = ?", (kind, key)
        )
        return row is not None

    async def _mark(self, kind: str, key: str) -> None:
        await self.cache.execute(
            "INSERT OR IGNORE INTO bt_fetched (kind, key) VALUES (?, ?)", (kind, key)
        )

    async def _read_assets(self) -> list[Asset]:
        rows = await self.cache.fetch_all("SELECT * FROM bt_assets ORDER BY symbol")
        return [
            Asset(
                symbol=r["symbol"],
                name=r["name"],
                exchange=r["exchange"],
                asset_class=r["asset_class"],
                status=r["status"],
                tradable=bool(r["tradable"]),
            )
            for r in rows
        ]

    async def _read_splits(self) -> list[Split]:
        rows = await self.cache.fetch_all("SELECT * FROM bt_splits ORDER BY symbol, ex_date")
        return [
            Split(
                symbol=r["symbol"],
                ex_date=date.fromisoformat(r["ex_date"]),
                process_date=None,
                old_rate=Decimal(r["old_rate"]),
                new_rate=Decimal(r["new_rate"]),
                reverse=bool(r["reverse"]),
            )
            for r in rows
        ]

    async def _or_cached(
        self, symbols: Sequence[str], session: date, feed: Feed
    ) -> dict[str, Bar | None]:
        rows = await self.cache.fetch_all(
            "SELECT symbol, open, high, low, close, volume FROM bt_or_raw "
            "WHERE date = ? AND feed = ?",
            (session.isoformat(), feed),
        )
        wanted, opens = set(symbols), et_datetime(session, OR_START)
        out: dict[str, Bar | None] = {}
        for r in rows:
            if r["symbol"] not in wanted:
                continue
            o, h, low, c = (db_to_dec(r[k]) for k in ("open", "high", "low", "close"))
            out[r["symbol"]] = (
                None
                if o is None or h is None or low is None or c is None
                else Bar(ts=opens, open=o, high=h, low=low, close=c, volume=int(r["volume"]))
            )
        return out

    async def _minute_cached(self, symbols: Sequence[str], day: date) -> dict[str, list[Bar]]:
        rows = await self.cache.fetch_all(
            "SELECT symbol, bars_json FROM bt_minute WHERE date = ?", (day.isoformat(),)
        )
        wanted = set(symbols)
        return {r["symbol"]: _bars_from_json(r["bars_json"]) for r in rows if r["symbol"] in wanted}


class AsOfView:
    """The market as it looked before day ``t``: the universe-build and scan interface."""

    def __init__(self, data: BacktestData, t: date) -> None:
        self._data, self.t = data, t

    async def assets(self) -> list[Asset]:
        return self._data.assets

    async def bars(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        *,
        feed: Feed,
        adjustment: Adjustment = "raw",
    ) -> dict[str, list[Bar]]:
        if timeframe == "1Day":
            before = self.t if end is None else min(self.t, end.date() + timedelta(days=1))
            rows = await self._data.daily_rows(start.date(), before)
            return {
                s: [self._daily_bar(s, r, adjustment) for r in rows[s]]
                for s in symbols
                if s in rows
            }
        if timeframe == "5Min":
            session = start.date()
            if session > self.t:
                raise LookAheadError(f"opening-range bars of {session} asked for on {self.t}")
            raw = await self._data.or_bars(symbols, session, feed)
            return {
                s: [_restate(bar, self._factor(s, session, adjustment))] for s, bar in raw.items()
            }
        raise ValueError(f"the backtest view serves 1Day and 5Min bars, not {timeframe}")

    async def splits(self, start: date, end: date) -> list[Split]:
        return self._data.splits_between(start, min(end, self.t))

    def _factor(self, symbol: str, bar_day: date, adjustment: Adjustment) -> Decimal:
        if adjustment == "raw":
            return Decimal(1)
        return self._data.split_factor(symbol, bar_day, self.t)

    def _daily_bar(self, symbol: str, r: sqlite3.Row, adjustment: Adjustment) -> Bar:
        day = date.fromisoformat(r["date"])
        bar = Bar.model_construct(
            ts=datetime.combine(day, time(0), ET),
            open=Decimal(r["open"]),
            high=Decimal(r["high"]),
            low=Decimal(r["low"]),
            close=Decimal(r["close"]),
            volume=int(r["volume"]),
            trades=None,
            vwap=None,
        )
        return _restate(bar, self._factor(symbol, day, adjustment))


def _restate(bar: Bar, factor: Decimal) -> Bar:
    """Prices x factor, volume / factor: a split-adjusted bar in the target share basis."""
    if factor == 1:
        return bar
    volume = int((Decimal(bar.volume) / factor).to_integral_value(rounding=ROUND_FLOOR))
    return Bar.model_construct(
        ts=bar.ts,
        open=bar.open * factor,
        high=bar.high * factor,
        low=bar.low * factor,
        close=bar.close * factor,
        volume=volume,
        trades=None,
        vwap=None,
    )


# ---------------------------------------------------------------- cache writes


def _create_tables(conn: sqlite3.Connection) -> None:
    for sql in CACHE_TABLES:
        conn.execute(sql)


def _store_assets(conn: sqlite3.Connection, assets: Sequence[Asset]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO bt_assets (symbol, name, exchange, asset_class, status, tradable) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(a.symbol, a.name, a.exchange, a.asset_class, a.status, int(a.tradable)) for a in assets],
    )


def _store_daily(conn: sqlite3.Connection, daily: dict[str, list[Bar]]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO bt_daily (symbol, date, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (s, b.ts.date().isoformat(), str(b.open), str(b.high), str(b.low), str(b.close),
             b.volume)
            for s, bars in daily.items()
            for b in bars
        ],
    )  # fmt: skip


def _daily_writer(daily: dict[str, list[Bar]]) -> Callable[[sqlite3.Connection], None]:
    def write(conn: sqlite3.Connection) -> None:
        _store_daily(conn, daily)

    return write


def _store_splits(conn: sqlite3.Connection, splits: Sequence[Split]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO bt_splits (symbol, ex_date, old_rate, new_rate, reverse) "
        "VALUES (?, ?, ?, ?, ?)",
        [(s.symbol, s.ex_date.isoformat(), str(s.old_rate), str(s.new_rate), int(s.reverse))
         for s in splits],
    )  # fmt: skip


def _store_or(
    conn: sqlite3.Connection, session: date, feed: Feed, found: dict[str, Bar | None]
) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO bt_or_raw (symbol, date, feed, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (s, session.isoformat(), feed, dec_to_db(b.open if b else None),
             dec_to_db(b.high if b else None), dec_to_db(b.low if b else None),
             dec_to_db(b.close if b else None), b.volume if b else 0)
            for s, b in found.items()
        ],
    )  # fmt: skip


def _bars_json(bars: Sequence[Bar]) -> str:
    return json.dumps(
        [[b.ts.isoformat(), str(b.open), str(b.high), str(b.low), str(b.close), b.volume]
         for b in bars]
    )  # fmt: skip


def _prints_json(prints: Sequence[SipTrade]) -> str:
    return json.dumps(
        [[p.ts.isoformat(), str(p.price), p.size, list(p.conditions), p.update] for p in prints]
    )


def _prints_from_json(symbol: str, text: str) -> list[SipTrade]:
    return [
        SipTrade(symbol=symbol, ts=parse_et_iso(ts), price=Decimal(price), size=int(size),
                 conditions=tuple(conditions), update=update)
        for ts, price, size, conditions, update in json.loads(text)
    ]  # fmt: skip


def _bars_from_json(text: str) -> list[Bar]:
    return [
        Bar(ts=parse_et_iso(ts), open=Decimal(o), high=Decimal(h), low=Decimal(low),
            close=Decimal(c), volume=int(v))
        for ts, o, h, low, c, v in json.loads(text)
    ]  # fmt: skip
