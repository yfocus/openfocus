# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import os
from contextlib import contextmanager
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient


def test_agent_space_ttyd_bridge_supports_command_click_link_messages():
    from openfocus.web.routes.agent_spaces import _ttyd_bridge_script

    script = _ttyd_bridge_script()

    assert (
        "document.addEventListener('pointerdown', onCommandOpenEvent, true)" in script
    )
    assert "document.addEventListener('mousedown', onCommandOpenEvent, true)" in script
    assert "document.addEventListener('click', onCommandOpenEvent, true)" in script
    assert "event.metaKey || event.ctrlKey" in script
    assert "openfocus:terminal-link-open" in script
    assert "postMessage(payload, window.location.origin)" in script
    assert "closest('a[href]')" in script
    assert "xterm-rows" in script
    assert "xterm-accessibility-tree" in script
    assert "path: target.path" in script
    assert "candidateTokens(line)" in script
    assert "caretPositionFromPoint" in script
    assert "registerLinkProvider" in script
    assert "file:\\/\\/" in script
    assert "value[0] === '@'" in script
    assert "openfocus:terminal-font-size" in script
    assert "applyTerminalFontSize" in script


def test_agent_space_ttyd_bridge_applies_font_size_through_xterm_options():
    from openfocus.web.routes.agent_spaces import _ttyd_bridge_script

    script = _ttyd_bridge_script()

    assert "term.options.fontSize = size" in script
    assert "scheduleTerminalFontSizeApply" in script
    assert "font-size: ' + size" not in script
    assert ".xterm-rows, .xterm-screen" not in script


def test_agent_space_ttyd_bridge_injection_is_html_only_and_idempotent():
    from openfocus.web.routes.agent_spaces import _maybe_inject_ttyd_bridge

    html = b"<html><head></head><body>ok</body></html>"
    injected = _maybe_inject_ttyd_bridge(html, "text/html; charset=utf-8")

    assert b"__openfocusTtydBridgeInstalled" in injected
    assert injected.count(b"__openfocusTtydBridgeInstalled") == 2
    assert _maybe_inject_ttyd_bridge(injected, "text/html") == injected
    assert _maybe_inject_ttyd_bridge(html, "application/json") == html


def test_agent_space_terminals_list_reconciles_stale_records(tmp_path):
    async def _run() -> None:
        from openfocus.app import COMPANION_GRPC, app
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import terminals as terminal_service
        from openfocus.models import (
            AgentSpace,
            Companion,
            Goal,
            RemoteTerminalOutput,
            RemoteTerminalSession,
            Task,
        )

        with session_scope() as s:
            companion = Companion(
                device_id="reconcile-device",
                name="reconcile",
                base_url="grpc://",
                status="active",
                auth_token="token",
            )
            s.add(companion)
            s.flush()
            companion_id = int(companion.id)

            goal = Goal(title="g", content="d", due_date=dt.date.today())
            s.add(goal)
            s.flush()
            task = Task(goal_id=goal.id, title="t", content="d", status="todo")
            s.add(task)
            s.flush()
            space = AgentSpace(
                task_public_id=str(task.public_id),
                companion_id=companion_id,
                root_path=str(tmp_path),
            )
            s.add(space)
            s.flush()
            space_id = int(space.id)
            owner = terminal_service.owner_for_agent_space(space_id)
            for tid in ("live-term", "stale-term"):
                terminal_service.create_terminal_record(
                    s,
                    owner=owner,
                    task_public_id=str(task.public_id),
                    companion_id=companion_id,
                    root_path=str(tmp_path),
                    terminal_id=tid,
                    backend="ttyd",
                    connect_url="http://127.0.0.1:7681",
                )
                s.add(
                    RemoteTerminalOutput(
                        space_id=owner.db_space_id,
                        terminal_id=tid,
                        data_b64=base64.b64encode(b"out").decode("ascii"),
                        nbytes=3,
                    )
                )

        class FakeConn:
            async def request_terminal_list_sessions(self, **_kwargs):
                return SimpleNamespace(
                    sessions=[
                        SimpleNamespace(
                            terminal_id="live-term",
                            root_path=str(tmp_path),
                            created_at=0.0,
                        )
                    ]
                )

            def close(self):
                pass

        conn = FakeConn()
        await COMPANION_GRPC.registry.set_connected(companion_id, conn)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                r = await client.get(f"/api/agent_spaces/{space_id}/terminals")
                assert r.status_code == 200
                data = r.json()
                assert data["companion"]["online"] is True
                assert [t["terminal_id"] for t in data["terminals"]] == ["live-term"]

            with session_scope() as s:
                assert (
                    s.query(RemoteTerminalSession)
                    .filter(RemoteTerminalSession.terminal_id == "stale-term")
                    .one_or_none()
                    is None
                )
                assert (
                    s.query(RemoteTerminalOutput)
                    .filter(RemoteTerminalOutput.terminal_id == "stale-term")
                    .count()
                    == 0
                )
        finally:
            await COMPANION_GRPC.registry.set_disconnected(companion_id, conn)

    asyncio.run(_run())


