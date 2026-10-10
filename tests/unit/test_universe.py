from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fixtures import nyse
from trader import runtime
from trader.cli import app
from trader.config import UniverseConfig, load_config
from trader.core.calendar import NotATradingDayError, TradingCalendar, parse_alpaca_calendar
from trader.core.clock import ET, SimClock, et_datetime
from trader.core.models import Asset, Bar, Split
from trader.data.recorder import read_table
from trader.data.universe import REASONS, UniverseBuilder, default_target
from trader.memory.journal import Journal

D = Decimal
T = date(2026, 10, 9)  # Friday
CAL = TradingCalendar(parse_alpaca_calendar(nyse.calendar_rows()), nyse.FIRST, nyse.LAST)
SESSIONS = CAL.last_sessions(T, 15)  # 2026-09-18 .. 2026-10-08
OR_SESSIONS = SESSIONS[1:]  # the 14 sessions of the opening-range baseline


def asset(symbol: str, exchange: str = "NASDAQ", tradable: bool = True) -> Asset:
    return Asset(
        symbol=symbol,
        name=f"{symbol} Inc",
        exchange=exchange,
        asset_class="us_equity",
        status="active",
        tradable=tradable,
    )


def daily(days: Sequence[date], high: str, low: str, close: str, volume: int) -> list[Bar]:
    def bar(d: date) -> Bar:
        return Bar(
            ts=et_datetime(d, time(0)),
            open=D(close),
            high=D(high),
            low=D(low),
            close=D(close),
            volume=volume,
        )

    return [bar(d) for d in days]


def split(symbol: str, ex_date: date) -> Split:
    return Split(
        symbol=symbol,
        ex_date=ex_date,
        process_date=None,
        old_rate=D(1),
        new_rate=D(2),
        reverse=False,
    )


GOOD_DAILY = ("21.00", "20.00", "20.50", 2_000_000)  # true range 1.00 every session


class FakeMarket:
    """In-memory stand-in for AlpacaRest; records every bars() request."""

    def __init__(self) -> None:
        self.assets_list = [
            asset("GOOD"),
            asset("QUIET"),
            asset("THIN"),
            asset("CALM"),
            asset("NEWB"),
            asset("HALT"),
            asset("SPLT"),
            asset("SPL2"),
            asset("ETFX", exchange="ARCA"),
            asset("OTCX", exchange="OTC"),
            asset("BRK.B", exchange="NYSE"),
            asset("DEAD", tradable=False),
        ]
        full = list(SESSIONS)
        self.daily = {
            "GOOD": daily(full, *GOOD_DAILY),
            "QUIET": daily(full, *GOOD_DAILY),
            "THIN": daily(full, "21.00", "20.00", "20.50", 500_000),
            "CALM": daily(full, "20.20", "20.00", "20.10", 2_000_000),  # ATR 0.20
            "NEWB": daily(full[-10:], *GOOD_DAILY),
            "HALT": daily(full[:-1], *GOOD_DAILY),  # no bar on t-1
            "SPLT": daily(full, *GOOD_DAILY),
            "SPL2": daily(full, *GOOD_DAILY),
            "ETFX": daily(full, *GOOD_DAILY),
        }
        self.or_volume: dict[tuple[date, str], int] = {}
        for day in OR_SESSIONS:
            for symbol in ("GOOD", "SPL2", "ETFX", "SPLT", "THIN", "CALM", "HALT"):
                self.or_volume[(day, symbol)] = 3_000
        for day in OR_SESSIONS[::2]:  # IEX printed in the opening range on 7 of 14 sessions
            self.or_volume[(day, "QUIET")] = 2_000
        self.split_list = [split("SPLT", SESSIONS[-1]), split("SPL2", date(2026, 10, 1))]
        self.requests: list[tuple[str, tuple[str, ...], datetime, datetime | None, str, str]] = []

    async def assets(self) -> list[Asset]:
        return self.assets_list

    async def bars(
        self,
        symbols: Sequence[str],
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
        *,
        feed: str,
        adjustment: str = "raw",
    ) -> dict[str, list[Bar]]:
        self.requests.append((timeframe, tuple(symbols), start, end, feed, adjustment))
        if timeframe == "1Day":
            return {s: self.daily[s] for s in symbols if s in self.daily}
        day, px = start.date(), D("20.00")
        return {
            s: [Bar(ts=start, open=px, high=px, low=px, close=px, volume=self.or_volume[(day, s)])]
            for s in symbols
            if (day, s) in self.or_volume
        }

    async def splits(self, start: date, end: date) -> list[Split]:
        return [s for s in self.split_list if start <= s.ex_date <= end]

    # used by the CLI through runtime.open_runtime
    async def calendar(self, start: date, end: date) -> list[dict[str, str]]:
        return nyse.calendar_rows(max(start, nyse.FIRST), min(end, nyse.LAST))

    async def __aenter__(self) -> "FakeMarket":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


