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
    assert "content.replace(/\\s+/g, ' ').trim()" in js
    assert "[${title}]" not in js
    assert "PATCH" in Path("openfocus/templates/agent_space_prompts.html").read_text(
        encoding="utf-8"
    )
    assert "rt-prompt-list" in css
    assert "rt-zone-divider" in css
    assert "min-height:32px" in css
    assert "text-align:left" in css
    assert "text-align:center" in css


def test_agent_space_prompt_crud_and_page_render():
    async def _run() -> None:
        from openfocus.app import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/agent_space_prompts")
            assert r.status_code == 200
            assert "AgentSpace Prompts" in r.text
            assert "Agent Prompts" in r.text

            r = await client.post(
                "/api/agent_space_prompts",
                json={
                    "title": "Review changes",
                    "content": "Review the current diff and report risks.",
                    "enabled": True,
                },
            )
            assert r.status_code == 200
            item = r.json()["item"]
            prompt_id = int(item["id"])
            assert item["title"] == "Review changes"
            assert item["enabled"] is True

            r = await client.get("/api/agent_space_prompts")
            assert r.status_code == 200
            items = r.json()["items"]
            assert [x["title"] for x in items] == ["Review changes"]

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
                },
            )
            assert r.status_code == 200
            assert r.json()["item"]["title"] == "Run tests"
            assert r.json()["item"]["enabled"] is False

            r = await client.get("/api/agent_space_prompts")
            assert r.status_code == 200
            assert r.json()["items"] == []

            r = await client.get(
                "/api/agent_space_prompts", params={"enabled_only": "false"}
            )
            assert r.status_code == 200
            assert [x["title"] for x in r.json()["items"]] == ["Run tests"]

            r = await client.delete(f"/api/agent_space_prompts/{prompt_id}")
            assert r.status_code == 200
            r = await client.get(
                "/api/agent_space_prompts", params={"enabled_only": "false"}
            )
            assert r.status_code == 200
            assert r.json()["items"] == []

    import asyncio

    asyncio.run(_run())