def test_agent_space_terminal_inject_cleans_stale_runtime_record(tmp_path):
    async def _run() -> None:
        from openfocus.app import COMPANION_GRPC, app
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import terminals as terminal_service
        from openfocus.models import (
            AgentSpace,
            Companion,
            Goal,
            RemoteTerminalOutput,
            RemoteTerminalSession,
            Task,
        )

        with session_scope() as s:
            companion = Companion(
                device_id="stale-inject-device",
                name="stale-inject",
                base_url="grpc://",
                status="active",
                auth_token="token",
            )
            s.add(companion)
            s.flush()
            companion_id = int(companion.id)

            goal = Goal(title="g", content="d", due_date=dt.date.today())
            s.add(goal)
            s.flush()
            task = Task(goal_id=goal.id, title="t", content="d", status="todo")
            s.add(task)
            s.flush()
            space = AgentSpace(
                task_public_id=str(task.public_id),
                companion_id=companion_id,
                root_path=str(tmp_path),
            )
            s.add(space)
            s.flush()
            space_id = int(space.id)
            owner = terminal_service.owner_for_agent_space(space_id)
            terminal_service.create_terminal_record(
                s,
                owner=owner,
                task_public_id=str(task.public_id),
                companion_id=companion_id,
                root_path=str(tmp_path),
                terminal_id="stale-inject",
                backend="ttyd",
                connect_url="http://127.0.0.1:7681",
            )
            s.add(
                RemoteTerminalOutput(
                    space_id=owner.db_space_id,
                    terminal_id="stale-inject",
                    data_b64=base64.b64encode(b"out").decode("ascii"),
                    nbytes=3,
                )
            )

        class FakeConn:
            async def request_terminal_input(self, **_kwargs):
                raise RuntimeError("terminal not found")

            def close(self):
                pass

        conn = FakeConn()
        await COMPANION_GRPC.registry.set_connected(companion_id, conn)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                r = await client.post(
                    f"/api/agent_spaces/{space_id}/terminals/stale-inject/inject",
                    json={"text": "pwd\n"},
                )
                assert r.status_code == 410
                assert r.json()["detail"] == "terminal runtime not found"

            with session_scope() as s:
                assert (
                    s.query(RemoteTerminalSession)
                    .filter(RemoteTerminalSession.terminal_id == "stale-inject")
                    .one_or_none()
                    is None
                )
                assert (
                    s.query(RemoteTerminalOutput)
                    .filter(RemoteTerminalOutput.terminal_id == "stale-inject")
                    .count()
                    == 0
                )
        finally:
            await COMPANION_GRPC.registry.set_disconnected(companion_id, conn)

    asyncio.run(_run())


async def _wait_until_companion_ready(
    client: AsyncClient, *, timeout_s: float = 2.0
) -> dict:
    from openfocus.app import COMPANION_GRPC

    deadline = asyncio.get_running_loop().time() + float(timeout_s)
    last = None
    while asyncio.get_running_loop().time() < deadline:
        r = await client.get("/api/companions")
        assert r.status_code == 200
        items = r.json().get("items") or []
        if items:
            comp = items[0]
            cid = int(comp.get("id") or 0)
            if cid and COMPANION_GRPC.registry.get(cid) is not None:
                return comp
        last = items
        await asyncio.sleep(0.02)
    raise AssertionError(f"companion not ready within {timeout_s}s, last={last}")


