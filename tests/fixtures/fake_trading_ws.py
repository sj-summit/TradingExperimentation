"""A scripted stand-in for Alpaca's trade-updates WebSocket (no network).

It answers like Alpaca's docs say: ``auth`` gets an ``authorization`` reply, ``listen`` gets a
``listening`` ack, and events arrive as ``{"stream":"trade_updates","data":{…}}`` objects.
"""

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from trader.data.ws import WsClosed

_CLOSE = object()


class FakeTradingConnection:
    def __init__(self, server: "FakeTradingServer", url: str) -> None:
        self.server, self.url = server, url
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._inbox: asyncio.Queue[object] = asyncio.Queue()

    def push(self, message: Mapping[str, Any]) -> None:
        self._inbox.put_nowait(json.dumps(message))

    def push_update(self, data: Mapping[str, Any]) -> None:
        self.push({"stream": "trade_updates", "data": data})

    def drop(self) -> None:
        self._inbox.put_nowait(_CLOSE)

    async def send(self, message: str) -> None:
        if self.closed:
            raise WsClosed("send on a closed connection")
        payload = json.loads(message)
        self.sent.append(payload)
        self.server.respond(self, payload)

    async def recv(self) -> str:
        item = await self._inbox.get()
        if item is _CLOSE:
            self.closed = True
            raise WsClosed("server closed the connection")
        assert isinstance(item, str)
        return item

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._inbox.put_nowait(_CLOSE)


class FakeTradingServer:
    def __init__(self, *, authorized: bool = True, grant: tuple[str, ...] = ("trade_updates",)):
        self.authorized, self.grant = authorized, grant
        self.connections: list[FakeTradingConnection] = []
        self.listening = asyncio.Event()

    async def connect(self, url: str) -> FakeTradingConnection:
        conn = FakeTradingConnection(self, url)
        self.connections.append(conn)
        return conn

    @property
    def latest(self) -> FakeTradingConnection:
        return self.connections[-1]

    def respond(self, conn: FakeTradingConnection, payload: Mapping[str, Any]) -> None:
        action = payload.get("action")
        if action == "auth":
            status = "authorized" if self.authorized else "unauthorized"
            conn.push(
                {"stream": "authorization", "data": {"status": status, "action": "authenticate"}}
            )
        elif action == "listen":
            conn.push({"stream": "listening", "data": {"streams": list(self.grant)}})
            self.listening.set()
