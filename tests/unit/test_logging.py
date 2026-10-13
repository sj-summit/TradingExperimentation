import io
import json
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import structlog

from trader.core.clock import ET, SimClock
from trader.core.logging import configure_logging, shutdown_logging

CLOCK = SimClock(datetime(2026, 10, 9, 9, 35, 5, tzinfo=ET))


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    shutdown_logging()
    structlog.reset_defaults()


def read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_json_lines_carry_clock_time_and_context(tmp_path: Path) -> None:
    console = io.StringIO()
    path = configure_logging(clock=CLOCK, secrets={}, logs_dir=tmp_path, console=console)
    assert path == tmp_path / "2026-10-09.jsonl"
    structlog.get_logger("trader.test").info("armed", book="A", signal_id="20261009-XYZ-orb5")
    (line,) = read_lines(path)
    assert line["ts"] == "2026-10-09T09:35:05-04:00"
    assert line["event"] == "armed"
    assert line["level"] == "info"
    assert line["book"] == "A"
    assert line["logger"] == "trader.test"
    assert "armed" in console.getvalue()


def test_stdlib_records_share_the_pipeline(tmp_path: Path) -> None:
    path = configure_logging(clock=CLOCK, secrets={}, logs_dir=tmp_path, console=None)
    assert path is not None
    logging.getLogger("httpx").warning("GET %s", "https://example.invalid/v2/account")
    (line,) = read_lines(path)
    assert line["event"] == "GET https://example.invalid/v2/account"
    assert line["logger"] == "httpx"
    assert line["ts"] == "2026-10-09T09:35:05-04:00"


def test_the_console_skips_httpx_info_lines_the_file_keeps_them(tmp_path: Path) -> None:
    console = io.StringIO()
    path = configure_logging(clock=CLOCK, secrets={}, logs_dir=tmp_path, console=console)
    assert path is not None
    httpx = logging.getLogger("httpx")
    request = 'HTTP Request: %s %s "%s %d %s"'  # httpx's own format
    httpx.info(request, "GET", "https://example.invalid/v2/clock", "HTTP/1.1", 200, "OK")
    httpx.info(request, "POST", "https://example.invalid/v2/orders", "HTTP/1.1", 403, "Forbidden")
    httpx.warning("HTTP retry")
    structlog.get_logger("trader.x").info("kept")
    shown = console.getvalue()
    assert "v2/clock" not in shown  # about 25 a minute all day would bury the incidents
    assert '"HTTP/1.1 403 Forbidden"' in shown  # a refusal still shows
    assert "HTTP retry" in shown
    assert "kept" in shown
    assert [line["event"] for line in read_lines(path)] == [
        'HTTP Request: GET https://example.invalid/v2/clock "HTTP/1.1 200 OK"',
        'HTTP Request: POST https://example.invalid/v2/orders "HTTP/1.1 403 Forbidden"',
        "HTTP retry",
        "kept",
    ]


def test_reconfiguring_replaces_earlier_handlers(tmp_path: Path) -> None:
    configure_logging(clock=CLOCK, secrets={}, logs_dir=tmp_path, console=None)
    path = configure_logging(clock=CLOCK, secrets={}, logs_dir=tmp_path, console=None)
    assert path is not None
    structlog.get_logger().info("once")
    assert len(read_lines(path)) == 1


def test_exceptions_are_logged_as_text(tmp_path: Path) -> None:
    path = configure_logging(clock=CLOCK, secrets={}, logs_dir=tmp_path, console=None)
    assert path is not None
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        structlog.get_logger().exception("failed")
    (line,) = read_lines(path)
    assert "RuntimeError: boom" in line["exception"]
