# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace


class FakeTerminalConn:
    def __init__(
        self, *, connect_url: str = "http://127.0.0.1:7681", live_terminal_ids=None
    ) -> None:
        self.starts: list[dict] = []
        self.inputs: list[dict] = []
        self.mouse_modes: list[dict] = []
        self.stops: list[dict] = []
        self.list_sessions: list[dict] = []
        self.connect_url = connect_url
        self.live_terminal_ids = list(live_terminal_ids or [])

    async def request_terminal_start(self, **kwargs):
        self.starts.append(kwargs)
        return SimpleNamespace(
            terminal_id=kwargs["terminal_id"],
            backend="ttyd",
            connect_url=self.connect_url,
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

    async def request_terminal_list_sessions(self, **kwargs):
        self.list_sessions.append(kwargs)
        return SimpleNamespace(
            sessions=[
                SimpleNamespace(terminal_id=tid, root_path="/tmp/ws", created_at=0.0)
                for tid in self.live_terminal_ids
            ]
        )


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


def test_terminal_gateway_rejects_ttyd_start_without_connect_url():
    async def _run() -> None:
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import terminals as terminal_records
        from openfocus.domains.terminals import gateway as terminal_gateway
        from openfocus.models import RemoteTerminalSession

        gateway = terminal_gateway.RemoteTerminalGateway(
            terminal_id_factory=lambda: "missing-url"
        )
        conn = FakeTerminalConn(connect_url="")
        owner = terminal_records.owner_for_agent_space(14)

        try:
            await gateway.start_terminal(
                owner=owner,
                conn=conn,
                companion_id=7,
                root_path="/tmp/ws",
                base_path="/api/agent_spaces/14/terminals/{terminal_id}/ttyd/",
                task_public_id="TASK-14",
            )
        except terminal_gateway.TerminalStartError as exc:
            assert "missing connect_url" in str(exc)
        else:
            raise AssertionError("ttyd start without connect_url must fail")

        assert conn.stops[0]["terminal_id"] == "missing-url"
        with session_scope() as s:
            assert s.query(RemoteTerminalSession).count() == 0

    asyncio.run(_run())


def test_terminal_gateway_reconciles_live_terminals_with_companion():
    async def _run() -> None:
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import terminals as terminal_records
        from openfocus.domains.terminals import gateway as terminal_gateway
        from openfocus.models import RemoteTerminalOutput, RemoteTerminalSession

        gateway = terminal_gateway.RemoteTerminalGateway()
        owner = terminal_records.owner_for_agent_space(18)
        with session_scope() as s:
            for tid in ("live-term", "stale-term"):
                terminal_records.create_terminal_record(
                    s,
                    owner=owner,
                    task_public_id="TASK-18",
                    companion_id=3,
                    root_path="/tmp/ws",
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

        conn = FakeTerminalConn(live_terminal_ids=["live-term"])
        terminals = await gateway.list_live_terminals(owner=owner, conn=conn)

        assert [t.terminal_id for t in terminals] == ["live-term"]
        assert conn.list_sessions[0]["timeout_seconds"] == 3.0
        with session_scope() as s:
            assert (
                s.query(RemoteTerminalSession)
                .filter(RemoteTerminalSession.terminal_id == "live-term")
                .one_or_none()
                is not None
            )
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

    asyncio.run(_run())


def test_terminal_gateway_loads_owner_scoped_history_with_sync_slicing():
    from openfocus.db import session_scope
    from openfocus.domains.agent_spaces import terminals as terminal_records
    from openfocus.domains.terminals import gateway as terminal_gateway
    from openfocus.models import RemoteTerminalOutput

    gateway = terminal_gateway.RemoteTerminalGateway()
    owner = terminal_records.owner_for_agent_space(22)
    other_owner = terminal_records.owner_for_inspiration_space(22)
    with session_scope() as s:
        terminal_records.create_terminal_record(
            s,
            owner=owner,
            task_public_id="TASK-22",
            companion_id=3,
            root_path="/tmp/ws",
            terminal_id="history-term",
            backend="ttyd",
            connect_url="http://127.0.0.1:7681",
        )
        for chunk in (
            b"before",
            b"\x1b[?1049hvim screen",
            b" still active",
        ):
            s.add(
                RemoteTerminalOutput(
                    space_id=owner.db_space_id,
                    terminal_id="history-term",
                    data_b64=base64.b64encode(chunk).decode("ascii"),
                    nbytes=len(chunk),
                )
            )

    result = gateway.load_history(
        owner=owner, terminal_id="history-term", max_bytes=1024
    )

    assert result["ok"] is True
    assert result["terminal_id"] == "history-term"
    assert base64.b64decode(result["data_b64"]) == b"\x1b[?1049hvim screen still active"
    assert result["truncated"] is False
    assert result["sync_sliced"] is True
    assert result["sync_reason"] == "alt_screen_active"

    try:
        gateway.load_history(owner=other_owner, terminal_id="history-term")
    except terminal_records.TerminalNotFound:
        pass
    else:
        raise AssertionError("terminal history lookup must stay owner scoped")


def test_terminal_gateway_releases_all_owner_terminals_best_effort():
    async def _run() -> None:
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import terminals as terminal_records
        from openfocus.domains.terminals import gateway as terminal_gateway
        from openfocus.models import RemoteTerminalOutput, RemoteTerminalSession

        gateway = terminal_gateway.RemoteTerminalGateway()
        owner = terminal_records.owner_for_agent_space(44)
        untouched_owner = terminal_records.owner_for_inspiration_space(44)
        with session_scope() as s:
            for tid in ("rel-a", "rel-b"):
                terminal_records.create_terminal_record(
                    s,
                    owner=owner,
                    task_public_id="TASK-44",
                    companion_id=3,
                    root_path="/tmp/ws",
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
            terminal_records.create_terminal_record(
                s,
                owner=untouched_owner,
                task_public_id="",
                companion_id=3,
                root_path="/tmp/insp",
                terminal_id="keep-me",
                backend="ttyd",
                connect_url="http://127.0.0.1:7681",
            )

        conn = FakeTerminalConn()
        cleared: list[str] = []
        released = await gateway.release_owner_terminals(
            owner=owner,
            conn=conn,
            clear_auto_prompt=cleared.append,
            timeout_seconds=5.0,
        )

        assert released == ["rel-a", "rel-b"]
        assert [item["terminal_id"] for item in conn.stops] == ["rel-a", "rel-b"]
        assert cleared == ["rel-a", "rel-b"]
        with session_scope() as s:
            assert (
                s.query(RemoteTerminalSession)
                .filter(RemoteTerminalSession.owner_type == "agent_space")
                .filter(RemoteTerminalSession.owner_id == 44)
                .count()
                == 0
            )
            assert (
                s.query(RemoteTerminalOutput)
                .filter(RemoteTerminalOutput.terminal_id.in_(["rel-a", "rel-b"]))
                .count()
                == 0
            )
            assert (
                s.query(RemoteTerminalSession)
                .filter(RemoteTerminalSession.terminal_id == "keep-me")
                .one_or_none()
                is not None
            )

    asyncio.run(_run())


def test_terminal_gateway_ttyd_proxy_helpers_are_protocol_neutral():
    async def _run() -> None:
        from openfocus.domains.terminals import gateway as terminal_gateway

        calls: list[object] = []

        class FakeResponse:
            status = 203
            headers = {
                "content-type": "text/html; charset=utf-8",
                "content-length": "999",
                "connection": "close",
                "x-openfocus": "ok",
            }

            def read(self) -> bytes:
                return b"<html><head></head><body>terminal</body></html>"

        def fake_opener(req, *, timeout):
            calls.append(req)
            assert timeout == 12.0
            return FakeResponse()

        target = terminal_gateway.ttyd_proxy_target(
            connect_url="http://127.0.0.1:7681",
            route_prefix="/api/agent_spaces",
            owner_id=7,
            terminal_id="term/one",
            path="ws",
            query="q=1",
        )
        assert target.proxy_prefix == "/api/agent_spaces/7/terminals/term%2Fone/ttyd/"
        assert (
            target.target_url
            == "http://127.0.0.1:7681/api/agent_spaces/7/terminals/term%2Fone/ttyd/ws?q=1"
        )
        assert (
            terminal_gateway.ttyd_websocket_target_url(target.target_url)
            == "ws://127.0.0.1:7681/api/agent_spaces/7/terminals/term%2Fone/ttyd/ws?q=1"
        )

        proxied = await terminal_gateway.proxy_ttyd_http_request(
            target_url=target.target_url,
            method="POST",
            headers={
                "host": "localhost",
                "connection": "keep-alive",
                "content-length": "5",
                "accept-encoding": "gzip",
                "x-user": "yes",
            },
            body=b"hello",
            opener=fake_opener,
            timeout_seconds=12.0,
        )

        assert calls
        assert proxied.status_code == 203
        assert proxied.media_type == "text/html; charset=utf-8"
        assert proxied.headers == {
            "content-type": "text/html; charset=utf-8",
            "x-openfocus": "ok",
        }
        assert b"__openfocusTtydBridgeInstalled" in proxied.body

    asyncio.run(_run())
