"""Smoke tests for the offline network guard in root conftest."""

import asyncio
import errno
import socket

import anyio
import pytest


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_offline_guard_blocks_public_connect() -> None:
    """Offline tests must fail fast when attempting outbound non-loopback connections."""
    with pytest.raises(OSError) as sync_exc:
        socket.create_connection(("93.184.215.14", 80), timeout=1.0)
    assert sync_exc.value.errno == errno.ENETUNREACH

    with pytest.raises(OSError) as anyio_exc:
        await anyio.connect_tcp("93.184.215.14", 80)
    assert anyio_exc.value.errno == errno.ENETUNREACH

    with pytest.raises(OSError) as stream_exc:
        await asyncio.open_connection("93.184.215.14", 80)
    assert stream_exc.value.errno == errno.ENETUNREACH

    loop = asyncio.get_running_loop()
    with pytest.raises(OSError) as loop_exc:
        await loop.create_connection(asyncio.Protocol, "example.com", 80)
    assert loop_exc.value.errno == errno.ENETUNREACH
