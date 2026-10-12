"""One authenticated Alpaca WebSocket with reconnects and subscription replay (A§4.2, A§10).

Shared by the market-data hub and the news hub. The protocol, verified against Alpaca's docs
(DECISIONS D-027):
- The server greets with ``{"T":"success","msg":"connected"}``.
- The client must authenticate within 10 s.
- Every server message is a JSON array.
- A subscribe or unsubscribe is acknowledged with the full current set, and a 405 rejects the
  whole request.

Bad input never ends the stream (DECISIONS D-048): a frame that isn't JSON, or a message the
owner can't parse, is logged and skipped, and any other unexpected error reconnects.
"""

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

import structlog
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from trader.core.clock import Clock

log = structlog.get_logger(__name__)

STOCK_STREAM_URL = "wss://stream.data.alpaca.markets/v2/{feed}"  # feed: iex | sip | test
NEWS_STREAM_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
# Alpaca clears half-dead sessions only after ~90 s, so a quick reconnect can hit 406.
CONNECTION_LIMIT_WAIT_S = 30.0
# 402 auth failed, 409 insufficient subscription: retrying cannot help.
FATAL_CODES = frozenset({402, 409})
CONNECTION_LIMIT = 406
ACK_TIMEOUT = 408  # ours, not Alpaca's: no ack for a subscription change in time


class WsClosed(Exception):
    """The connection closed, from either side."""


class StreamError(Exception):
    """An error message from the server: ``[{"T":"error","code":405,"msg":"..."}]``."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{code} {message}")
        self.code, self.message = code, message


class WsConnection(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


WsConnector = Callable[[str], Awaitable[WsConnection]]


class _Websocket:
    """Adapts a ``websockets`` client connection to WsConnection."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send(self, message: str) -> None:
        try:
            await self._ws.send(message)
        except ConnectionClosed as exc:
            raise WsClosed(str(exc)) from None

    async def recv(self) -> str:
        try:
            data = await self._ws.recv()
        except ConnectionClosed as exc:
            raise WsClosed(str(exc)) from None
        return data if isinstance(data, str) else bytes(data).decode("utf-8")

    async def close(self) -> None:
        await self._ws.close()


async def websockets_connector(url: str) -> WsConnection:
    """Open a real connection; ``websockets`` answers server pings on its own."""
    try:
        ws = await connect(url, open_timeout=10, ping_interval=10, ping_timeout=30, max_size=2**22)
    except (OSError, TimeoutError, WebSocketException) as exc:
        raise WsClosed(f"connect failed: {type(exc).__name__}: {exc}") from None
    return _Websocket(ws)


class _ConnectionLimit(Exception):
    pass


