"""Tests are offline (CLAUDE.md invariant 9): pytest-socket blocks every non-loopback connect."""

import asyncio
import socket

import pytest
from pytest_socket import SocketConnectBlockedError


@pytest.mark.filterwarnings("ignore:A test tried to use socket:UserWarning")
def test_outbound_connect_is_blocked() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(SocketConnectBlockedError):
            sock.connect(("192.0.2.1", 443))  # TEST-NET-1, never routable
    finally:
        sock.close()


async def test_event_loop_still_works() -> None:
    # On Windows the Proactor loop needs a loopback socketpair; the guard must allow it.
    assert await asyncio.sleep(0, result=42) == 42
