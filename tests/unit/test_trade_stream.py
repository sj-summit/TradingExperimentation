import asyncio
import contextlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from tests.fixtures.fake_trading_ws import FakeTradingServer
from trader.core.clock import ET, SimClock
from trader.data.ws import _Websocket
from trader.execution.trade_stream import AlpacaTradeStream

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=ET)


class Recorder:
    def __init__(self) -> None:
        self.updates: list[Mapping[str, Any]] = []
        self.connects = 0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)


def make_stream(
    server: FakeTradingServer, rec: Recorder, reply_timeout_s: float = 1.0
) -> AlpacaTradeStream:
    def connected() -> None:
        rec.connects += 1

    return AlpacaTradeStream(
        url="wss://paper-api.alpaca.markets/stream",
        key_id="PKTEST",
        secret_key="secret-value",
        clock=SimClock(NOW),
        on_update=rec.updates.append,
        on_connected=connected,
        connector=server.connect,
        reply_timeout_s=reply_timeout_s,
        sleep=rec.sleep,
    )


async def until(condition: Any, rounds: int = 200) -> None:
    for _ in range(rounds):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def test_auth_listen_then_updates() -> None:
    server, rec = FakeTradingServer(), Recorder()
    stream = make_stream(server, rec)
    task = asyncio.create_task(stream.run())
    await asyncio.wait_for(server.listening.wait(), 1.0)
    auth, listen = server.latest.sent
    assert auth == {"action": "auth", "key": "PKTEST", "secret": "secret-value"}
    assert listen == {"action": "listen", "data": {"streams": ["trade_updates"]}}
    await until(lambda: rec.connects == 1)
    server.latest.push_update({"event": "new", "order": {"id": "o1"}})
    server.latest.push({"stream": "listening", "data": {"streams": ["trade_updates"]}})
    await until(lambda: len(rec.updates) == 1)
    assert rec.updates[0]["event"] == "new"
    await stream.stop()
    await asyncio.wait_for(task, 1.0)


async def test_rejected_keys_stop_for_good() -> None:
    server, rec = FakeTradingServer(authorized=False), Recorder()
    stream = make_stream(server, rec)
    await asyncio.wait_for(stream.run(), 1.0)
    assert stream.failed is not None
    assert "unauthorized" in str(stream.failed)
    assert (rec.connects, len(server.connections)) == (0, 1)


async def test_a_stream_not_granted_is_refused() -> None:
    server, rec = FakeTradingServer(grant=()), Recorder()
    stream = make_stream(server, rec)
    await asyncio.wait_for(stream.run(), 1.0)
    assert stream.failed is not None


async def test_every_connection_announces_itself_so_the_owner_resyncs() -> None:
    server, rec = FakeTradingServer(), Recorder()
    stream = make_stream(server, rec)
    task = asyncio.create_task(stream.run())
    await until(lambda: rec.connects == 1)
    server.latest.push({"action": "error", "data": {"error_message": "internal server error"}})
    await until(lambda: rec.connects == 2)  # reconnected after the server error
    assert rec.sleeps == [1.0]
    server.latest.drop()
    await until(lambda: rec.connects == 3)
    assert stream.connections == 3
    await stream.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 1.0)


async def test_silent_server_times_out_and_retries() -> None:
    server, rec = FakeTradingServer(), Recorder()
    server.respond = lambda conn, payload: None  # type: ignore[method-assign]
    stream = make_stream(server, rec, reply_timeout_s=0.01)
    task = asyncio.create_task(stream.run())
    for _ in range(500):  # real time: the reply timeout is a real 10 ms
        if len(server.connections) >= 2:
            break
        await asyncio.sleep(0.005)
    assert len(server.connections) >= 2
    assert stream.failed is None
    await stream.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class _BinaryWs:
    async def recv(self) -> bytes:
        return b'{"stream":"trade_updates","data":{}}'


async def test_binary_frames_are_decoded() -> None:
    # Paper sends trade updates in binary frames (Alpaca docs); the connector decodes them.
    assert await _Websocket(_BinaryWs()).recv() == '{"stream":"trade_updates","data":{}}'
