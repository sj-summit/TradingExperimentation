"""Push notifications and the alert pump (P3.7; DECISIONS D-046), offline over respx."""

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from tests.helpers import FAKE_SECRETS, set_yaml, write_env
from trader.config import load_config
from trader.core.clock import ET, SimClock
from trader.memory.incidents import record_incident
from trader.memory.journal import Journal
from trader.notify import (
    DiscordNotifier,
    NtfyNotifier,
    NullNotifier,
    notifier_from_config,
    pump_alerts,
)

T0 = datetime(2026, 10, 13, 11, 0, tzinfo=ET)
NTFY = "https://ntfy.sh/s3cret-topic"
HOOK = "https://discord.com/api/webhooks/123/s3cret-token"


async def test_ntfy_posts_plain_text_with_its_headers() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(NTFY).mock(return_value=httpx.Response(200, json={"id": "abc"}))
        notifier = NtfyNotifier("https://ntfy.sh/", SecretStr("s3cret-topic"),
                                client=httpx.AsyncClient(), prefix="[SIM] ")  # fmt: skip
        ok = await notifier.send("P1 unprotected_position", "é" * 3_000, priority="urgent",
                                 tags=("rotating_light",))  # fmt: skip
    assert ok
    request = route.calls.last.request
    assert request.headers["X-Title"] == "[SIM] P1 unprotected_position"
    assert (request.headers["X-Priority"], request.headers["X-Tags"]) == ("5", "rotating_light")
    assert len(request.content) == 4_000  # cut under ntfy's 4,096-byte attachment threshold
    request.content.decode("utf-8")  # never split inside a character


async def test_ntfy_failures_never_raise() -> None:
    notifier = NtfyNotifier(
        "https://ntfy.sh", SecretStr("s3cret-topic"), client=httpx.AsyncClient()
    )
    with respx.mock as mock:
        mock.post(NTFY).mock(return_value=httpx.Response(429))
        assert await notifier.send("t", "m") is False
        mock.post(NTFY).mock(side_effect=httpx.ConnectError("down"))
        assert await notifier.send("t", "m") is False


async def test_discord_payload_and_429_retry() -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    with respx.mock as mock:
        route = mock.post(HOOK).mock(
            side_effect=[
                httpx.Response(429, json={"retry_after": 0.4, "global": False}),
                httpx.Response(204),
            ]
        )
        notifier = DiscordNotifier(SecretStr(HOOK), client=httpx.AsyncClient(), sleep=sleep)
        assert await notifier.send("P2 data_stale", "x" * 3_000)
    assert waits == [0.4]
    payload = route.calls.last.request.read()
    body = httpx.Response(200, content=payload).json()
    assert body["allowed_mentions"] == {"parse": []}
    assert body["content"].startswith("**P2 data_stale**\n")
    assert len(body["content"]) == 2_000


async def test_a_gone_discord_webhook_disables_the_provider() -> None:
    with respx.mock as mock:
        route = mock.post(HOOK).mock(return_value=httpx.Response(404))
        notifier = DiscordNotifier(SecretStr(HOOK), client=httpx.AsyncClient())
        assert await notifier.send("t", "m") is False
        assert notifier.disabled
        assert await notifier.send("t", "m") is False
    assert route.call_count == 1  # Discord asks not to keep using a 404ing webhook


def test_the_configured_provider_needs_its_secret(repo: Path) -> None:
    assert isinstance(notifier_from_config(load_config(repo)), NullNotifier)  # provider none
    set_yaml(repo / "config" / "settings.yaml", "notify.provider", "ntfy")
    assert isinstance(notifier_from_config(load_config(repo)), NullNotifier)  # no topic yet
    write_env(repo, {**FAKE_SECRETS, "CLAUDE_TOKEN_CREATED": "2026-10-01",
                     "NTFY_TOPIC": "s3cret-topic"})  # fmt: skip
    assert isinstance(notifier_from_config(load_config(repo)), NtfyNotifier)
    set_yaml(repo / "config" / "settings.yaml", "notify.provider", "discord")
    write_env(repo, {**FAKE_SECRETS, "CLAUDE_TOKEN_CREATED": "2026-10-01",
                     "DISCORD_WEBHOOK_URL": HOOK})  # fmt: skip
    assert isinstance(notifier_from_config(load_config(repo)), DiscordNotifier)


class Recorder:
    def __init__(self, ok: bool = True) -> None:
        self.ok, self.sent = ok, []  # type: ignore[var-annotated]

    async def send(self, title: str, message: str, *, priority: str = "default",
                   tags: Sequence[str] = ()) -> bool:  # fmt: skip
        self.sent.append((title, priority))
        return self.ok


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    yield opened
    await opened.aclose()


async def incident(journal: Journal, severity: str, code: str) -> None:
    await record_incident(journal, at=T0, severity=severity, component="execution.A",  # type: ignore[arg-type]
                          code=code, message=f"{code} happened")  # fmt: skip


async def test_the_pump_pushes_each_p1_and_p2_once(journal: Journal) -> None:
    await incident(journal, "P1", "unprotected_position")
    await incident(journal, "P2", "data_stale")
    await incident(journal, "P3", "scan_slow")  # report only
    clock = SimClock(T0)
    failing = Recorder(ok=False)
    assert await pump_alerts(journal, failing, clock) == (0, 2)  # left for the next pump
    working = Recorder()
    assert await pump_alerts(journal, working, clock) == (2, 0)
    assert working.sent == [("P1 unprotected_position", "urgent"), ("P2 data_stale", "high")]
    assert await pump_alerts(journal, working, clock) == (0, 0)
    stamped = await journal.fetch_all("SELECT code, alerted_at FROM incidents ORDER BY id")
    assert [(r["code"], r["alerted_at"] is not None) for r in stamped] == [
        ("unprotected_position", True), ("data_stale", True), ("scan_slow", False),
    ]  # fmt: skip