UNIVERSE = UniverseConfig(
    exchanges=("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"),
    exclude_symbol_chars=(".", "/", "-", " "),
    min_adv14=1_000_000,
    min_atr14=D("0.50"),
    min_history_sessions=14,
    exclude_corporate_action_days=1,
)


def builder(market: FakeMarket, journal: Journal) -> UniverseBuilder:
    return UniverseBuilder(
        data=market, journal=journal, calendar=CAL, universe=UNIVERSE, feed="iex"
    )


def five_minute_requests(market: FakeMarket) -> list[tuple[str, ...]]:
    return [symbols for tf, symbols, *_ in market.requests if tf == "5Min"]


async def stored_rows(journal: Journal) -> list[object]:
    return list(await journal.fetch_all("SELECT * FROM universe_daily WHERE date = ?", (str(T),)))


# ---------------------------------------------------------------- eligibility


async def test_build_applies_every_rule(tmp_path: Path) -> None:
    async with await Journal.open(tmp_path / "trader.db") as journal:
        report, rows = await builder(FakeMarket(), journal).build(T)
        stored = await stored_rows(journal)
    reasons = {row.symbol: row.exclusion_reason for row in rows}
    assert reasons == {
        "CALM": "atr",
        "ETFX": None,
        "GOOD": None,
        "HALT": "stale_prev_close",
        "NEWB": "insufficient_history",
        "QUIET": None,
        "SPL2": None,  # split inside the window, but not on t or t-1
        "SPLT": "split",
        "THIN": "adv",
    }
    assert (report.total_assets, report.candidates, report.eligible) == (12, 9, 4)
    assert report.excluded == {
        "exchange": 1,
        "symbol_chars": 1,
        "not_tradable": 1,
        "adv": 1,
        "atr": 1,
        "insufficient_history": 1,
        "stale_prev_close": 1,
        "split": 1,
    }
    assert set(report.excluded) - {"exchange", "symbol_chars", "not_tradable"} <= set(REASONS)
    assert len(stored) == 9  # one row per candidate; asset-level exclusions are only counted


async def test_features_are_stored(tmp_path: Path) -> None:
    async with await Journal.open(tmp_path / "trader.db") as journal:
        await builder(FakeMarket(), journal).build(T)
        good = await journal.fetch_one("SELECT * FROM universe_daily WHERE symbol = 'GOOD'")
        quiet = await journal.fetch_one("SELECT * FROM universe_daily WHERE symbol = 'QUIET'")
    assert good is not None
    assert quiet is not None
    assert (good["prev_close"], good["atr14"], good["adv14"]) == ("20.50", "1.0000", 2_000_000)
    assert good["or_vol_avg14"] == pytest.approx(3_000.0)
    assert good["eligible"] == 1
    assert quiet["or_vol_avg14"] == pytest.approx(1_000.0)  # 7 x 2,000 + 7 x 0 over 14


async def test_requests_use_split_adjusted_sip_daily_and_live_feed_or_bars(tmp_path: Path) -> None:
    market = FakeMarket()
    async with await Journal.open(tmp_path / "trader.db") as journal:
        await builder(market, journal).build(T)
    daily_requests = [r for r in market.requests if r[0] == "1Day"]
    assert len(daily_requests) == 1
    _, _, start, end, feed, adjustment = daily_requests[0]
    assert (start, end, feed, adjustment) == (
        et_datetime(SESSIONS[0], time(0)),
        None,
        "sip",
        "split",
    )
    or_requests = [r for r in market.requests if r[0] == "5Min"]
    assert len(or_requests) == 14  # one per baseline session
    _, symbols, start, end, feed, adjustment = or_requests[0]
    assert symbols == ("ETFX", "GOOD", "QUIET", "SPL2")  # only symbols that passed so far
    assert (start.time(), feed, adjustment) == (time(9, 30), "iex", "split")
    assert end is not None
    assert end.time() == time(9, 34, 59)


