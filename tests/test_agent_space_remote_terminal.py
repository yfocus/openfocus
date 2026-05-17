# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import os

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


def test_agent_space_ttyd_bridge_injection_is_html_only_and_idempotent():
    from openfocus.web.routes.agent_spaces import _maybe_inject_ttyd_bridge

    html = b"<html><head></head><body>ok</body></html>"
    injected = _maybe_inject_ttyd_bridge(html, "text/html; charset=utf-8")

    assert b"__openfocusTtydBridgeInstalled" in injected
    assert injected.count(b"__openfocusTtydBridgeInstalled") == 2
    assert _maybe_inject_ttyd_bridge(injected, "text/html") == injected
    assert _maybe_inject_ttyd_bridge(html, "application/json") == html


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
