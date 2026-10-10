"""Shared setup for CLI commands that use the journal and, optionally, Alpaca REST."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from trader.config import AppConfig, load_config
from trader.core.calendar import TradingCalendar, default_sync_range, sync_calendar
from trader.core.clock import Clock, RealClock
from trader.core.logging import configure_logging, shutdown_logging
from trader.data.alpaca_rest import AlpacaRest
from trader.memory.journal import Journal


@dataclass(frozen=True)
class Local:
    """Config, clock and journal: enough for commands that never call Alpaca."""

    cfg: AppConfig
    clock: Clock
    journal: Journal


@dataclass(frozen=True)
class Runtime(Local):
    rest: AlpacaRest

    async def calendar(self) -> TradingCalendar:
        """Sync the trading calendar around today (one request) and return it."""
        first, last = default_sync_range(self.clock.now().date())
        calendar, _ = await sync_calendar(self.rest.calendar, self.journal, first, last)
        return calendar


@asynccontextmanager
async def open_local(root: Path) -> AsyncIterator[Local]:
    """Load config, start redacting logs, and open the journal."""
    cfg = load_config(root)
    clock = RealClock()
    configure_logging(
        clock=clock,
        secrets=cfg.secrets.redaction_map(),
        logs_dir=cfg.path(cfg.settings.storage.logs_dir),
    )
    try:
        async with await Journal.open(cfg.path(cfg.settings.storage.db_path)) as journal:
            yield Local(cfg=cfg, clock=clock, journal=journal)
    finally:
        shutdown_logging()


@asynccontextmanager
async def open_runtime(
    root: Path, *, rest_factory: Callable[[AppConfig, Clock], AlpacaRest] | None = None
) -> AsyncIterator[Runtime]:
    """``open_local`` plus an Alpaca REST client."""
    make_rest = rest_factory or AlpacaRest.from_config
    async with open_local(root) as local, make_rest(local.cfg, local.clock) as rest:
        yield Runtime(cfg=local.cfg, clock=local.clock, journal=local.journal, rest=rest)
