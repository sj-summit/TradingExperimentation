from datetime import datetime, timedelta
from pathlib import Path

import pydantic
import pytest
import yaml

from tests.helpers import REPO
from trader.core.clock import ET
from trader.core.events import load_events

EVENTS = """\
events:
  - date: 2026-10-28
    time: "14:00"
    name: FOMC rate decision
    impact: high
    source: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
    verified_by_human: true
  - date: 2026-10-15
    time: "08:30"
    name: CPI
    impact: high
    source: https://www.bls.gov/schedule/
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "events.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_committed_events_file_is_valid() -> None:
    load_events(REPO / "config" / "events.yaml")


def test_between_filters_and_orders_by_time(tmp_path: Path) -> None:
    events = load_events(write(tmp_path, EVENTS))
    start = datetime(2026, 10, 9, tzinfo=ET)
    two_weeks = events.between(start, start + timedelta(days=14))
    assert [e.name for e in two_weeks] == ["CPI"]
    assert two_weeks[0].at() == datetime(2026, 10, 15, 8, 30, tzinfo=ET)
    month = events.between(start, start + timedelta(days=30))
    assert [e.name for e in month] == ["CPI", "FOMC rate decision"]
    assert month[0].verified_by_human is False


def test_unknown_event_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(pydantic.ValidationError):
        load_events(write(tmp_path, EVENTS + "    typo: 1\n"))


def test_duplicate_event_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(yaml.YAMLError, match="duplicate key"):
        load_events(write(tmp_path, EVENTS + '    time: "09:00"\n'))
