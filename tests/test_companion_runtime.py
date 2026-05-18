# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import importlib
import json
import logging
from pathlib import Path

import pytest


def _unused_local_addr() -> str:
    return "127.0.0.1:1"


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


def test_hook_client_converts_hook_payload_to_runtime_signal(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("OPENFOCUS_INSTANCE_ID", "default")
    rt = _load_runtime(monkeypatch, tmp_path / "companion_state.json")

    payload = {
        "hook_kind": "turn-ended",
        "agent_runtime": "codex",
        "runtime": {
            "openfocus_session_id": "sess-1",
            "openfocus_task_id": "task-public-id",
            "openfocus_terminal_id": "term-1",
            "cwd": str(tmp_path),
            "tty": "ttys001",
            "ppid": "123",
        },
        "runtime_ts": "1770000000.5",
        "payload": {
            "turn_id": "turn-1",
            "summary": "done",
        },
    }

    class Reader:
        async def read(self, n: int) -> bytes:
            return json.dumps(payload).encode("utf-8")

    class Writer:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    async def _run() -> None:
        out_q = asyncio.Queue()
        writer = Writer()
        await rt._handle_hook_client(Reader(), writer, out_q)
        msg = await asyncio.wait_for(out_q.get(), timeout=1.0)
        assert writer.closed is True
        assert msg.WhichOneof("msg") == "runtime_signal"
        sig = msg.runtime_signal
        assert sig.raw_kind == "turn-ended"
        assert sig.agent_runtime == "codex"
        assert sig.session_id == "sess-1"
        assert sig.turn_id == "turn-1"
        assert sig.task_public_id == "task-public-id"
        assert sig.terminal_id == "term-1"
        assert sig.cwd == str(tmp_path)
        assert sig.ppid == 123
        assert sig.runtime_ts == 1770000000.5
        assert sig.source == "hook"
        assert json.loads(sig.payload_json)["summary"] == "done"

    asyncio.run(_run())


def test_hook_client_ignores_signals_from_other_openfocus_instance(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("OPENFOCUS_INSTANCE_ID", "dev")
    rt = _load_runtime(monkeypatch, tmp_path / "companion_state.json")

    payload = {
        "hook_kind": "user-prompt-submit",
        "agent_runtime": "codex",
        "runtime": {
            "openfocus_instance_id": "debug",
            "openfocus_task_id": "task-public-id",
            "openfocus_terminal_id": "term-1",
        },
        "payload": {"session_id": "sess-1"},
    }

    class Reader:
        async def read(self, n: int) -> bytes:
            return json.dumps(payload).encode("utf-8")

    class Writer:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    async def _run() -> None:
        out_q = asyncio.Queue()
        writer = Writer()
        await rt._handle_hook_client(Reader(), writer, out_q)
        assert writer.closed is True
        assert out_q.empty()

    asyncio.run(_run())


def test_hook_spool_drain_converts_spooled_payload_to_runtime_signal(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("OPENFOCUS_INSTANCE_ID", "default")
    monkeypatch.setenv("OPENFOCUS_HOOK_SPOOL_DIR", str(tmp_path / "spool"))
    rt = _load_runtime(monkeypatch, tmp_path / "companion_state.json")
    spool = tmp_path / "spool"
    spool.mkdir()
    payload_path = spool / "signal.json"
    payload_path.write_text(
        json.dumps(
            {
                "hook_kind": "user-prompt-submit",
                "agent_runtime": "codex",
                "runtime": {
                    "openfocus_instance_id": "default",
                    "openfocus_session_id": "sess-spool",
                    "openfocus_task_id": "task-public-id",
                    "openfocus_terminal_id": "term-spool",
                    "cwd": str(tmp_path),
                },
                "payload": {"turn_id": "turn-spool", "prompt": "hello"},
            }
        ),
        encoding="utf-8",
    )

    async def _run() -> None:
        out_q = asyncio.Queue()
        assert await rt._drain_hook_spool_once(out_q) == 1
        assert not payload_path.exists()
        msg = await asyncio.wait_for(out_q.get(), timeout=1.0)
        sig = msg.runtime_signal
        assert sig.raw_kind == "user-prompt-submit"
        assert sig.agent_runtime == "codex"
        assert sig.session_id == "sess-spool"
        assert sig.turn_id == "turn-spool"
        assert sig.task_public_id == "task-public-id"
        assert sig.terminal_id == "term-spool"

    asyncio.run(_run())


def test_float_ball_backend_env_is_allowlisted(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENFOCUS_SYSTEM_FLOAT_BALL_BACKEND", "test")
    rt = _load_runtime(monkeypatch, tmp_path / "companion_state.json")
    assert rt._float_ball_backend() == "test"
    assert "system_float_ball.test" in rt._capabilities()

    monkeypatch.setenv("OPENFOCUS_SYSTEM_FLOAT_BALL_BACKEND", "shell")
    assert rt._float_ball_backend() == "unsupported"
    assert all(not c.startswith("system_float_ball") for c in rt._capabilities())

    monkeypatch.setenv("OPENFOCUS_SYSTEM_FLOAT_BALL_BACKEND", "swift")
    assert rt._float_ball_backend() == "swift"
    assert "system_float_ball.swift" in rt._capabilities()


def test_float_ball_manager_reports_helper_startup_exit(monkeypatch, tmp_path) -> None:
    rt = _load_runtime(monkeypatch, tmp_path / "companion_state.json")
    rt.RUNTIME.auth_token = "tok_test"

    class FakeStderr:
        def read(self) -> bytes:
            return b"No module named _tkinter"

    class FakeProc:
        returncode = 2
        stderr = FakeStderr()

        def poll(self) -> int:
            return self.returncode

    def fake_popen(*args, **kwargs):
        return FakeProc()

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(rt, "_float_ball_backend", lambda: "tk")
    monkeypatch.setattr(rt.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rt.asyncio, "sleep", fake_sleep)

    async def _run() -> None:
        mgr = rt._FloatBallManager()
        with pytest.raises(RuntimeError, match="helper exited during startup"):
            await mgr.start(
                browser_session_id="browser-session-id-12345",
                openfocus_base_url="http://127.0.0.1:8001",
                summary_json="{}",
            )
        assert mgr._sessions == {}

    asyncio.run(_run())


def test_float_ball_manager_waits_for_helper_ready_file(monkeypatch, tmp_path) -> None:
    rt = _load_runtime(monkeypatch, tmp_path / "companion_state.json")
    rt.RUNTIME.auth_token = "tok_test"

    captured: dict = {}

    class FakeProc:
        stderr = None

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            captured["terminated"] = True

    def fake_popen(*args, **kwargs):
        env = kwargs["env"]
        captured["env"] = env
        ready_path = Path(env["OPENFOCUS_FLOAT_BALL_READY_FILE"])
        ready_path.write_text("ready\n", encoding="utf-8")
        return FakeProc()

    monkeypatch.setattr(rt, "_float_ball_backend", lambda: "tk")
    monkeypatch.setattr(rt, "_float_ball_helper_python", lambda: "python")
    monkeypatch.setattr(rt.subprocess, "Popen", fake_popen)

    async def _run() -> None:
        mgr = rt._FloatBallManager()
        backend = await mgr.start(
            browser_session_id="browser-session-id-12345",
            openfocus_base_url="http://127.0.0.1:8001",
            summary_json='{"counts":{"running":1,"waiting":2}}',
        )
        assert backend == "tk"
        assert "browser-session-id-12345" in mgr._sessions
        ready_path = Path(captured["env"]["OPENFOCUS_FLOAT_BALL_READY_FILE"])
        assert not ready_path.exists()
        assert captured["env"]["OPENFOCUS_FLOAT_BALL_BACKEND"] == "tk"

    asyncio.run(_run())
