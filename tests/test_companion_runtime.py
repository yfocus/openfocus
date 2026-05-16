# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import importlib
import logging
import socket
from pathlib import Path


def _unused_local_addr() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()
    finally:
        sock.close()
    return f"{host}:{port}"


def _load_runtime(monkeypatch, state_path: Path):
    monkeypatch.setenv("OPENFOCUS_COMPANION_STATE", str(state_path))
    from openfocus.companion import runtime as rt

    return importlib.reload(rt)


def test_connect_once_reports_unavailable_without_marking_connected(
    monkeypatch, tmp_path
) -> None:
    rt = _load_runtime(monkeypatch, tmp_path / "companion_state.json")

    async def _run() -> None:
        stop = asyncio.Event()
        res = await asyncio.wait_for(
            rt._connect_once(_unused_local_addr(), stop), timeout=2.0
        )
        assert res.connected is False
        assert res.stable is False
        assert "UNAVAILABLE" in res.reason

    asyncio.run(_run())


def test_run_companion_backs_off_and_rate_limits_repeated_disconnects(
    monkeypatch, tmp_path, caplog
) -> None:
    rt = _load_runtime(monkeypatch, tmp_path / "companion_state.json")

    sleep_delays: list[float] = []

    async def fake_connect_once(addr: str, stop_event: asyncio.Event):
        return rt._ConnectOnceResult(
            connected=False,
            stable=False,
            reason="StatusCode.UNAVAILABLE",
            detail="connection refused",
        )

    async def fake_sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
        sleep_delays.append(seconds)
        if len(sleep_delays) >= 4:
            stop_event.set()

    monkeypatch.setattr(rt, "_connect_once", fake_connect_once)
    monkeypatch.setattr(rt, "_sleep_or_stop", fake_sleep_or_stop)
    caplog.set_level(logging.INFO, logger="openfocus.companion")

    asyncio.run(rt.run_companion(grpc_addr="127.0.0.1:1"))

    assert sleep_delays == [0.2, 0.4, 0.8, 1.6]
    assert (
        len(
            [
                r
                for r in caplog.records
                if "OpenFocus gRPC 连接结束，将重试" in r.getMessage()
            ]
        )
        == 1
    )
    assert (
        len([r for r in caplog.records if "连接 OpenFocus gRPC" in r.getMessage()]) == 1
    )


def test_run_companion_resets_backoff_after_stable_connection(
    monkeypatch, tmp_path
) -> None:
    rt = _load_runtime(monkeypatch, tmp_path / "companion_state.json")

    sleep_delays: list[float] = []
    results = [
        rt._ConnectOnceResult(
            connected=False,
            stable=False,
            reason="StatusCode.UNAVAILABLE",
            detail="connection refused",
        ),
        rt._ConnectOnceResult(
            connected=False,
            stable=False,
            reason="StatusCode.UNAVAILABLE",
            detail="connection refused",
        ),
        rt._ConnectOnceResult(
            connected=True,
            stable=True,
            reason="StatusCode.UNAVAILABLE",
            detail="server restarted",
        ),
    ]

    async def fake_connect_once(addr: str, stop_event: asyncio.Event):
        return results.pop(0)

    async def fake_sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
        sleep_delays.append(seconds)
        if not results:
            stop_event.set()

    monkeypatch.setattr(rt, "_connect_once", fake_connect_once)
    monkeypatch.setattr(rt, "_sleep_or_stop", fake_sleep_or_stop)

    asyncio.run(rt.run_companion(grpc_addr="127.0.0.1:1"))

    assert sleep_delays == [0.2, 0.4, 0.2]
