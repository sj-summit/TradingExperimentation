"""A scripted stand-in for Alpaca's WebSocket servers (no network).

It answers like Alpaca: greets with ``connected``, authenticates, and acknowledges every
subscribe or unsubscribe with the full current set, or rejects it whole with 405.
"""

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Any

from trader.data.ws import WsClosed

_CLOSE = object()


class FakeConnection:
    def __init__(self, server: "FakeAlpacaServer", url: str) -> None:
        self.server, self.url = server, url
        self.sent: list[dict[str, Any]] = []
        self.subscriptions: dict[str, set[str]] = {}
        self.closed = False
        self._inbox: asyncio.Queue[object] = asyncio.Queue()

    def push(self, *messages: Mapping[str, Any]) -> None:
        """Deliver one server frame: a JSON array of messages."""
        self._inbox.put_nowait(json.dumps(list(messages)))

    def drop(self) -> None:
        """The server goes away."""
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


class FakeAlpacaServer:
    def __init__(
        self,
        *,
        auth_errors: list[tuple[int, str]] | None = None,
        symbol_limit: int = 30,
        after_subscribe: Callable[[FakeConnection], None] | None = None,
    ) -> None:
        self.connections: list[FakeConnection] = []
        self.auth_errors = list(auth_errors or [])  # consumed one per auth attempt
        self.symbol_limit = symbol_limit
        self.after_subscribe = after_subscribe

    async def connect(self, url: str) -> FakeConnection:
        conn = FakeConnection(self, url)
        self.connections.append(conn)
        conn.push({"T": "success", "msg": "connected"})
        return conn

    def respond(self, conn: FakeConnection, payload: Mapping[str, Any]) -> None:
        action = payload.get("action")
        if action == "auth":
            if self.auth_errors:
                code, msg = self.auth_errors.pop(0)
                conn.push({"T": "error", "code": code, "msg": msg})
            else:
                conn.push({"T": "success", "msg": "authenticated"})
            return
        if action not in ("subscribe", "unsubscribe"):
            conn.push({"T": "error", "code": 400, "msg": "invalid syntax"})
            return
        proposed = {channel: set(symbols) for channel, symbols in conn.subscriptions.items()}
        for channel, symbols in payload.items():
            if channel == "action":
                continue
            current = proposed.setdefault(channel, set())
            if action == "subscribe":
                current.update(symbols)
            else:
                current.difference_update(symbols)
        if any(len(symbols) > self.symbol_limit for symbols in proposed.values()):
            conn.push({"T": "error", "code": 405, "msg": "symbol limit exceeded"})
            return
        conn.subscriptions = proposed
        ack = {channel: sorted(symbols) for channel, symbols in proposed.items()}
        if "trades" in ack:
            ack["corrections"] = ack["trades"]  # Alpaca adds these with trades
        conn.push({"T": "subscription", **ack})
        if action == "subscribe" and self.after_subscribe is not None:
            self.after_subscribe(conn)
