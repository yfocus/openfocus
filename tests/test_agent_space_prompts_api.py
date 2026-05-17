# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from httpx import ASGITransport, AsyncClient


def test_terminal_prompt_zone_loads_custom_prompts():
    from pathlib import Path

    js = Path("openfocus/static/terminal-panel/terminal.js").read_text(encoding="utf-8")
    css = Path("openfocus/static/terminal-panel/terminal.css").read_text(
        encoding="utf-8"
    )

    assert "/api/agent_space_prompts" in js
    assert "/auto_prompts" in js
    assert "auto_enabled" in js
    assert "data-auto-prompt-id" in js
    assert "data-auto-builtin" in js
    assert "rt-custom-prompts" in js
    assert "data-prompt-id" in js
    assert "rt-zone-divider" in js
    assert js.count("rt-zone-divider") >= 2
    assert "custom prompts" not in js
    assert "system prompts" not in js
    assert 'id="rt-custom"' not in js
    assert "Custom</button>" not in js
    assert "Prompt Zone" not in js
    assert "prompt zone" in js
    assert "agent_mode" not in js
    assert "Agent Mode" not in js
    assert "normalizeAutoPromptText(content)" in js
    assert "[${title}]" not in js
    assert "PATCH" in Path("openfocus/templates/agent_space_prompts.html").read_text(
        encoding="utf-8"
    )
    assert "rt-prompt-list" in css
    assert "rt-zone-divider" in css
    assert "rt-auto-switch" in css
    assert "min-height:32px" in css
    assert "text-align:left" in css
    assert "text-align:center" in css


def test_ttyd_auto_prompt_rewriter_appends_on_submit():
    from openfocus.infrastructure.streaming import TerminalEventHub

    hub = TerminalEventHub()
    hub.ttyd_auto_prompts["term-1"] = {
        "enabled": True,
        "prompt": "Always report risky external calls.",
    }

    out = hub.rewrite_ttyd_input_for_auto_prompts("term-1", b"0hello\r")

    assert out.startswith(b"0hello")
    assert b"Always report risky external calls." in out
    assert out.endswith(b"\r")
    assert hub.rewrite_ttyd_input_for_auto_prompts("term-2", b"0hello\r") == b"0hello\r"


def test_agent_space_prompt_crud_and_page_render():
    async def _run() -> None:
        from openfocus.app import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/agent_space_prompts")
            assert r.status_code == 200
            assert "AgentSpace Prompts" in r.text
            assert "Agent Prompts" in r.text
            assert "Auto attach" in r.text

            r = await client.post(
                "/api/agent_space_prompts",
                json={
                    "title": "Review changes",
                    "content": "Review the current diff and report risks.",
                    "enabled": True,
                    "auto_enabled": True,
                },
            )
            assert r.status_code == 200
            item = r.json()["item"]
            prompt_id = int(item["id"])
            assert item["title"] == "Review changes"
            assert item["enabled"] is True
            assert item["auto_enabled"] is True

            r = await client.get("/api/agent_space_prompts")
            assert r.status_code == 200
            items = r.json()["items"]
            assert [x["title"] for x in items] == ["Review changes"]
            assert items[0]["auto_enabled"] is True

            r = await client.patch(
                f"/api/agent_space_prompts/{prompt_id}/auto_enabled",
                json={"auto_enabled": False},
            )
            assert r.status_code == 200
            assert r.json()["item"]["auto_enabled"] is False

            r = await client.patch(
                f"/api/agent_space_prompts/{prompt_id}/enabled",
                json={"enabled": False},
            )
            assert r.status_code == 200
            assert r.json()["item"]["enabled"] is False

            r = await client.get("/api/agent_space_prompts")
            assert r.status_code == 200
            assert r.json()["items"] == []

            r = await client.put(
                f"/api/agent_space_prompts/{prompt_id}",
                json={
                    "title": "Run tests",
                    "content": "Run focused tests and summarize failures.",
                    "enabled": False,
                    "auto_enabled": True,
                },
            )
            assert r.status_code == 200
            assert r.json()["item"]["title"] == "Run tests"
            assert r.json()["item"]["enabled"] is False
            assert r.json()["item"]["auto_enabled"] is True

            r = await client.get("/api/agent_space_prompts")
            assert r.status_code == 200
            assert r.json()["items"] == []

            r = await client.get(
                "/api/agent_space_prompts", params={"enabled_only": "false"}
            )
            assert r.status_code == 200
            assert [x["title"] for x in r.json()["items"]] == ["Run tests"]
            assert r.json()["items"][0]["auto_enabled"] is True

            r = await client.delete(f"/api/agent_space_prompts/{prompt_id}")
            assert r.status_code == 200
            r = await client.get(
                "/api/agent_space_prompts", params={"enabled_only": "false"}
            )
            assert r.status_code == 200
            assert r.json()["items"] == []

    import asyncio

    asyncio.run(_run())


def test_agent_space_terminal_auto_prompts_endpoint_updates_rewriter():
    async def _run() -> None:
        import datetime as dt

        from openfocus.app import app
        from openfocus.db import session_scope
        from openfocus.infrastructure import streaming
        from openfocus.models import AgentSpace, Goal, RemoteTerminalSession, Task

        with session_scope() as s:
            g = Goal(title="g", content="d", due_date=dt.date.today())
            s.add(g)
            s.flush()
            t = Task(goal_id=g.id, title="t", content="d", status="todo")
            s.add(t)
            s.flush()
            sp = AgentSpace(task_public_id=t.public_id, root_path="/tmp")
            s.add(sp)
            s.flush()
            space_id = int(sp.id)
            term = RemoteTerminalSession(
                owner_type="agent_space",
                owner_id=space_id,
                space_id=space_id,
                task_public_id=t.public_id,
                root_path="/tmp",
                name="terminal",
                terminal_id="term-auto",
                backend="ttyd",
                connect_url="http://127.0.0.1:9999",
                status="active",
            )
            s.add(term)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                f"/api/agent_spaces/{space_id}/terminals/term-auto/auto_prompts",
                json={"enabled": True, "prompt": "Report every external message."},
            )
            assert r.status_code == 200
            assert r.json()["enabled"] is True
            assert (
                streaming.terminal_event_hub.ttyd_auto_prompts["term-auto"]["prompt"]
                == "Report every external message."
            )

            r = await client.post(
                f"/api/agent_spaces/{space_id}/terminals/term-auto/auto_prompts",
                json={"enabled": False, "prompt": ""},
            )
            assert r.status_code == 200
            assert "term-auto" not in streaming.terminal_event_hub.ttyd_auto_prompts

    import asyncio

    asyncio.run(_run())
