"""structlog setup: JSON lines to ``<logs_dir>/YYYY-MM-DD.jsonl`` plus a plain console stream.

Timestamps come from the injected clock, so replays log simulated time. Every line, including
records from third-party stdlib loggers, passes through secret redaction (CLAUDE.md invariant 6).
Logging is process-wide by nature: configure it once, at CLI entry.
"""

import logging
import re
import sys
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

import structlog
from pydantic import SecretStr
from structlog.typing import EventDict, Processor, WrappedLogger

from trader.core.clock import Clock, fmt_iso
from trader.core.redact import MIN_SECRET_LEN, redact_text

_HANDLER_PREFIX = "trader-"
# String values under these key names are hidden whatever they contain.
_SENSITIVE_KEY = re.compile(
    r"token|secret|password|passwd|authorization|api_?key|key_id|webhook|cookie", re.IGNORECASE
)
_MASK = "**********"
# Libraries whose INFO lines stay out of the console: httpx logs every request (about 25 a
# minute all day), which buries warnings and incidents. The JSON log keeps them, and an answer
# of 400 or more (a refused order, a failed call) still reaches the console.
_QUIET_LOGGERS = ("httpx", "httpcore")
_HTTPX_STATUS_ARG = 3  # 'HTTP Request: %s %s "%s %d %s"': method, url, version, status, reason


def _quiet_libraries(record: logging.LogRecord) -> bool:
    if record.levelno >= logging.WARNING or not record.name.startswith(_QUIET_LOGGERS):
        return True
    args = record.args
    status = args[_HTTPX_STATUS_ARG] if isinstance(args, tuple) and len(args) > 3 else None
    return isinstance(status, int) and status >= 400


class RedactSecrets:
    """structlog processor: scrub known secret values and anything under a sensitive key name."""

    def __init__(self, secrets: Mapping[str, str]) -> None:
        self._secrets = dict(secrets)

    def __call__(self, logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        return {key: self._clean(key, value) for key, value in event_dict.items()}

    def _clean(self, key: str | None, value: Any) -> Any:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, SecretStr):
            return _MASK
        if isinstance(value, Mapping):
            return {str(k): self._clean(str(k), v) for k, v in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [self._clean(None, v) for v in value]
        if isinstance(value, datetime | date | time):
            text = value.isoformat()
        elif isinstance(value, Decimal | Path):
            text = str(value)
        else:
            text = value if isinstance(value, str) else str(value)
        if key is not None and _SENSITIVE_KEY.search(key) and len(text) >= MIN_SECRET_LEN:
            return _MASK
        return redact_text(text, self._secrets)


def configure_logging(
    *,
    clock: Clock,
    secrets: Mapping[str, str],
    logs_dir: Path | None,
    console: TextIO | None = sys.stderr,
    level: int = logging.INFO,
) -> Path | None:
    """Route structlog and stdlib logging through one redacting pipeline.

    Returns the JSON log file, or None when ``logs_dir`` is None. Calling it again replaces the
    handlers it installed earlier.
    """

    def add_timestamp(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        event_dict["ts"] = fmt_iso(clock.now())
        return event_dict

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        add_timestamp,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    redact = RedactSecrets(secrets)

    def formatter(renderer: Processor) -> structlog.stdlib.ProcessorFormatter:
        return structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,  # tracebacks become text, then get redacted
                redact,
                renderer,
            ],
        )

    shutdown_logging()
    root = logging.getLogger()
    root.setLevel(level)
    path: Path | None = None
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / f"{clock.now():%Y-%m-%d}.jsonl"
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.set_name(f"{_HANDLER_PREFIX}json")
        file_handler.setFormatter(formatter(structlog.processors.JSONRenderer()))
        root.addHandler(file_handler)
    if console is not None:
        console_handler = logging.StreamHandler(console)
        console_handler.set_name(f"{_HANDLER_PREFIX}console")
        console_handler.addFilter(_quiet_libraries)
        # Plain tracebacks: rich's default formatter prints local variables, which can hold secrets.
        renderer = structlog.dev.ConsoleRenderer(
            colors=False, timestamp_key="ts", exception_formatter=structlog.dev.plain_traceback
        )
        console_handler.setFormatter(formatter(renderer))
        root.addHandler(console_handler)
    return path


def shutdown_logging() -> None:
    """Remove and close the handlers installed by ``configure_logging``."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if (handler.get_name() or "").startswith(_HANDLER_PREFIX):
            root.removeHandler(handler)
            handler.close()
