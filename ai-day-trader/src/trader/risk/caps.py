"""Hard risk caps (RISK.md §1). Human-owned ceilings that configuration can never exceed.

Config loading fails fast on any violation (trader.config). Claude Code must ask before editing
this file (.claude/settings.json ask rule) and never loosens a value without an explicit human
instruction in the current session (CLAUDE.md invariant 2).
"""

from decimal import Decimal
from typing import Final

MAX_RISK_PER_TRADE_PCT: Final[Decimal] = Decimal("2.0")  # configured per-trade risk <= 2% of equity
MAX_DAILY_LOSS_LIMIT_PCT: Final[Decimal] = Decimal("5.0")  # configured daily loss limit <= 5%
MAX_POSITION_PCT: Final[Decimal] = Decimal("100.0")  # notional per position <= 100% of equity
MAX_CONCURRENT_POSITIONS: Final[int] = 3
MAX_ENTRIES_PER_DAY: Final[int] = 6
LEVERAGE: Final[Decimal] = Decimal("1.0")  # gross exposure <= cash; margin is never used
ALLOW_SHORT: Final[bool] = False
ALLOW_EXTENDED_HOURS: Final[bool] = False
ALLOWED_ASSET_CLASSES: Final[frozenset[str]] = frozenset({"us_equity"})
MIN_PRICE_FLOOR: Final[Decimal] = Decimal("1.00")  # absolute floor (config default is 5.00)
LIVE_MAX_ACCOUNT_EQUITY: Final[Decimal] = Decimal("2000.00")  # refuse a richer live account
FLATTEN_LATEST_BEFORE_CLOSE_MIN: Final[int] = 3  # flatten time <= close - 3 min
