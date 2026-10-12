"""AlpacaTradeStream: one account's ``trade_updates`` WebSocket, reconnecting with backoff.

The protocol, from Alpaca's docs (DECISIONS D-030):
- Auth with ``{"action":"auth","key":…,"secret":…}``; the reply is
  ``{"stream":"authorization","data":{"status":"authorized"|"unauthorized",…}}``.
- Subscribe with ``{"action":"listen","data":{"streams":["trade_updates"]}}``; the ack is
  ``{"stream":"listening","data":{"streams":[…]}}`` and omits streams that aren't available.
- Events arrive as ``{"stream":"trade_updates","data":{…}}``. Paper sends them in binary frames,
  which the connector decodes.
- In-stream errors arrive as ``{"action":"error","data":{"error_message":…}}`` before a close.
- Missed events are never replayed, so ``on_connected`` runs after every connection, the first
  included, and the owner resyncs over REST.

The stream knows the wire protocol only; payloads go to ``on_update`` as decoded dicts.
"""

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from trader.core.clock import Clock
from trader.data.ws import BACKOFF_S, WsClosed, WsConnection, WsConnector, websockets_connector

log = structlog.get_logger(__name__)

TRADE_STREAM_PAPER_URL = "wss://paper-api.alpaca.markets/stream"
STREAM = "trade_updates"


class TradeStreamError(Exception):
    """The server refused us: bad keys, or the trade_updates stream isn't available."""


class AlpacaTradeStream:
    def __init__(
        self,
        *,
        url: str,
        key_id: str,
        secret_key: str,
        clock: Clock,
        on_update: Callable[[Mapping[str, Any]], None],
        on_connected: Callable[[], None],
        connector: WsConnector = websockets_connector,
        reply_timeout_s: float = 10.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url, self._key, self._secret = url, key_id, secret_key
        self._clock, self._on_update, self._on_connected = clock, on_update, on_connected
        self._connector, self._reply_timeout_s, self._sleep = connector, reply_timeout_s, sleep
        self._conn: WsConnection | None = None
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()
        self.connections = 0
        self.disconnected_since: datetime | None = clock.now()
        self.failed: TradeStreamError | None = None

    async def run(self) -> None:
        """Run until ``stop()`` or the server rejects the keys, reconnecting on any drop."""
        attempt = 0
        while not self._stop.is_set():
            conn: WsConnection | None = None
            try:
                conn = await self._connector(self._url)
                await self._handshake(conn)
                self._opened(conn)
                attempt = 0
                await self._read(conn)
            except TradeStreamError as exc:
                self.failed = exc
                log.error("trade stream refused", url=self._url, reason=str(exc))
                return
            except (WsClosed, TimeoutError, ValueError) as exc:
                if self._stop.is_set():  # our own close at the end of the day (D-039)
                    log.info("trade stream closed", url=self._url)
                else:
                    log.warning(
                        "trade stream disconnected", url=self._url, reason=str(exc) or "timeout"
                    )
            finally:
                await self._closed(conn)
            if self._stop.is_set():
                return
            await self._sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
            attempt += 1

    async def stop(self) -> None:
        self._stop.set()
        conn = self._conn
        if conn is not None:
            await conn.close()

    # ---------------------------------------------------------------- connection lifecycle

    async def _handshake(self, conn: WsConnection) -> None:
        await conn.send(json.dumps({"action": "auth", "key": self._key, "secret": self._secret}))
        reply = await self._reply(conn, "authorization")
        if reply.get("status") != "authorized":
            raise TradeStreamError(f"authorization {reply.get('status') or 'failed'}")
        await conn.send(json.dumps({"action": "listen", "data": {"streams": [STREAM]}}))
        ack = await self._reply(conn, "listening")
        if STREAM not in (ack.get("streams") or []):
            raise TradeStreamError("the server did not grant the trade_updates stream")

    async def _reply(self, conn: WsConnection, stream: str) -> Mapping[str, Any]:
        """The ``data`` of the next message on ``stream``, within the reply timeout."""

        async def wait() -> Mapping[str, Any]:
            while True:
                for message in await self._recv(conn):
                    self._raise_on_error(message)
                    if message.get("stream") == stream:
                        data = message.get("data")
                        return data if isinstance(data, Mapping) else {}

        return await asyncio.wait_for(wait(), self._reply_timeout_s)

    def _opened(self, conn: WsConnection) -> None:
        self._conn = conn
        self.connections += 1
        self.disconnected_since = None
        self.connected.set()
        log.info("trade stream connected", url=self._url, connections=self.connections)
        self._on_connected()

    async def _closed(self, conn: WsConnection | None) -> None:
        self._conn = None
        self.connected.clear()
        if self.disconnected_since is None:
            self.disconnected_since = self._clock.now()
        if conn is not None:
            with contextlib.suppress(Exception):  # already gone; nothing else to do
                await conn.close()

    async def _read(self, conn: WsConnection) -> None:
        while not self._stop.is_set():
            for message in await self._recv(conn):
                self._raise_on_error(message)
                if message.get("stream") == STREAM and isinstance(message.get("data"), Mapping):
                    self._on_update(message["data"])

    @staticmethod
    async def _recv(conn: WsConnection) -> list[dict[str, Any]]:
        decoded = json.loads(await conn.recv(), parse_float=Decimal)
        items = decoded if isinstance(decoded, list) else [decoded]
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _raise_on_error(message: Mapping[str, Any]) -> None:
        if message.get("action") == "error" or message.get("stream") == "error":
            data = message.get("data")
            detail = data.get("error_message") if isinstance(data, Mapping) else None
            raise WsClosed(f"server error: {detail or 'unknown'}")
