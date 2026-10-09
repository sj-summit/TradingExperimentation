"""Shared pydantic field types. ARCHITECTURE.md §5 data contracts join as phases need them."""

import re
from datetime import time
from decimal import Decimal
from typing import Annotated

from pydantic import BeforeValidator

_TIME_RE = re.compile(r"\d{2}:\d{2}(:\d{2})?")


def _clock_time(value: object) -> object:
    if isinstance(value, time):
        return value
    if isinstance(value, str) and _TIME_RE.fullmatch(value):
        return time.fromisoformat(value)
    # YAML 1.1 reads an unquoted 15:30 as the integer 930, so only quoted strings are accepted.
    raise ValueError('must be a quoted "HH:MM" or "HH:MM:SS" string')


def _exact_decimal(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("must be a number")
    if isinstance(value, float):
        return Decimal(str(value))  # 0.3 -> Decimal("0.3"), not its binary expansion
    return value


ClockTime = Annotated[time, BeforeValidator(_clock_time)]
"""Wall-clock ET time of day, written as a quoted "HH:MM[:SS]" string."""

Dec = Annotated[Decimal, BeforeValidator(_exact_decimal)]
"""Decimal that converts YAML floats through their shortest repr, so 0.03 stays exactly 0.03."""