def test_remote_terminal_create_input_output_and_close_via_grpc(tmp_path):
    async def _run() -> None:
        os.environ["OPENFOCUS_DB_PATH"] = str(tmp_path / "openfocus_test.db")
        os.environ["OPENFOCUS_MEMORY_DIR"] = str(tmp_path / "memory")
        os.environ["OPENFOCUS_COMPANION_STATE"] = str(tmp_path / "companion_state.json")
        os.environ["OPENFOCUS_TEST_PAIRING_CODE"] = "A1B2C3D4E5"
        os.environ["OPENFOCUS_TEST_TERMINAL_ECHO"] = "1"

        os.environ["OPENFOCUS_GRPC_AUTOSTART"] = "0"
        os.environ["OPENFOCUS_GRPC_PORT"] = "0"

        from openfocus.app import (
            COMPANION_GRPC,
            _term_subscribe,
            _term_unsubscribe,
            app,
        )
        from openfocus.companion import run_companion
        from openfocus.db import get_engine, reset_engine, session_scope
        from openfocus.models import Base, Goal, RemoteTerminalSession, Task

        reset_engine()
        Base.metadata.create_all(bind=get_engine())

        ws = tmp_path / "ws"
        ws.mkdir()

        await COMPANION_GRPC.start()
        assert COMPANION_GRPC.bound_addr

        stop = asyncio.Event()
        comp_task = asyncio.create_task(
            run_companion(grpc_addr=COMPANION_GRPC.bound_addr, stop_event=stop)
        )
        try:
            with session_scope() as s:
                g = Goal(title="g", content="d", due_date=dt.date.today())
                s.add(g)
                s.flush()
                t = Task(goal_id=g.id, title="t", content="d", status="todo")
                s.add(t)
                s.flush()
                task_pid = t.public_id

            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                comp = await _wait_until_companion_ready(client)
                cid = comp["id"]

                r = await client.post(f"/api/companions/{cid}/pairing_code")
                assert r.status_code == 200
                r = await client.post(
                    f"/api/companions/{cid}/pair", json={"code": "A1B2C3D4E5"}
                )
                assert r.status_code == 200

                r = await client.post(
                    f"/api/tasks/{task_pid}/agent_space",
                    json={"companion_id": cid, "root_path": str(ws)},
                )
                assert r.status_code == 200
                space_id = int(r.json()["space_id"])

                r = await client.post(f"/api/agent_spaces/{space_id}/terminals/new")
                assert r.status_code == 200
                tid = r.json()["terminal"]["terminal_id"]
                assert tid
                name = r.json()["terminal"]["name"]
                assert name

                with session_scope() as s:
                    row = (
                        s.query(RemoteTerminalSession)
                        .filter(RemoteTerminalSession.terminal_id == tid)
                        .one()
                    )
                    assert row.owner_type == "agent_space"
                    assert row.owner_id == space_id
                    assert row.space_id == space_id
                    assert row.task_public_id == task_pid

                r = await client.get(f"/api/agent_spaces/{space_id}/terminals")
                assert r.status_code == 200
                tids = [t["terminal_id"] for t in (r.json().get("terminals") or [])]
                assert tid in tids
                names = {t.get("name") for t in (r.json().get("terminals") or [])}
                assert name in names

                # rename should be unique within the same space
                r = await client.post(
                    f"/api/agent_spaces/{space_id}/terminals/{tid}/rename",
                    json={"name": "work"},
                )
                assert r.status_code == 200
                assert r.json()["terminal"]["name"] == "work"

                r2 = await client.post(f"/api/agent_spaces/{space_id}/terminals/new")
                assert r2.status_code == 200
                tid2 = r2.json()["terminal"]["terminal_id"]
                assert tid2 and tid2 != tid

                r = await client.post(
                    f"/api/agent_spaces/{space_id}/terminals/{tid2}/rename",
                    json={"name": "work"},
                )
                assert r.status_code == 400

                # Subscribe to output hub and send input through gRPC; echo mode should publish the same bytes.
                q = await _term_subscribe(tid)
                try:
                    conn = COMPANION_GRPC.registry.get(int(cid))
                    assert conn is not None
                    live = await conn.request_terminal_list_sessions(
                        timeout_seconds=5.0
                    )
                    assert tid in [session.terminal_id for session in live.sessions]
                    # Send >256KB but <1MB to ensure history isn't truncated at 256KB.
                    blob = (b"a" * (320 * 1024)) + b"\n"
                    await conn.request_terminal_input(
                        terminal_id=tid, data=blob, timeout_seconds=5.0
                    )
                    ev = await asyncio.wait_for(q.get(), timeout=2.0)
                    assert ev.get("type") == "output"
                    assert ev.get("terminal_id") == tid
                    data = base64.b64decode(ev.get("data_b64") or "")
                    assert blob[:1024] in data

                    # history should include the echoed output
                    r = await client.get(
                        f"/api/agent_spaces/{space_id}/terminals/{tid}/history",
                        params={"max_bytes": 1024 * 1024},
                    )
                    assert r.status_code == 200
                    hist_b = base64.b64decode(r.json().get("data_b64") or "")
                    assert blob[:1024] in hist_b
                    assert r.json().get("truncated") is False

                    r = await client.post(
                        f"/api/agent_spaces/{space_id}/terminals/{tid}/mouse_mode",
                        json={"enabled": False},
                    )
                    assert r.status_code == 200
                    assert r.json()["enabled"] is False

                    r = await client.post(
                        f"/api/agent_spaces/{space_id}/terminals/{tid}/mouse_mode",
                        json={"enabled": True},
                    )
                    assert r.status_code == 200
                    assert r.json()["enabled"] is True

                    await client.post(
                        f"/api/agent_spaces/{space_id}/terminals/{tid}/close"
                    )

                    r = await client.get(f"/api/agent_spaces/{space_id}/terminals")
                    assert r.status_code == 200
                    tids3 = [
                        t["terminal_id"] for t in (r.json().get("terminals") or [])
                    ]
                    assert tid not in tids3

                    closed = None
                    for _ in range(50):
                        ev2 = await asyncio.wait_for(q.get(), timeout=2.0)
                        if ev2.get("terminal_id") == tid and bool(ev2.get("closed")):
                            closed = ev2
                            break
                    assert closed is not None
                finally:
                    await _term_unsubscribe(tid, q)

                # releasing space should delete terminals records
                r = await client.delete(f"/api/tasks/{task_pid}/agent_space")
                assert r.status_code == 200

            audit_files = list((tmp_path / "memory" / "audit").glob("**/*.md"))
            assert audit_files
            audit_text = "\n".join(p.read_text(encoding="utf-8") for p in audit_files)
            assert "terminal.output" in audit_text
        finally:
            stop.set()
            await asyncio.wait_for(comp_task, timeout=5.0)
            await COMPANION_GRPC.stop()

    asyncio.run(_run())


