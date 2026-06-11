# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import html
import json
import re

from httpx import ASGITransport, AsyncClient


def _create_agent_space_for_prompt_master() -> tuple[int, str]:
    import datetime as dt

    from openfocus.db import session_scope
    from openfocus.models import AgentSpace, Goal, Task

    with session_scope() as s:
        goal = Goal(
            title="Ship Prompt Master",
            content="Add backend support for prompt optimization.",
            due_date=dt.date.today(),
        )
        s.add(goal)
        s.flush()
        task = Task(
            goal_id=goal.id,
            title="Implement optimize endpoint",
            content="Create an API that improves a textarea prompt with LLM context.",
            status="todo",
        )
        s.add(task)
        s.flush()
        space = AgentSpace(task_public_id=task.public_id, root_path="/tmp/work")
        s.add(space)
        s.flush()
        return int(space.id), str(task.public_id)


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
    assert "rt-prompt-row-single" not in css
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


def test_agent_space_prompt_master_optimize_returns_llm_prompt(monkeypatch):
    async def _run() -> None:
        import openfocus.app as app_module
        from openfocus.agent.llm.types import LLMCallResult
        from openfocus.app import app
        from openfocus.db import session_scope
        from openfocus.models import AgentSpacePrompt

        space_id, _task_public_id = _create_agent_space_for_prompt_master()

        class FakeProvider:
            def __init__(self):
                self.calls = []

            def chat_completions(self, **kwargs):
                self.calls.append(kwargs)
                user_text = str((kwargs["messages"][1] or {}).get("content") or "")
                assert "Implement optimize endpoint" in user_text
                assert "improves a textarea prompt" in user_text
                assert "make this better" in user_text
                return LLMCallResult(
                    content="Review the current backend/spec/test changes and propose a concise implementation plan.",
                    finish_reason="stop",
                    usage={"total_tokens": 37},
                    tool_calls=None,
                )

        provider = FakeProvider()
        monkeypatch.setattr(
            app_module,
            "_get_llm_provider_or_error",
            lambda: (provider, None),
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                f"/api/agent_spaces/{space_id}/prompt_master/optimize",
                json={"prompt": "make this better"},
            )

        assert r.status_code == 200
        assert r.json() == {
            "ok": True,
            "prompt": "Review the current backend/spec/test changes and propose a concise implementation plan.",
            "usage": {"total_tokens": 37},
        }
        assert len(provider.calls) == 1
        assert provider.calls[0]["temperature"] == 0.2

        with session_scope() as s:
            assert s.query(AgentSpacePrompt).count() == 0

    import asyncio

    asyncio.run(_run())


def test_agent_space_prompt_master_optimize_rejects_empty_prompt(monkeypatch):
    async def _run() -> None:
        import openfocus.app as app_module
        from openfocus.app import app

        space_id, _task_public_id = _create_agent_space_for_prompt_master()
        monkeypatch.setattr(
            app_module,
            "_get_llm_provider_or_error",
            lambda: (_ for _ in ()).throw(AssertionError("provider not needed")),
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                f"/api/agent_spaces/{space_id}/prompt_master/optimize",
                json={"prompt": "   "},
            )

        assert r.status_code == 400
        assert r.json()["detail"] == "prompt is required"

    import asyncio

    asyncio.run(_run())


def test_agent_space_prompt_master_optimize_maps_missing_space_to_404(monkeypatch):
    async def _run() -> None:
        import openfocus.app as app_module
        from openfocus.app import app

        monkeypatch.setattr(
            app_module,
            "_get_llm_provider_or_error",
            lambda: (_ for _ in ()).throw(AssertionError("provider not needed")),
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/api/agent_spaces/99999/prompt_master/optimize",
                json={"prompt": "Improve this prompt"},
            )

        assert r.status_code == 404
        assert r.json()["detail"] == "AgentSpace not found"

    import asyncio

    asyncio.run(_run())


def test_agent_space_prompt_master_optimize_maps_llm_failure_to_502(monkeypatch):
    async def _run() -> None:
        import openfocus.app as app_module
        from openfocus.app import app

        space_id, _task_public_id = _create_agent_space_for_prompt_master()

        class FailingProvider:
            def chat_completions(self, **kwargs):
                raise RuntimeError("upstream unavailable")

        monkeypatch.setattr(
            app_module,
            "_get_llm_provider_or_error",
            lambda: (FailingProvider(), None),
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                f"/api/agent_spaces/{space_id}/prompt_master/optimize",
                json={"prompt": "Improve this prompt"},
            )

        assert r.status_code == 502
        assert r.json()["detail"] == "upstream unavailable"

    import asyncio

    asyncio.run(_run())


def test_agent_space_prompt_master_optimize_maps_missing_llm_config_to_502(
    monkeypatch,
):
    async def _run() -> None:
        import openfocus.app as app_module
        from openfocus.app import app

        space_id, _task_public_id = _create_agent_space_for_prompt_master()
        monkeypatch.setattr(
            app_module,
            "_get_llm_provider_or_error",
            lambda: (None, "Missing LLM configuration"),
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                f"/api/agent_spaces/{space_id}/prompt_master/optimize",
                json={"prompt": "Improve this prompt"},
            )

        assert r.status_code == 502
        assert r.json()["detail"] == "Missing LLM configuration"

    import asyncio

    asyncio.run(_run())


def test_agent_space_view_passes_task_basic_and_autostart_config():
    async def _run() -> None:
        import datetime as dt

        from openfocus.app import app
        from openfocus.db import session_scope
        from openfocus.models import AgentSpace, Goal, Task

        task_basic = "Investigate the failing prompt zone flow.\nRun focused tests."

        with session_scope() as s:
            g = Goal(title="g", content="d", due_date=dt.date.today())
            s.add(g)
            s.flush()
            t = Task(goal_id=g.id, title="t", content=task_basic, status="todo")
            s.add(t)
            s.flush()
            sp = AgentSpace(
                task_public_id=t.public_id,
                root_path="/tmp/openfocus-ws",
                start_agent_command="coco -y",
            )
            s.add(sp)
            s.flush()
            task_public_id = str(t.public_id)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(f"/tasks/{task_public_id}/agent_space?autostart=1")
            assert r.status_code == 200

        match = re.search(
            r'id="agent-space-react-root"[^>]*data-config=\'([^\']+)\'',
            r.text,
        )
        assert match is not None
        config = json.loads(html.unescape(match.group(1)))
        assert config["taskBasic"] == task_basic
        assert config["taskTitle"] == "t"
        assert config["taskUrl"] == f"/goals?task={task_public_id}"
        assert config["taskDueDate"] == str(dt.date.today())
        assert "spaceCreatedAt" in config
        assert "spaceCompanion" in config
        assert config["startAgentCommand"] == "coco -y"
        assert config["autoStartDefaultTerminal"] is True
        assert "space-copy-task" not in r.text

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
