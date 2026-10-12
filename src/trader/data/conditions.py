"""Which trades may set a price (DECISIONS D-043).

Alpaca's market-data FAQ lists the trade conditions that don't update a minute bar's high or
low; the strictest rule wins, so one excluded code excludes the trade. Triggers and the hub's
``last_price()`` use the same filter, so they agree with ``or_high`` and with SIP bars. Unknown
codes count: Alpaca doesn't document the IEX feed's conditions.
"""

from trader.core.models import Trade

EXCLUDED_CONDITIONS = frozenset(
    {"B", "C", "G", "H", "I", "M", "N", "P", "Q", "R", "U", "V", "W", "Z", "4", "7", "9"}
)


def counts_for_price(trade: Trade) -> bool:
    return not any(code in EXCLUDED_CONDITIONS for code in trade.conditions)