def test_agent_space_release_keeps_terminal_records_if_local_delete_fails(
    tmp_path, monkeypatch
):
    async def _run() -> None:
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import terminals as terminal_service
        from openfocus.domains.agent_spaces import workspace as agent_space_workspace
        from openfocus.models import AgentSpace, Goal, RemoteTerminalSession, Task
        from openfocus.web.routes import agent_spaces as agent_spaces_routes

        with session_scope() as s:
            goal = Goal(title="g", content="d", due_date=dt.date.today())
            s.add(goal)
            s.flush()
            task = Task(goal_id=goal.id, title="t", content="d", status="todo")
            s.add(task)
            s.flush()
            task_pid = str(task.public_id)
            space = AgentSpace(task_public_id=task_pid, root_path=str(tmp_path))
            s.add(space)
            s.flush()
            space_id = int(space.id)
            terminal_service.create_terminal_record(
                s,
                owner=terminal_service.owner_for_agent_space(space_id),
                task_public_id=task_pid,
                companion_id=None,
                root_path=str(tmp_path),
                terminal_id="survives-local-failure",
                backend="ttyd",
                connect_url="http://127.0.0.1:7681",
            )

        real_session_scope = agent_space_workspace.session_scope
        calls = {"count": 0}

        @contextmanager
        def flaky_session_scope():
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("simulated local delete failure")
            with real_session_scope() as s:
                yield s

        monkeypatch.setattr(agent_space_workspace, "session_scope", flaky_session_scope)

        try:
            await agent_spaces_routes.delete_agent_space_for_task(
                SimpleNamespace(registry={}), task_pid
            )
        except RuntimeError as exc:
            assert "simulated local delete failure" in str(exc)
        else:
            raise AssertionError("expected simulated local delete failure")

        with session_scope() as s:
            assert s.get(AgentSpace, space_id) is not None
            assert (
                s.query(RemoteTerminalSession)
                .filter(RemoteTerminalSession.terminal_id == "survives-local-failure")
                .one_or_none()
                is not None
            )

    asyncio.run(_run())