# ---------------------------------------------------------------- baselines


async def test_sessions_without_iex_prints_store_zero_volume(tmp_path: Path) -> None:
    async with await Journal.open(tmp_path / "trader.db") as journal:
        await builder(FakeMarket(), journal).build(T)
        rows = await journal.fetch_all(
            "SELECT date, open, volume FROM or_bars WHERE symbol = 'QUIET' ORDER BY date"
        )
    assert len(rows) == 14
    zero = [r for r in rows if r["volume"] == 0]
    assert len(zero) == 7
    assert all(r["open"] is None for r in zero)


async def test_baselines_are_cached_except_after_a_split(tmp_path: Path) -> None:
    market = FakeMarket()
    async with await Journal.open(tmp_path / "trader.db") as journal:
        await builder(market, journal).build(T)
        market.requests.clear()
        market.or_volume.update({(d, "SPL2"): 6_000 for d in OR_SESSIONS})  # post-split shares
        report, rows = await builder(market, journal).build(T)
        stored = await stored_rows(journal)
    assert five_minute_requests(market) == [("SPL2",)] * 14  # only the split symbol is refetched
    spl2 = next(row for row in rows if row.symbol == "SPL2")
    assert spl2.or_vol_avg14 == pytest.approx(6_000.0)
    assert len(stored) == 9  # rebuilding replaces the day's rows
    assert report.eligible == 4


async def test_or_baseline_alone_writes_no_universe(tmp_path: Path) -> None:
    async with await Journal.open(tmp_path / "trader.db") as journal:
        _, rows = await builder(FakeMarket(), journal).build(T, persist=False)
        stored = await journal.fetch_all("SELECT * FROM universe_daily")
        cached = await journal.fetch_all("SELECT * FROM or_bars")
    assert stored == []
    assert len(cached) == 4 * 14
    assert sum(row.eligible for row in rows) == 4


async def test_building_for_a_holiday_raises(tmp_path: Path) -> None:
    async with await Journal.open(tmp_path / "trader.db") as journal:
        with pytest.raises(NotATradingDayError):
            await builder(FakeMarket(), journal).build(date(2026, 11, 26))


# ---------------------------------------------------------------- target date


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 9, 8, 0, tzinfo=ET), date(2026, 10, 9)),  # before the open
        (datetime(2026, 10, 9, 10, 0, tzinfo=ET), date(2026, 10, 12)),  # after it
        (datetime(2026, 10, 10, 12, 0, tzinfo=ET), date(2026, 10, 12)),  # Saturday
        (datetime(2026, 11, 26, 8, 0, tzinfo=ET), date(2026, 11, 27)),  # Thanksgiving
    ],
)
def test_default_target(now: datetime, expected: date) -> None:
    assert default_target(CAL, now) == expected


# ---------------------------------------------------------------- CLI


def test_cli_universe_build(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    market = FakeMarket()
    monkeypatch.setattr(
        runtime, "RealClock", lambda: SimClock(datetime(2026, 10, 8, 20, tzinfo=ET))
    )
    monkeypatch.setattr(runtime.AlpacaRest, "from_config", lambda _cfg, _clock: market)
    result = CliRunner().invoke(app, ["--root", str(repo), "universe", "build"])
    assert result.exit_code == 0, result.output
    assert "universe for 2026-10-09: 12 assets, 9 candidates, 4 eligible" in result.output
    assert (repo / "runtime" / "trader.db").is_file()
    snapshot = read_table(repo / "runtime" / "recordings" / "2026-10-09" / "universe.parquet")
    assert len(snapshot) == 9


def test_cli_reports_a_holiday_cleanly(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime, "RealClock", lambda: SimClock(datetime(2026, 10, 8, 20, tzinfo=ET))
    )
    monkeypatch.setattr(runtime.AlpacaRest, "from_config", lambda _cfg, _clock: FakeMarket())
    args = ["--root", str(repo), "universe", "build", "--date", "2026-11-26"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert "not a trading day" in result.output


def test_settings_universe_matches_the_test_config(repo: Path) -> None:
    assert load_config(repo).settings.strategy.universe == UNIVERSE
