# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio

import pytest

from openfocus.companion.grpc import CompanionConnection, CompanionRegistry


def test_companion_connection_close_wakes_outgoing() -> None:
    async def _run() -> None:
        conn = CompanionConnection(companion_id=1, device_id="dev-test")
        yielded: list[object] = []

        async def consume() -> None:
            async for msg in conn.outgoing():
                yielded.append(msg)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        conn.close()
        await asyncio.wait_for(task, timeout=0.5)

        assert yielded == []

    asyncio.run(_run())


def test_companion_registry_close_all_closes_connections() -> None:
    async def _run() -> None:
        registry = CompanionRegistry()
        conn = CompanionConnection(companion_id=1, device_id="dev-test")
        await registry.set_connected(1, conn)

        outgoing = conn.outgoing()
        next_msg = asyncio.create_task(outgoing.__anext__())
        await asyncio.sleep(0)

        await registry.close_all()

        assert registry.get(1) is None
        assert conn._closed.is_set()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(next_msg, timeout=0.5)

    asyncio.run(_run())
