# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from openfocus.infrastructure import streaming


def test_schedule_observed_task_records_background_failure(monkeypatch):
    failures: list[dict] = []

    def fake_audit_memory(**kwargs) -> None:
        failures.append(kwargs)

    monkeypatch.setattr(streaming.memory_service, "try_audit_memory", fake_audit_memory)

    async def _run() -> list[dict]:
        loop = asyncio.get_running_loop()
        unhandled: list[dict] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:

            async def fail() -> None:
                raise ValueError("boom")

            task = streaming.schedule_observed_task(
                fail(),
                task_name="unit.background",
                failure_context={"session_id": "s-1", "request_id": "r-1"},
            )

            assert task is not None
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert unhandled == []
            return failures
        finally:
            loop.set_exception_handler(previous_handler)

    observed = asyncio.run(_run())

    assert len(observed) == 1
    assert observed[0]["kind"] == "streaming.background_task.failure"
    assert observed[0]["source"] == "streaming:unit.background"
    assert observed[0]["metadata"] == {
        "task_name": "unit.background",
        "session_id": "s-1",
        "request_id": "r-1",
        "error_type": "ValueError",
    }
    assert "boom" in observed[0]["detail"]


def test_schedule_observed_task_swallows_audit_and_logging_failures(monkeypatch):
    def fail_audit_memory(**_kwargs) -> None:
        raise RuntimeError("audit unavailable")

    class FailingLogger:
        def warning(self, *_args, **_kwargs) -> None:
            raise RuntimeError("warning handler failed")

        def error(self, *_args, **_kwargs) -> None:
            raise RuntimeError("error handler failed")

    monkeypatch.setattr(streaming.memory_service, "try_audit_memory", fail_audit_memory)
    monkeypatch.setattr(streaming, "_LOG", FailingLogger())

    async def _run() -> list[dict]:
        loop = asyncio.get_running_loop()
        unhandled: list[dict] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:

            async def fail() -> None:
                raise ValueError("boom")

            task = streaming.schedule_observed_task(
                fail(),
                task_name="unit.background",
                failure_context={"session_id": "s-1", "request_id": "r-1"},
            )

            assert task is not None
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return unhandled
        finally:
            loop.set_exception_handler(previous_handler)

    assert asyncio.run(_run()) == []


def test_schedule_observed_task_does_not_raise_without_running_loop():
    async def noop() -> None:
        return None

    assert (
        streaming.schedule_observed_task(
            noop(),
            task_name="unit.no_loop",
            failure_context={"kind": "test"},
        )
        is None
    )


def test_install_listeners_schedule_observed_tasks(monkeypatch):
    registered: dict[str, object] = {}
    scheduled: list[tuple[str, dict]] = []

    def fake_schedule_observed_task(coro, *, task_name, failure_context):
        coro.close()
        scheduled.append((task_name, failure_context))
        return None

    monkeypatch.setattr(streaming, "_AGENT_LISTENER_INSTALLED", False)
    monkeypatch.setattr(streaming, "_TERM_LISTENER_INSTALLED", False)
    monkeypatch.setattr(streaming, "_RUNTIME_SIGNAL_LISTENER_INSTALLED", False)
    monkeypatch.setattr(
        streaming,
        "add_agent_chunk_listener",
        lambda listener: registered.setdefault("agent_chunk", listener),
    )
    monkeypatch.setattr(
        streaming,
        "add_terminal_output_listener",
        lambda listener: registered.setdefault("terminal_output", listener),
    )
    monkeypatch.setattr(
        streaming,
        "add_runtime_signal_listener",
        lambda listener: registered.setdefault("runtime_signal", listener),
    )
    monkeypatch.setattr(
        streaming, "schedule_observed_task", fake_schedule_observed_task
    )

    streaming.install_agent_chunk_listener_once()
    streaming.install_terminal_listener_once()
    streaming.install_runtime_signal_listener_once()

    registered["agent_chunk"](
        SimpleNamespace(session_id="session-1", request_id="request-1")
    )
    registered["terminal_output"](SimpleNamespace(terminal_id="terminal-1"))
    registered["runtime_signal"](
        SimpleNamespace(
            kind="runtime.turn.completed",
            raw_kind="Stop",
            session_id="session-2",
            turn_id="turn-1",
            task_public_id="task-1",
            terminal_id="terminal-2",
            companion_id=7,
            source="companion",
        )
    )

    assert scheduled == [
        (
            "agent_chunk",
            {"session_id": "session-1", "request_id": "request-1"},
        ),
        ("terminal_output", {"terminal_id": "terminal-1"}),
        (
            "runtime_signal",
            {
                "kind": "runtime.turn.completed",
                "raw_kind": "Stop",
                "session_id": "session-2",
                "turn_id": "turn-1",
                "task_public_id": "task-1",
                "terminal_id": "terminal-2",
                "companion_id": 7,
                "source": "companion",
            },
        ),
    ]
