"""Deterministic IDs (ARCHITECTURE.md §4.1)."""

import re
from datetime import date
from typing import Literal, get_args

Book = Literal["A", "B"]
# "smoke" marks the 1-share entry of `trader smoke paper`, so it never takes a strategy id.
Leg = Literal["entry", "stop", "exit", "smoke"]

# A§4.1's limit. Alpaca now accepts 128 characters; its older docs and FIX spec say 48, so 48
# is safe everywhere (DECISIONS D-030).
MAX_CLIENT_ORDER_ID_LEN = 48
_CLIENT_ORDER_ID_RE = re.compile(r"[A-Za-z0-9-]+")
_SYMBOL_RE = re.compile(r"[A-Z]{1,6}")


def signal_id(d: date, symbol: str) -> str:
    """``YYYYMMDD-SYMBOL-orb5``: one ORB signal per symbol per day."""
    _check_symbol(symbol)
    return f"{d:%Y%m%d}-{symbol}-orb5"


def client_order_id(book: Book, d: date, symbol: str, leg: Leg, attempt: int) -> str:
    """``BOOK-YYYYMMDD-SYMBOL-LEG-ATTEMPT``. Resubmitting after a confirmed miss uses attempt+1."""
    if book not in get_args(Book):
        raise ValueError(f"unknown book {book!r}")
    if leg not in get_args(Leg):
        raise ValueError(f"unknown leg {leg!r}")
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    _check_symbol(symbol)
    return check_client_order_id(f"{book}-{d:%Y%m%d}-{symbol}-{leg}-{attempt}")


def check_client_order_id(cid: str) -> str:
    """Return ``cid`` if it fits A§4.1's format (``[A-Za-z0-9-]``, at most 48 chars)."""
    if len(cid) > MAX_CLIENT_ORDER_ID_LEN or not _CLIENT_ORDER_ID_RE.fullmatch(cid):
        raise ValueError(f"client_order_id {cid!r} breaks Alpaca's format or length limit")
    return cid


def _check_symbol(symbol: str) -> None:
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError(f"symbol {symbol!r} must be 1-6 upper-case letters")
