# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace


class FakeTerminalConn:
    def __init__(self) -> None:
        self.starts: list[dict] = []
        self.inputs: list[dict] = []
        self.mouse_modes: list[dict] = []
        self.stops: list[dict] = []

    async def request_terminal_start(self, **kwargs):
        self.starts.append(kwargs)
        return SimpleNamespace(
            terminal_id=kwargs["terminal_id"],
            backend="ttyd",
            connect_url="http://127.0.0.1:7681",
        )

    async def request_terminal_input(self, **kwargs):
        self.inputs.append(kwargs)
        return SimpleNamespace(ok=True)

    async def request_terminal_mouse_mode(self, **kwargs):
        self.mouse_modes.append(kwargs)
        return SimpleNamespace(enabled=kwargs["enabled"])

    async def request_terminal_stop(self, **kwargs):
        self.stops.append(kwargs)
        return SimpleNamespace(ok=True)


def test_terminal_gateway_lifecycle_is_owner_scoped():
    async def _run() -> None:
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import terminals as terminal_records
        from openfocus.domains.terminals import gateway as terminal_gateway
        from openfocus.models import RemoteTerminalOutput, RemoteTerminalSession

        ids = iter(["term-a", "term-b"])
        gateway = terminal_gateway.RemoteTerminalGateway(
            terminal_id_factory=lambda: next(ids)
        )
        conn = FakeTerminalConn()
        owner = terminal_records.owner_for_agent_space(10)

        created = await gateway.start_terminal(
            owner=owner,
            conn=conn,
            companion_id=7,
            root_path="/tmp/ws",
            base_path="/api/agent_spaces/10/terminals/{terminal_id}/ttyd/",
            task_public_id="TASK-1",
        )

        assert created.terminal_id == "term-a"
        assert created.name == "terminal"
        assert conn.starts[0]["base_path"].endswith("/term-a/ttyd/")

        with session_scope() as s:
            row = (
                s.query(RemoteTerminalSession)
                .filter(RemoteTerminalSession.terminal_id == "term-a")
                .one()
            )
            assert row.owner_type == "agent_space"
            assert row.owner_id == 10
            assert row.space_id == 10
            assert row.task_public_id == "TASK-1"
            s.add(
                RemoteTerminalOutput(
                    space_id=10,
                    terminal_id="term-a",
                    data_b64=base64.b64encode(b"hello").decode("ascii"),
                    nbytes=5,
                )
            )

        renamed = gateway.rename_terminal(
            owner=owner, terminal_id="term-a", name="work"
        )
        assert renamed == "work"

        raw = await gateway.inject_input(
            owner=owner,
            terminal_id="term-a",
            payload={"text": "ls\n"},
            conn=conn,
        )
        assert raw == b"ls\n"
        assert conn.inputs[0]["terminal_id"] == "term-a"

        enabled = await gateway.set_mouse_mode(
            owner=owner, terminal_id="term-a", enabled=True, conn=conn
        )
        assert enabled is True
        assert conn.mouse_modes[0]["enabled"] is True

        cleared: list[str] = []
        await gateway.close_terminal(
            owner=owner,
            terminal_id="term-a",
            conn=conn,
            clear_auto_prompt=cleared.append,
        )

        assert conn.stops[0]["terminal_id"] == "term-a"
        assert cleared == ["term-a"]
        with session_scope() as s:
            assert (
                s.query(RemoteTerminalSession)
                .filter(RemoteTerminalSession.terminal_id == "term-a")
                .one_or_none()
                is None
            )
            assert (
                s.query(RemoteTerminalOutput)
                .filter(RemoteTerminalOutput.terminal_id == "term-a")
                .count()
                == 0
            )

    asyncio.run(_run())


def test_terminal_gateway_payload_and_ttyd_helpers_work_for_inspiration_owner():
    from openfocus.db import session_scope
    from openfocus.domains.agent_spaces import terminals as terminal_records
    from openfocus.domains.terminals import gateway as terminal_gateway
    from openfocus.models import RemoteTerminalSession

    owner = terminal_records.owner_for_inspiration_space(5)
    with session_scope() as s:
        terminal = terminal_records.create_terminal_record(
            s,
            owner=owner,
            task_public_id="",
            companion_id=3,
            root_path="/tmp/inspiration",
            terminal_id="insp-term",
            backend="ttyd",
            connect_url="http://127.0.0.1:7681",
        )
        payload = terminal_gateway.terminal_payload(
            5, terminal, route_prefix="/api/inspirations"
        )

    assert payload["terminal_id"] == "insp-term"
    assert payload["embed_url"] == "/api/inspirations/5/terminals/insp-term/ttyd/"
    assert (
        terminal_gateway.ttyd_target_url(
            "http://127.0.0.1:7681",
            "/api/inspirations/5/terminals/insp-term/ttyd/",
            "q=1",
        )
        == "http://127.0.0.1:7681/api/inspirations/5/terminals/insp-term/ttyd/?q=1"
    )

    html = b"<html><head></head><body>ok</body></html>"
    injected = terminal_gateway.maybe_inject_ttyd_bridge(html, "text/html")
    assert b"__openfocusTtydBridgeInstalled" in injected
    assert terminal_gateway.maybe_inject_ttyd_bridge(injected, "text/html") == injected

    with session_scope() as s:
        row = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == "insp-term")
            .one()
        )
        assert row.owner_type == "inspiration_space"
        assert row.owner_id == 5
        assert row.space_id == -5
