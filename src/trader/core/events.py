"""High-impact scheduled macro events from config/events.yaml (RISK.md R-021).

The human maintains the file and verifies every date (PLAN.md P1.8).
"""

import datetime as dt
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from trader.core.clock import et_datetime
from trader.core.models import ClockTime
from trader.core.yamlfile import load_yaml

EVENTS_FILE = Path("config/events.yaml")


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    date: dt.date
    time: ClockTime
    name: str = Field(min_length=1)
    impact: Literal["high", "medium"]
    source: str = Field(pattern=r"^https?://\S+$")
    verified_by_human: StrictBool = False

    def at(self) -> dt.datetime:
        """The event's instant, tz-aware ET."""
        return et_datetime(self.date, self.time)


class EventsFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    events: tuple[Event, ...] = ()

    def between(self, start: dt.datetime, end: dt.datetime) -> list[Event]:
        """Events with ``start <= at() < end``, in time order."""
        return sorted((e for e in self.events if start <= e.at() < end), key=Event.at)


def load_events(path: Path) -> EventsFile:
    """Parse and validate the events file. Raises yaml.YAMLError or pydantic.ValidationError."""
    return EventsFile.model_validate(load_yaml(path))
