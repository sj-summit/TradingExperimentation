"""CLAUDE.md invariant 6: secret values never reach a log file or the console."""

import io
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
import structlog

from tests.helpers import FAKE_SECRETS
from trader.config import load_config
from trader.core.clock import ET, SimClock
from trader.core.logging import configure_logging, shutdown_logging


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    shutdown_logging()
    structlog.reset_defaults()


def test_no_secret_value_reaches_any_log_sink(repo: Path) -> None:
    cfg = load_config(repo)
    console = io.StringIO()
    path = configure_logging(
        clock=SimClock(datetime(2026, 10, 9, 9, 35, tzinfo=ET)),
        secrets=cfg.secrets.redaction_map(),
        logs_dir=repo / "runtime" / "logs",
        console=console,
    )
    assert path is not None
    token = FAKE_SECRETS["CLAUDE_CODE_OAUTH_TOKEN"]
    key = FAKE_SECRETS["ALPACA_PAPER_A_SECRET_KEY"]
    log = structlog.get_logger("trader.test")

    log.info("config loaded", config=cfg, secrets=cfg.secrets)
    log.info(f"calling claude with token {token}")
    log.info("request", headers={"APCA-API-SECRET-KEY": key, "Authorization": f"Bearer {token}"})
    log.info("nested", payload=[{"note": f"key={key}"}], raw=cfg.secrets.claude_code_oauth_token)
    try:
        raise RuntimeError(f"auth failed for {key}")
    except RuntimeError:
        log.exception("broker call failed")
    logging.getLogger("httpx").warning("GET https://paper-api.alpaca.markets/v2/account?k=%s", key)
    shutdown_logging()

    sinks = path.read_text(encoding="utf-8") + console.getvalue()
    for name, value in FAKE_SECRETS.items():
        assert value not in sinks, f"{name} leaked into the logs"
    assert "***CLAUDE_CODE_OAUTH_TOKEN***" in sinks  # scrubbed in place, not dropped
    assert "***ALPACA_PAPER_A_SECRET_KEY***" in sinks
