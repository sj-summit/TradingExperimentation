"""Reconciliation (A§4.6, RISK.md §5): does the broker agree with the journal?

``Reconciler.check`` first catches up: it reads every order of the day over REST and journals
them through records.py, which books the fills and cancels the stream missed. It then reads
positions before open orders and compares them with the journal through the pure ``diff``.

Symbols with orders still in flight are skipped, and the service acts only on a difference seen
on two passes in a row (DECISIONS D-037). A fill that lands between two reads therefore isn't a
false alarm.
"""

import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from trader.config import BookName
from trader.execution.broker import BrokerAdapter, Order
from trader.execution.records import OWN_STATUSES, Records
from trader.memory.journal import Journal


@dataclass(frozen=True)
class ReconcileDiff:
    unknown_positions: dict[str, int] = field(default_factory=dict)  # held, but not by us
    qty_mismatches: dict[str, tuple[int, int]] = field(default_factory=dict)  # broker, journal
    unknown_orders: tuple[str, ...] = ()  # open at the broker, but not ours

    @property
    def empty(self) -> bool:
        return not (self.unknown_positions or self.qty_mismatches or self.unknown_orders)

    @property
    def positions_differ(self) -> bool:
        return bool(self.unknown_positions or self.qty_mismatches)


def diff(
    *,
    broker_positions: Mapping[str, int],
    journal_positions: Mapping[str, int],
    open_orders: Sequence[Order],
    our_client_ids: Collection[str],
    our_groups: Collection[str],
    skip: Collection[str] = (),
) -> ReconcileDiff:
    """Compare what the broker holds and has open with what the journal says we did."""
    unknown = {
        symbol: qty
        for symbol, qty in broker_positions.items()
        if qty and symbol not in skip and not journal_positions.get(symbol, 0)
    }
    symbols = set(broker_positions) | set(journal_positions)
    mismatched = {
        symbol: (broker_positions.get(symbol, 0), journal_positions.get(symbol, 0))
        for symbol in sorted(symbols)
        if symbol not in skip
        and symbol not in unknown
        and broker_positions.get(symbol, 0) != journal_positions.get(symbol, 0)
    }
    strangers = tuple(
        order.client_order_id
        for order in open_orders
        if order.symbol not in skip
        and order.client_order_id not in our_client_ids
        and (order.group_id is None or order.group_id not in our_groups)
    )
    return ReconcileDiff(unknown, mismatched, strangers)


@dataclass(frozen=True)
class JournalView:
    positions: dict[str, int]
    client_ids: frozenset[str]
    groups: frozenset[str]
    in_flight: frozenset[str]  # symbols with an order we sent but the broker hasn't confirmed


def journal_view(conn: sqlite3.Connection, book: str) -> JournalView:
    """The journal's side, read inside one transaction."""
    positions: dict[str, int] = {}
    for row in conn.execute(
        "SELECT symbol, SUM(CASE side WHEN 'buy' THEN qty ELSE -qty END) AS held FROM fills "
        "WHERE book = ? GROUP BY symbol",
        (book,),
    ):
        if row["held"]:
            positions[row["symbol"]] = int(row["held"])
    orders = conn.execute(
        "SELECT client_order_id, group_id, symbol, status FROM orders WHERE book = ?", (book,)
    ).fetchall()
    return JournalView(
        positions=positions,
        client_ids=frozenset(row["client_order_id"] for row in orders),
        groups=frozenset(row["group_id"] for row in orders if row["group_id"]),
        in_flight=frozenset(row["symbol"] for row in orders if row["status"] in OWN_STATUSES),
    )


class Reconciler:
    def __init__(
        self, *, book: BookName, broker: BrokerAdapter, records: Records, journal: Journal
    ) -> None:
        self.book, self._broker, self._records, self._journal = book, broker, records, journal

    async def check(self, *, since: datetime, skip: Collection[str] = ()) -> ReconcileDiff:
        """Catch up on the day's orders, then compare. ``skip``: symbols the caller knows are
        mid-change (e.g. a flatten in progress).

        Only orders the journal already knows (by id, or as a leg by group) are journaled: an
        order someone else placed must stay a stranger, or the diff could never see it.
        """
        known = await self._view()
        for order in await self._broker.orders_since(since):
            if _ours(order, known):
                await self._records.apply(order)
        positions = {p.symbol: p.qty for p in await self._broker.positions()}
        open_orders = await self._broker.open_orders()
        for order in open_orders:
            if _ours(order, known):
                await self._records.apply(order)
        view = await self._view()
        return diff(
            broker_positions=positions,
            journal_positions=view.positions,
            open_orders=open_orders,
            our_client_ids=view.client_ids,
            our_groups=view.groups,
            skip=set(skip) | view.in_flight,
        )

    async def _view(self) -> JournalView:
        return await self._journal.read(lambda conn: journal_view(conn, self.book))


def _ours(order: Order, view: JournalView) -> bool:
    return order.client_order_id in view.client_ids or (
        order.group_id is not None and order.group_id in view.groups
    )