class AlpacaStream:
    """Connect, authenticate, keep the desired subscriptions, and reconnect with backoff.

    Data messages go to ``on_data`` as decoded dicts (prices as Decimal). ``on_connected(first)``
    runs after each successful authentication, e.g. to backfill a gap. While
    ``should_be_busy()`` is true, ``silence_s`` without any message forces a reconnect, which
    catches sockets that stay open but stop delivering.
    """

    def __init__(
        self,
        *,
        url: str,
        key_id: str,
        secret_key: str,
        channels: Iterable[str],
        clock: Clock,
        on_data: Callable[[Mapping[str, Any]], None],
        on_connected: Callable[[bool], Awaitable[None]] | None = None,
        on_error: Callable[[StreamError], None] | None = None,
        connector: WsConnector = websockets_connector,
        should_be_busy: Callable[[], bool] = lambda: False,
        silence_s: float = 60.0,
        auth_timeout_s: float = 10.0,
        ack_timeout_s: float = 10.0,
        connection_limit_retries: int | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """``connection_limit_retries``: after this many consecutive 406s, stop with ``failed``
        set (None retries forever). ``on_error`` hears every server error that no request was
        waiting for, such as a 405 on the subscriptions replayed after a reconnect."""
        self._url, self._key, self._secret = url, key_id, secret_key
        self._limit_retries, self._limit_hits = connection_limit_retries, 0
        self._channels = tuple(channels)
        self._clock, self._on_data, self._on_connected = clock, on_data, on_connected
        self._on_error = on_error
        self._connector, self._should_be_busy = connector, should_be_busy
        self._silence_s, self._auth_timeout_s, self._ack_timeout_s = (
            silence_s,
            auth_timeout_s,
            ack_timeout_s,
        )
        self._sleep = sleep
        self._desired: dict[str, set[str]] = {}
        self._conn: WsConnection | None = None
        self._pending: asyncio.Future[dict[str, list[str]]] | None = None
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()
        self.connections = 0
        self.disconnected_since: datetime | None = clock.now()
        self.failed: StreamError | None = None
        self.acked: dict[str, list[str]] = {}

    # ---------------------------------------------------------------- public

    async def run(self) -> None:
        """Run until ``stop()`` or a fatal error (402, 409), reconnecting on any drop."""
        attempt, first = 0, True
        while not self._stop.is_set():
            wait = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            conn: WsConnection | None = None
            try:
                conn = await self._connector(self._url)
                await self._authenticate(conn)
                await self._opened(conn, first)
                first, attempt = False, 0
                await self._read(conn)
            except _ConnectionLimit:
                self._limit_hits += 1
                log.warning("stream connection limit", url=self._url, hits=self._limit_hits)
                if self._limit_retries is not None and self._limit_hits > self._limit_retries:
                    self.failed = StreamError(CONNECTION_LIMIT, "connection limit exceeded")
                    return
                wait = max(wait, CONNECTION_LIMIT_WAIT_S)
            except StreamError as exc:
                if exc.code in FATAL_CODES:
                    self.failed = exc
                    log.error(
                        "stream failed for good", url=self._url, code=exc.code, msg=exc.message
                    )
                    return
                log.warning("stream error", url=self._url, code=exc.code, msg=exc.message)
            except (WsClosed, TimeoutError) as exc:
                if self._stop.is_set():
                    log.info("stream closed", url=self._url)
                else:
                    log.warning("stream disconnected", url=self._url, reason=str(exc) or "timeout")
            except Exception:
                # Never let one surprise end market data for the day: reconnect instead.
                log.exception("stream failed unexpectedly; reconnecting", url=self._url)
            finally:
                await self._closed(conn)
            if self._stop.is_set():
                return
            attempt += 1
            await self._sleep(wait)

    async def stop(self) -> None:
        self._stop.set()
        conn = self._conn
        if conn is not None:
            await conn.close()

    async def update(self, desired: Mapping[str, Iterable[str]]) -> dict[str, list[str]]:
        """Make the server's subscriptions equal ``desired``; returns the acknowledged set.

        Unsubscribes go first, so the change never briefly exceeds the plan's limit. If the
        server rejects a request (e.g. 405) the StreamError propagates and the server keeps
        its previous set. While disconnected the change waits for the next connect. With no
        ack in time, the target is kept, the connection is dropped so the reconnect replays it,
        and StreamError(ACK_TIMEOUT) is raised.
        """
        target = {channel: set(symbols) for channel, symbols in desired.items()}
        async with self._lock:
            conn = self._conn
            if conn is None:
                self._desired = target
                return {}
            channels = set(self._desired) | set(target)
            remove = {c: self._desired.get(c, set()) - target.get(c, set()) for c in channels}
            add = {c: target.get(c, set()) - self._desired.get(c, set()) for c in channels}
            try:
                for action, change in (("unsubscribe", remove), ("subscribe", add)):
                    message = {channel: sorted(s) for channel, s in change.items() if s}
                    if message:
                        await self._request(conn, action, message)
            except WsClosed:
                self._desired = target  # replayed on reconnect
                return {}
            except TimeoutError:
                self._desired = target  # replayed on reconnect
                with contextlib.suppress(Exception):
                    await conn.close()
                raise StreamError(ACK_TIMEOUT, "no subscription ack in time") from None
            return self.acked

    # ---------------------------------------------------------------- connection lifecycle

    async def _authenticate(self, conn: WsConnection) -> None:
        greeting = await asyncio.wait_for(self._recv(conn), self._auth_timeout_s)
        self._raise_errors(greeting)
        await conn.send(json.dumps({"action": "auth", "key": self._key, "secret": self._secret}))
        while True:
            reply = await asyncio.wait_for(self._recv(conn), self._auth_timeout_s)
            self._raise_errors(reply)
            if any(m.get("T") == "success" and m.get("msg") == "authenticated" for m in reply):
                return

    async def _opened(self, conn: WsConnection, first: bool) -> None:
        self._conn = conn
        self.connections += 1
        self._limit_hits = 0
        replay = {channel: sorted(s) for channel, s in self._desired.items() if s}
        if replay:
            await conn.send(json.dumps({"action": "subscribe", **replay}))
        log.info("stream connected", url=self._url, connections=self.connections)
        if self._on_connected is not None:
            try:
                await self._on_connected(first)
            except Exception:
                log.exception("stream on_connected hook failed")
        self.disconnected_since = None
        self.connected.set()

    async def _closed(self, conn: WsConnection | None) -> None:
        self._conn = None
        self.connected.clear()
        if self.disconnected_since is None:
            self.disconnected_since = self._clock.now()
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(WsClosed("connection closed while waiting for an ack"))
        if conn is not None:
            with contextlib.suppress(Exception):  # already gone; nothing else to do
                await conn.close()

    async def _read(self, conn: WsConnection) -> None:
        while not self._stop.is_set():
            try:
                messages = await asyncio.wait_for(self._recv(conn), self._silence_s)
            except TimeoutError:
                if self._should_be_busy():
                    raise TimeoutError(f"no message for {self._silence_s:.0f} s") from None
                continue
            except ValueError as exc:  # not JSON: skip the frame, keep the connection
                log.warning("unreadable stream frame", url=self._url, error=str(exc)[:200])
                continue
            for message in messages:
                try:
                    self._dispatch(message)
                except StreamError:
                    raise
                except Exception as exc:
                    log.warning(
                        "unreadable stream message",
                        url=self._url,
                        error=f"{type(exc).__name__}: {exc}"[:200],
                        kind=str(message.get("T"))[:8],
                    )

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        kind = message.get("T")
        if kind == "subscription":
            self.acked = {
                channel: list(symbols)
                for channel, symbols in message.items()
                if channel in self._channels and isinstance(symbols, list)
            }
            self._desired = {channel: set(symbols) for channel, symbols in self.acked.items()}
            if self._pending is not None and not self._pending.done():
                self._pending.set_result(self.acked)
        elif kind == "error":
            error = StreamError(int(message.get("code", 0)), str(message.get("msg", "")))
            if self._pending is not None and not self._pending.done():
                self._pending.set_exception(error)
            elif error.code in FATAL_CODES:
                raise error
            else:
                log.warning(
                    "stream error message", url=self._url, code=error.code, msg=error.message
                )
                if self._on_error is not None:
                    self._on_error(error)
        elif kind == "success":
            return
        else:
            self._on_data(message)

    async def _request(self, conn: WsConnection, action: str, change: Mapping[str, Any]) -> None:
        self._pending = asyncio.get_running_loop().create_future()
        await conn.send(json.dumps({"action": action, **change}))
        await asyncio.wait_for(self._pending, self._ack_timeout_s)

    @staticmethod
    async def _recv(conn: WsConnection) -> list[dict[str, Any]]:
        decoded = json.loads(await conn.recv(), parse_float=Decimal)
        items = decoded if isinstance(decoded, list) else [decoded]
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _raise_errors(messages: list[dict[str, Any]]) -> None:
        for message in messages:
            if message.get("T") == "error":
                code = int(message.get("code", 0))
                if code == CONNECTION_LIMIT:
                    raise _ConnectionLimit()
                raise StreamError(code, str(message.get("msg", "")))
