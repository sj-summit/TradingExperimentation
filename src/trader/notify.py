"""Push notifications (A§4.10, RISK.md §11; DECISIONS D-046): ``none``, ntfy or Discord.

- **ntfy:** ``POST {server}/{topic}`` with a plain-text body and ``X-Title``, ``X-Priority``
  (1-5) and ``X-Tags``. Bodies are cut to 4,000 bytes: ntfy turns anything over 4,096 into an
  attachment. The topic acts as the password (ntfy docs), so it is a secret.
- **Discord:** ``POST`` to the webhook URL (a secret: anyone holding it can post) with
  ``{"content": ≤2,000 chars, "allowed_mentions": {"parse": []}}``. Success is 204. A 429 waits
  ``retry_after`` once; a 404 means the webhook is gone, so the provider disables itself.

Messages never carry secrets or account numbers; a failed send is logged (never the URL or
topic) and never stops the day. ``pump_alerts`` pushes every P1/P2 incident once, stamping
``incidents.alerted_at``: incidents raised anywhere reach the human without coupling, and a crash
can't lose an alert (an unsent one is retried).
"""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Literal, Protocol

import httpx
import structlog
from pydantic import SecretStr

from trader.config import AppConfig
from trader.core.clock import Clock, fmt_iso
from trader.memory.journal import Journal

log = structlog.get_logger(__name__)

Priority = Literal["min", "low", "default", "high", "urgent"]
NTFY_PRIORITY: dict[Priority, int] = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
NTFY_MAX_BYTES = 4_000
DISCORD_MAX_CHARS = 2_000
TIMEOUT_S = 10.0
ALERT_PRIORITY: dict[str, Priority] = {"P1": "urgent", "P2": "high"}


class Notifier(Protocol):
    async def send(
        self, title: str, message: str, *, priority: Priority = "default", tags: Sequence[str] = ()
    ) -> bool:
        """True once the message is delivered (or there is nowhere to deliver it)."""
        ...


class NullNotifier:
    """Provider ``none``: nothing is sent, and every message counts as handled."""

    async def send(
        self, title: str, message: str, *, priority: Priority = "default", tags: Sequence[str] = ()
    ) -> bool:
        log.debug("notification not sent (provider none)", title=title)
        return True


class NtfyNotifier:
    def __init__(
        self,
        server: str,
        topic: SecretStr,
        *,
        client: httpx.AsyncClient | None = None,
        prefix: str = "",
    ) -> None:
        self._url = f"{server.rstrip('/')}/{topic.get_secret_value()}"
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT_S)
        self._prefix = prefix

    async def send(
        self, title: str, message: str, *, priority: Priority = "default", tags: Sequence[str] = ()
    ) -> bool:
        headers = {
            "X-Title": _ascii(self._prefix + title),
            "X-Priority": str(NTFY_PRIORITY[priority]),
        }
        if tags:
            headers["X-Tags"] = ",".join(tags)
        body = message.encode("utf-8")[:NTFY_MAX_BYTES].decode("utf-8", errors="ignore")
        try:
            response = await self._client.post(
                self._url, content=body.encode("utf-8"), headers=headers
            )
        except httpx.HTTPError as exc:
            log.warning("ntfy send failed", error=type(exc).__name__)
            return False
        if response.is_success:
            return True
        log.warning("ntfy refused the message", status=response.status_code)
        return False


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: SecretStr,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        prefix: str = "",
    ) -> None:
        self._url = webhook_url.get_secret_value()
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT_S)
        self._sleep, self._prefix = sleep, prefix
        self.disabled = False

    async def send(
        self, title: str, message: str, *, priority: Priority = "default", tags: Sequence[str] = ()
    ) -> bool:
        if self.disabled:
            return False
        content = f"**{self._prefix}{title}**\n{message}"[:DISCORD_MAX_CHARS]
        payload = {"content": content, "allowed_mentions": {"parse": []}}
        for attempt in range(2):
            try:
                response = await self._client.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                log.warning("discord send failed", error=type(exc).__name__)
                return False
            if response.is_success:
                return True
            if response.status_code == 404:
                self.disabled = True  # Discord: don't keep using a webhook that 404s
                log.error("discord webhook is gone; notifications disabled for this run")
                return False
            if response.status_code == 429 and attempt == 0:
                await self._sleep(_retry_after(response))
                continue
            log.warning("discord refused the message", status=response.status_code)
            return False
        return False


def notifier_from_config(cfg: AppConfig, *, prefix: str = "") -> Notifier:
    """The configured provider, or ``none`` when its secret is missing (with a warning)."""
    notify = cfg.settings.notify
    if notify.provider == "ntfy":
        topic = cfg.secrets.secret(notify.ntfy_topic_env)
        if topic is not None:
            return NtfyNotifier(notify.ntfy_server, topic, prefix=prefix)
        log.warning("notify.provider is ntfy but its topic is not set", env=notify.ntfy_topic_env)
    elif notify.provider == "discord":
        url = cfg.secrets.secret(notify.discord_webhook_env)
        if url is not None:
            return DiscordNotifier(url, prefix=prefix)
        log.warning(
            "notify.provider is discord but its webhook is not set", env=notify.discord_webhook_env
        )
    return NullNotifier()


async def pump_alerts(journal: Journal, notifier: Notifier, clock: Clock) -> tuple[int, int]:
    """Push every unalerted P1/P2 incident once; returns (sent, failed). A failed one stays
    unstamped and is tried again on the next pump."""
    rows = await journal.fetch_all(
        "SELECT id, ts, severity, component, code, message FROM incidents "
        "WHERE alerted_at IS NULL AND severity IN ('P1', 'P2') ORDER BY id"
    )
    sent = failed = 0
    for row in rows:
        ok = await notifier.send(
            f"{row['severity']} {row['code']}",
            f"{row['component']}: {row['message']} ({row['ts']})",
            priority=ALERT_PRIORITY[row["severity"]],
            tags=("warning",) if row["severity"] == "P2" else ("rotating_light",),
        )
        if ok:
            await journal.write(_stamper(int(row["id"]), clock.now()))
            sent += 1
        else:
            failed += 1
    return sent, failed


def _stamper(incident_id: int, at: datetime) -> Callable[[sqlite3.Connection], None]:
    def stamp(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE incidents SET alerted_at = ? WHERE id = ? AND alerted_at IS NULL",
            (fmt_iso(at), incident_id),
        )

    return stamp


def _retry_after(response: httpx.Response) -> float:
    try:
        value = float(response.json().get("retry_after", 0))
    except (ValueError, AttributeError):
        value = 0.0
    if not value:
        try:
            value = float(response.headers.get("Retry-After", "1"))
        except ValueError:
            value = 1.0
    return min(max(value, 0.0), 30.0)


def _ascii(text: str) -> str:
    """HTTP header values must be ASCII; ntfy titles are short labels."""
    return text.encode("ascii", errors="replace").decode("ascii")[:250]
