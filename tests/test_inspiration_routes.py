# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.anyio
async def test_inspiration_pages_and_nav_render(monkeypatch):
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)

    from openfocus.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/inspirations")
        assert r.status_code == 200
        assert "Create Space" in r.text
        assert "No Inspiration space is selected yet." in r.text
        assert "Publish History" not in r.text

        r = await client.get("/goals")
        assert r.status_code == 200
        assert "Inspiration" in r.text
        assert "Create Space" in r.text


@pytest.mark.anyio
async def test_missing_inspiration_detail_falls_back_to_empty_state(monkeypatch):
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)

    from openfocus.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/inspirations/999999")
        assert r.status_code == 200
        assert "Inspiration space #999999 no longer exists" in r.text
        assert "No Inspiration space is selected yet." in r.text


@pytest.mark.anyio
async def test_inspiration_detail_renders_sidebar_status_and_resource_controls(
    monkeypatch,
):
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)

    from openfocus.app import app
    from openfocus.db import session_scope
    from openfocus.models import InspirationSpace

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        open_resp = await client.post("/api/inspirations", json={"title": "Open space"})
        assert open_resp.status_code == 200
        open_id = int(open_resp.json()["item"]["id"])

        resource_resp = await client.post(
            f"/api/inspirations/{open_id}/resources",
            data={
                "type": "text",
                "name": "Long context",
                "text_content": "Long context. " * 30,
            },
        )
        assert resource_resp.status_code == 200

        multiline_resp = await client.post(
            f"/api/inspirations/{open_id}/resources",
            data={
                "type": "text",
                "name": "Outline note",
                "text_content": "\n".join(["step"] * 12),
            },
        )
        assert multiline_resp.status_code == 200

        url_resp = await client.post(
            f"/api/inspirations/{open_id}/resources",
            data={
                "type": "url",
                "name": "Reference link",
                "url_content": "https://example.com/spec",
            },
        )
        assert url_resp.status_code == 200

        image_resp = await client.post(
            f"/api/inspirations/{open_id}/resources",
            data={"type": "image", "name": "Mockup"},
            files={"file": ("mockup.png", b"png-bytes", "image/png")},
        )
        assert image_resp.status_code == 200

        closed_resp = await client.post(
            "/api/inspirations", json={"title": "Closed space"}
        )
        assert closed_resp.status_code == 200
        closed_id = int(closed_resp.json()["item"]["id"])

        published_resp = await client.post(
            "/api/inspirations", json={"title": "Published space"}
        )
        assert published_resp.status_code == 200
        published_id = int(published_resp.json()["item"]["id"])

        with session_scope() as s:
            closed_space = s.get(InspirationSpace, closed_id)
            assert closed_space is not None
            closed_space.status = "closed"
            closed_space.closed_at = dt.datetime.now(dt.timezone.utc)

            published_space = s.get(InspirationSpace, published_id)
            assert published_space is not None
            published_space.status = "published"
            published_space.published_at = dt.datetime.now(dt.timezone.utc)

        page = await client.get(f"/inspirations/{open_id}")
        assert page.status_code == 200
        html = page.text

        assert 'role="button">List</a>' not in html
        assert 'class="insp-status-dot open"' in html
        assert 'class="insp-status-dot closed"' in html
        assert 'class="insp-status-dot published"' in html
        assert 'class="btn-primary insp-create-btn"' in html
        assert ".resources-scroll{" in html
        assert 'id="insp-open-resource-modal"' in html
        assert 'id="insp-resource-modal" class="modal-backdrop" hidden' in html
        assert 'id="insp-resource-form" class="modal-form"' in html
        assert "Idea incubation spaces" not in html
        assert (
            "URLs, images, notes, and summaries sent into the planner context."
            not in html
        )
        assert (
            "Attach URLs, images, and notes into the planner context only when needed."
            not in html
        )
        assert (
            "普通对话直接发送消息；命令通过 `/summary_title` 或 `/plan` 进入同一聊天流。"
            not in html
        )
        assert '<form id="insp-resource-form" class="stack">' not in html
        assert (
            'class="btn-ghost composer-tool-btn insp-command-btn" id="insp-title-suggest"'
            in html
        )
        assert (
            'class="btn-ghost composer-tool-btn insp-command-btn" id="insp-generate-draft"'
            in html
        )
        assert 'id="insp-fork-space"' not in html
        assert 'class="composer-input-row"' in html
        assert 'class="btn-primary insp-create-btn composer-submit"' in html
        assert (
            'class="btn-primary insp-create-btn" id="insp-open-resource-modal"' in html
        )
        assert ".insp-shell.stretch{" in html
        assert ".insp-panel-body{" in html
        assert (
            ".insp-detail{ display:grid; grid-template-columns: 300px minmax(0, 1fr); gap:16px; align-items:stretch; min-height:0; height:calc(100vh - 124px); }"
            in html
        )
        assert (
            ".insp-shell > .divider{ height:1px; margin:0; background:rgba(255,255,255,0.10); }"
            in html
        )
        assert (
            ".resource-preview{ margin-top:4px; position:relative; color:rgba(255,255,255,0.82); font-size:12px; line-height:1.35; white-space:pre-wrap; }"
            in html
        )
        assert (
            ".resource-preview-link.collapsed{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }"
            in html
        )
        assert (
            ".resource-preview-text.collapsed{ max-height:calc(1.35em * 4); overflow:hidden; }"
            in html
        )
        assert (
            ".resources-items-scroll{ flex:1; min-height:0; height:100%; overflow-y:auto; overflow-x:hidden; padding-right:4px; display:flex; flex-direction:column; gap:6px; }"
            in html
        )
        assert ".resource-actions .btn-ghost," in html
        assert ".resource-actions a.btn-ghost{" in html
        assert (
            ".resource-send-btn{ min-width:auto; justify-self:end; align-self:flex-end; }"
            in html
        )
        assert (
            ".resource-toggle{ padding:0; border:none; background:none; color:rgba(0,229,255,0.86); cursor:pointer; justify-self:start; align-self:end; text-align:left; line-height:1.15; }"
            in html
        )
        assert 'data-auto-collapse="true"' in html
        assert 'data-collapse-threshold="64"' in html
        assert "data-resource-toggle" in html
        assert "data-resource-edit" in html
        assert "data-resource-copy" in html
        assert "data-resource-delete" in html
        assert "data-resource-replace" in html
        assert 'data-resource-send="[#2 Long context] "' in html
        assert 'data-resource-send="[#3 Outline note] "' in html
        assert 'data-resource-send="[#4 Reference link] "' in html
        assert 'data-resource-send="[#5 Mockup] "' in html
        assert 'download="Mockup"' in html
        assert ">Expand</button>" in html
        assert "scheduleBusyPoll(900)" in html
        assert "refreshPageFromServer" in html
        assert (
            "setTimeout(()=>{ try{ location.reload(); }catch(_){ } }, 1200);"
            not in html
        )
        assert re.search(
            r'class="btn-ghost resource-send-btn" data-resource-send="\[#2 Long context\] "\s*>Send</button>',
            html,
        )
        assert re.search(
            r'<div class="resource-toolbar">\s*<button type="button" class="resource-toggle"[^>]*>Expand</button>\s*<div class="resource-actions">',
            html,
        )
        assert re.search(
            r'data-resource-edit[^>]*>Edit</button>\s*<button type="button" class="btn-ghost" data-resource-copy>Copy</button>\s*<button type="button" class="btn-ghost" data-resource-delete[^>]*>Delete</button>\s*<button type="button" class="btn-ghost resource-send-btn"',
            html,
        )
        assert re.search(
            r'data-resource-replace[^>]*>Replace</button>\s*<a class="btn-ghost" href="/api/inspirations/\d+/resources/\d+/raw" download="Mockup">Download</a>\s*<button type="button" class="btn-ghost" data-resource-delete[^>]*>Delete</button>\s*<button type="button" class="btn-ghost resource-send-btn"',
            html,
        )

        published_page = await client.get(f"/inspirations/{published_id}")
        assert published_page.status_code == 200
        assert 'id="insp-fork-space"' in published_page.text


@pytest.mark.anyio
async def test_inspiration_resource_actions_support_edit_replace_and_delete(
    monkeypatch,
):
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)

    from openfocus.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/inspirations", json={"title": "Editable resources"}
        )
        assert create_resp.status_code == 200
        space_id = int(create_resp.json()["item"]["id"])

        text_resp = await client.post(
            f"/api/inspirations/{space_id}/resources",
            data={
                "type": "text",
                "name": "Draft note",
                "text_content": "original text",
            },
        )
        assert text_resp.status_code == 200
        text_id = int(text_resp.json()["item"]["id"])

        url_resp = await client.post(
            f"/api/inspirations/{space_id}/resources",
            data={
                "type": "url",
                "name": "Spec",
                "url_content": "https://example.com/old",
            },
        )
        assert url_resp.status_code == 200
        url_id = int(url_resp.json()["item"]["id"])

        image_resp = await client.post(
            f"/api/inspirations/{space_id}/resources",
            data={"type": "image", "name": "Screen v1"},
            files={"file": ("screen-v1.png", b"image-v1", "image/png")},
        )
        assert image_resp.status_code == 200
        image_item = image_resp.json()["item"]
        image_id = int(image_item["id"])

        edit_text = await client.patch(
            f"/api/inspirations/{space_id}/resources/{text_id}",
            json={"name": "Draft note v2", "text_content": "updated\ntext"},
        )
        assert edit_text.status_code == 200
        assert edit_text.json()["item"]["name"] == "Draft note v2"
        assert edit_text.json()["item"]["text_content"] == "updated\ntext"

        edit_url = await client.patch(
            f"/api/inspirations/{space_id}/resources/{url_id}",
            json={"name": "Spec v2", "url_content": "https://example.com/new"},
        )
        assert edit_url.status_code == 200
        assert edit_url.json()["item"]["name"] == "Spec v2"
        assert edit_url.json()["item"]["url_content"] == "https://example.com/new"

        replace_image = await client.post(
            f"/api/inspirations/{space_id}/resources/{image_id}/replace",
            data={"name": "Screen v2"},
            files={"file": ("screen-v2.jpg", b"image-v2", "image/jpeg")},
        )
        assert replace_image.status_code == 200
        replaced_item = replace_image.json()["item"]
        assert replaced_item["name"] == "Screen v2"
        assert replaced_item["raw_url"].endswith(
            f"/api/inspirations/{space_id}/resources/{image_id}/raw"
        )

        raw_resp = await client.get(replaced_item["raw_url"])
        assert raw_resp.status_code == 200
        assert raw_resp.content == b"image-v2"

        delete_resp = await client.delete(
            f"/api/inspirations/{space_id}/resources/{url_id}"
        )
        assert delete_resp.status_code == 200

        space_resp = await client.get(f"/api/inspirations/{space_id}")
        assert space_resp.status_code == 200
        remaining_ids = {int(item["id"]) for item in space_resp.json()["resources"]}
        assert text_id in remaining_ids
        assert image_id in remaining_ids
        assert url_id not in remaining_ids


@pytest.mark.anyio
async def test_inspiration_workspace_resource_files_and_draft_summary_sync(monkeypatch):
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)

    from openfocus.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/inspirations",
            json={
                "title": "Terminal ideation",
                "initial_message": "First terminal context",
                "mode": "terminal",
            },
        )
        assert create_resp.status_code == 200
        item = create_resp.json()["item"]
        space_id = int(item["id"])
        assert item["mode"] == "terminal"
        workspace = Path(item["workspace_path"])
        assert workspace.exists()
        assert (workspace / "resources").exists()
        initial_resources = sorted((workspace / "resources").glob("resource_1_*.md"))
        assert initial_resources
        initial_body = initial_resources[0].read_text(encoding="utf-8")
        assert "# Terminal ideation" in initial_body
        assert "First terminal context" in initial_body

        text_resp = await client.post(
            f"/api/inspirations/{space_id}/resources",
            data={"type": "text", "name": "Idea note", "text_content": "hello idea"},
        )
        assert text_resp.status_code == 200
        text_item = text_resp.json()["item"]
        assert text_item["external_path"].startswith("resources/")
        assert (workspace / text_item["external_path"]).read_text(
            encoding="utf-8"
        ) == "hello idea"

        draft_summary = workspace / "resources" / "draft_summary.md"
        draft_summary.write_text(
            "Idea\nBuild something\n\nWhy now\nNow\n\nGoal\nShip it\n\nProposed tasks\n- Task A\n\nOpen questions\n- None\n\nRejected / deferred ideas\n- Later",
            encoding="utf-8",
        )
        external_note = workspace / "resources" / "external_note.md"
        external_note.write_text(
            "# External note\n\nCreated outside OpenFocus", encoding="utf-8"
        )
        sync_resp = await client.post(f"/api/inspirations/{space_id}/resources/sync")
        assert sync_resp.status_code == 200
        sync_data = sync_resp.json()
        assert sync_data["synced"] is True
        assert sync_data["item"]["name"] == "Summary"
        assert sync_data["item"]["source"] == "terminal_agent"
        assert sync_data["item"]["external_path"] == "resources/draft_summary.md"
        synced_paths = {item["external_path"] for item in sync_data["items"]}
        assert "resources/external_note.md" in synced_paths

        space_after_sync = await client.get(f"/api/inspirations/{space_id}")
        assert space_after_sync.status_code == 200
        resource_paths = {
            item["external_path"] for item in space_after_sync.json()["resources"]
        }
        assert "resources/external_note.md" in resource_paths

        gen_resp = await client.post(
            f"/api/inspirations/{space_id}/drafts/generate_from_draft_summary"
        )
        assert gen_resp.status_code == 200
        settled = await _wait_until_not_waiting(client, space_id)
        assert any(msg["kind"] == "draft_generated" for msg in settled["messages"])

        resource_gen_resp = await client.post(
            f"/api/inspirations/{space_id}/drafts/generate_from_resource",
            json={"resource_id": int(text_item["id"])},
        )
        assert resource_gen_resp.status_code == 200
        deadline = asyncio.get_running_loop().time() + 3.0
        while True:
            settled = await _wait_until_not_waiting(client, space_id)
            if len(settled.get("drafts") or []) >= 2:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("resource-based draft was not generated in time")
            await asyncio.sleep(0.05)


@pytest.mark.anyio
async def test_inspiration_terminal_api_uses_workspace_and_direct_prompt(monkeypatch):
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)

    import openfocus.app as app_mod
    from openfocus.db import session_scope
    from openfocus.models import RemoteTerminalSession

    class FakeConn:
        def __init__(self):
            self.started = []
            self.inputs = []
            self.stopped = []
            self.mouse_modes = []

        async def request_terminal_start(self, **kwargs):
            self.started.append(kwargs)
            return SimpleNamespace(
                terminal_id=kwargs["terminal_id"],
                backend="ttyd",
                connect_url="http://127.0.0.1:43210",
            )

        async def request_terminal_input(self, **kwargs):
            self.inputs.append(kwargs)

        async def request_terminal_stop(self, **kwargs):
            self.stopped.append(kwargs)

        async def request_terminal_mouse_mode(self, **kwargs):
            self.mouse_modes.append(kwargs)
            return SimpleNamespace(enabled=bool(kwargs.get("enabled")))

    fake_conn = FakeConn()

    def fake_select_online_companion(companion_id=None):
        return SimpleNamespace(id=77), fake_conn

    monkeypatch.setattr(
        app_mod, "_select_online_companion", fake_select_online_companion
    )
    monkeypatch.setattr(app_mod, "_has_online_companion", lambda: True)

    app = app_mod.app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/inspirations", json={"title": "BYO terminal", "mode": "terminal"}
        )
        assert create_resp.status_code == 200
        item = create_resp.json()["item"]
        space_id = int(item["id"])
        workspace = Path(item["workspace_path"])

        page_resp = await client.get(f"/inspirations/{space_id}")
        assert page_resp.status_code == 200
        page_html = page_resp.text
        assert 'id="insp-remote-terminal"' in page_html
        assert "Remote Terminal" not in page_html
        assert 'id="insp-message-form"' not in page_html
        assert 'id="insp-open-terminal"' not in page_html
        assert 'id="insp-sync-resources"' not in page_html
        assert 'id="insp-terminal-draft-summary"' not in page_html
        assert 'id="insp-generate-from-summary"' not in page_html
        assert 'id="insp-resource-sync"' in page_html
        assert "terminal agent is an untrusted collaborator" not in page_html
        assert "Suggest Titles" not in page_html
        assert "Generate Draft" not in page_html
        assert "Agent Mode" not in page_html
        assert "Draft Summary" not in page_html

        term_resp = await client.post(f"/api/inspirations/{space_id}/terminals/new")
        assert term_resp.status_code == 200
        term = term_resp.json()["terminal"]
        tid = term["terminal_id"]
        assert term["embed_url"].startswith(
            f"/api/inspirations/{space_id}/terminals/{tid}/ttyd/"
        )
        assert fake_conn.started
        assert Path(fake_conn.started[-1]["root_path"]) == workspace

        mouse_off = await client.post(
            f"/api/inspirations/{space_id}/terminals/{tid}/mouse_mode",
            json={"enabled": False},
        )
        assert mouse_off.status_code == 200
        assert mouse_off.json()["enabled"] is False
        assert fake_conn.mouse_modes[-1]["terminal_id"] == tid
        assert fake_conn.mouse_modes[-1]["enabled"] is False

        mouse_on = await client.post(
            f"/api/inspirations/{space_id}/terminals/{tid}/mouse_mode",
            json={"enabled": True},
        )
        assert mouse_on.status_code == 200
        assert mouse_on.json()["enabled"] is True
        assert fake_conn.mouse_modes[-1]["enabled"] is True

        with session_scope() as s:
            row = (
                s.query(RemoteTerminalSession)
                .filter(RemoteTerminalSession.terminal_id == tid)
                .one()
            )
            assert row.owner_type == "inspiration_space"
            assert row.owner_id == space_id
            assert row.space_id == -space_id
            assert row.task_public_id is None
            assert row.root_path == str(workspace)

        prep = await client.post(
            f"/api/inspirations/{space_id}/terminals/{tid}/prepare_draft_summary"
        )
        assert prep.status_code == 200
        assert fake_conn.inputs
        raw = fake_conn.inputs[-1]["data"]
        assert b"resources/draft_summary.md" in raw
        assert b"level-1 heading as the goal title" in raw
        assert b"\x1b[200~" not in raw
        assert b"\x1b[201~" not in raw
        assert b"\n" not in raw
        assert not raw.endswith(b"\r")

        detail = await client.get(f"/api/inspirations/{space_id}")
        assert detail.status_code == 200
        resource_item = detail.json()["resources"][0]
        resource_id = int(resource_item["id"])
        resource_path = str(resource_item.get("external_path") or "").strip()
        assert resource_path.startswith("resources/")
        resource_send_payload = f"\x1b[200~./{resource_path}\x1b[201~".encode()
        send_resource = await client.post(
            f"/api/inspirations/{space_id}/terminals/{tid}/inject",
            json={"data_b64": base64.b64encode(resource_send_payload).decode("ascii")},
        )
        assert send_resource.status_code == 200
        assert fake_conn.inputs[-1]["terminal_id"] == tid
        assert fake_conn.inputs[-1]["data"] == resource_send_payload

        gen_before_close = await client.post(
            f"/api/inspirations/{space_id}/drafts/generate_from_resource",
            json={"resource_id": resource_id},
        )
        assert gen_before_close.status_code == 200
        await _wait_until_not_waiting(client, space_id)
        draft_page = await client.get(f"/inspirations/{space_id}")
        assert draft_page.status_code == 200
        assert "terminal-drafts" in draft_page.text
        assert "insp-draft-cancel-btn" in draft_page.text

        close = await client.post(f"/api/inspirations/{space_id}/close")
        assert close.status_code == 200
        assert fake_conn.stopped[-1]["terminal_id"] == tid
        closed_page = await client.get(f"/inspirations/{space_id}")
        assert closed_page.status_code == 200
        assert 'id="insp-remote-terminal"' not in closed_page.text
        assert '<div class="terminal-drafts' not in closed_page.text
        with session_scope() as s:
            assert (
                s.query(RemoteTerminalSession)
                .filter(RemoteTerminalSession.terminal_id == tid)
                .one_or_none()
                is None
            )

        reopen = await client.post(f"/api/inspirations/{space_id}/reopen")
        assert reopen.status_code == 200
        reopened_page = await client.get(f"/inspirations/{space_id}")
        assert reopened_page.status_code == 200
        assert 'id="insp-remote-terminal"' in reopened_page.text
        assert '<div class="terminal-drafts' not in reopened_page.text

        term_resp2 = await client.post(f"/api/inspirations/{space_id}/terminals/new")
        assert term_resp2.status_code == 200
        tid2 = term_resp2.json()["terminal"]["terminal_id"]

        detail = await client.get(f"/api/inspirations/{space_id}")
        assert detail.status_code == 200
        resource_id = int(detail.json()["resources"][0]["id"])
        gen_resp = await client.post(
            f"/api/inspirations/{space_id}/drafts/generate_from_resource",
            json={"resource_id": resource_id},
        )
        assert gen_resp.status_code == 200
        draft_detail = await _wait_until_not_waiting(client, space_id)
        draft_messages = [
            msg for msg in draft_detail["messages"] if msg["kind"] == "draft_generated"
        ]
        assert draft_messages
        draft_id = int(draft_messages[-1]["payload"]["draft"]["id"])
        publish = await client.post(
            f"/api/inspirations/{space_id}/publish", json={"draft_id": draft_id}
        )
        assert publish.status_code == 200
        published = await _wait_until_published(client, space_id)
        assert published["item"]["status"] == "published"
        assert fake_conn.stopped[-1]["terminal_id"] == tid2
        published_page = await client.get(f"/inspirations/{space_id}")
        assert published_page.status_code == 200
        assert 'id="insp-remote-terminal"' not in published_page.text
        assert '<div class="terminal-drafts published-view"' in published_page.text


async def _wait_until_not_waiting(
    client: AsyncClient, space_id: int, *, timeout: float = 3.0
):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        r = await client.get(f"/api/inspirations/{space_id}")
        assert r.status_code == 200
        data = r.json()
        if not data.get("is_waiting"):
            return data
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("inspiration response did not finish in time")
        await asyncio.sleep(0.05)


async def _wait_until_published(
    client: AsyncClient, space_id: int, *, timeout: float = 3.0
):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        r = await client.get(f"/api/inspirations/{space_id}")
        assert r.status_code == 200
        data = r.json()
        status = str((data.get("item") or {}).get("status") or "")
        if status == "published":
            return data
        if status == "error":
            raise AssertionError("inspiration publish failed")
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("inspiration publish did not finish in time")
        await asyncio.sleep(0.05)


@pytest.mark.anyio
async def test_inspiration_waiting_state_and_command_flow(monkeypatch):
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)

    import openfocus.app as app_mod

    original_followup = app_mod._kickoff_inspiration_followup
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_followup(
        *, space_id: int, user_message_id: int, pending_message_id: int
    ):
        started.set()
        await release.wait()
        await original_followup(
            space_id=space_id,
            user_message_id=user_message_id,
            pending_message_id=pending_message_id,
        )

    monkeypatch.setattr(app_mod, "_kickoff_inspiration_followup", blocked_followup)

    app = app_mod.app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/api/inspirations",
            json={"title": "Async waiting space"},
        )
        assert create.status_code == 200
        space_id = int(create.json()["item"]["id"])

        resource = await client.post(
            f"/api/inspirations/{space_id}/resources",
            data={
                "type": "text",
                "name": "context",
                "text_content": "Supporting context that can be cited back into the conversation.",
            },
        )
        assert resource.status_code == 200

        send = await client.post(
            f"/api/inspirations/{space_id}/messages",
            json={"content": "Please help me think this through."},
        )
        assert send.status_code == 200
        send_data = send.json()
        assert send_data["queued"] is True
        assert send_data["assistant_message"]["kind"] == "pending"

        await asyncio.wait_for(started.wait(), timeout=1.0)

        immediate = await client.get(f"/api/inspirations/{space_id}")
        assert immediate.status_code == 200
        immediate_data = immediate.json()
        assert immediate_data["is_waiting"] is True
        assert any(msg["kind"] == "pending" for msg in immediate_data["messages"])

        page = await client.get(f"/inspirations/{space_id}")
        assert page.status_code == 200
        assert "waiting for agent" in page.text
        assert "readonly disabled" in page.text
        assert 'data-resource-send="[#2 context] "' in page.text
        assert re.search(
            r'class="btn-ghost resource-send-btn" data-resource-send="\[#2 context\] "\s+disabled>Send</button>',
            page.text,
        )

        release.set()
        settled = await _wait_until_not_waiting(client, space_id)
        assert settled["is_waiting"] is False
        assert any(msg["kind"] == "message" for msg in settled["messages"])

        cmd = await client.post(f"/api/inspirations/{space_id}/commands/summary_title")
        assert cmd.status_code == 200
        cmd_data = cmd.json()
        assert cmd_data["queued"] is True
        assert cmd_data["user_message"]["content"] == "/summary_title"

        settled_cmd = await _wait_until_not_waiting(client, space_id)
        assert any(
            msg["content"] == "/summary_title" for msg in settled_cmd["messages"]
        )
        assert any(
            msg["kind"] == "title_suggestions" for msg in settled_cmd["messages"]
        )


@pytest.mark.anyio
async def test_inspiration_plan_generation_does_not_block_other_pages(monkeypatch):
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)

    import openfocus.app as app_mod

    original_followup = app_mod._kickoff_inspiration_followup
    started = asyncio.Event()

    async def observed_followup(
        *, space_id: int, user_message_id: int, pending_message_id: int
    ):
        started.set()
        await original_followup(
            space_id=space_id,
            user_message_id=user_message_id,
            pending_message_id=pending_message_id,
        )

    def slow_fallback_draft(space, messages, resources):
        time.sleep(0.35)
        return {
            "goal_title": space.title,
            "goal_description": "background draft",
            "tasks": [{"title": "Task 1", "description": "Do it"}],
            "open_questions": [],
            "rejected_or_deferred_ideas": [],
        }

    monkeypatch.setattr(app_mod, "_kickoff_inspiration_followup", observed_followup)
    monkeypatch.setattr(app_mod, "_inspiration_fallback_draft", slow_fallback_draft)

    app = app_mod.app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/api/inspirations",
            json={"title": "Non-blocking plan space"},
        )
        assert create.status_code == 200
        space_id = int(create.json()["item"]["id"])

        draft_resp = await client.post(f"/api/inspirations/{space_id}/drafts/generate")
        assert draft_resp.status_code == 200
        assert draft_resp.json()["queued"] is True

        await asyncio.wait_for(started.wait(), timeout=1.0)

        t0 = time.perf_counter()
        page = await client.get("/goals")
        elapsed = time.perf_counter() - t0
        assert page.status_code == 200
        assert elapsed < 0.2

        settled = await _wait_until_not_waiting(client, space_id, timeout=3.0)
        assert any(msg["kind"] == "draft_generated" for msg in settled["messages"])


@pytest.mark.anyio
async def test_inspiration_publish_state_persists_across_navigation(monkeypatch):
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)

    import openfocus.app as app_mod

    original_publish = app_mod._kickoff_inspiration_publish
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_publish(
        *, space_id: int, draft_id: int, due_date_iso: str, previous_status: str
    ):
        started.set()
        await release.wait()
        await original_publish(
            space_id=space_id,
            draft_id=draft_id,
            due_date_iso=due_date_iso,
            previous_status=previous_status,
        )

    monkeypatch.setattr(app_mod, "_kickoff_inspiration_publish", blocked_publish)

    app = app_mod.app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/api/inspirations", json={"title": "Persistent publishing space"}
        )
        assert create.status_code == 200
        space_id = int(create.json()["item"]["id"])

        draft_resp = await client.post(f"/api/inspirations/{space_id}/drafts/generate")
        assert draft_resp.status_code == 200
        draft_detail = await _wait_until_not_waiting(client, space_id)
        draft_messages = [
            msg for msg in draft_detail["messages"] if msg["kind"] == "draft_generated"
        ]
        assert draft_messages
        draft = draft_messages[-1]["payload"]["draft"]

        publish_resp = await client.post(
            f"/api/inspirations/{space_id}/publish",
            json={"draft_id": draft["id"]},
        )
        assert publish_resp.status_code == 200
        assert publish_resp.json()["queued"] is True

        await asyncio.wait_for(started.wait(), timeout=1.0)

        page = await client.get(f"/inspirations/{space_id}")
        assert page.status_code == 200
        assert "publishing" in page.text
        assert "Publishing..." in page.text

        list_page = await client.get("/inspirations")
        assert list_page.status_code == 200
        assert "Create Space" in list_page.text

        t0 = time.perf_counter()
        goals_page = await client.get("/goals")
        elapsed = time.perf_counter() - t0
        assert goals_page.status_code == 200
        assert elapsed < 0.2

        release.set()
        detail = await _wait_until_published(client, space_id)
        assert detail["item"]["status"] == "published"


@pytest.mark.anyio
async def test_inspiration_publish_does_not_hold_db_write_lock_during_summarization(
    monkeypatch,
):
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)

    import openfocus.app as app_mod
    from openfocus.db import session_scope
    from openfocus.models import Goal

    original_load_publish_snapshot = app_mod._inspiration_load_publish_snapshot
    started = threading.Event()
    release = threading.Event()

    def blocked_load_publish_snapshot(space_id: int, draft_id: int):
        snapshot = original_load_publish_snapshot(space_id, draft_id)
        started.set()
        if not release.wait(timeout=2.0):
            raise RuntimeError("timed out waiting to release publish snapshot")
        return snapshot

    monkeypatch.setattr(
        app_mod, "_inspiration_load_publish_snapshot", blocked_load_publish_snapshot
    )

    app = app_mod.app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        goal_resp = await client.post(
            "/goals",
            data={
                "title": "Regular goal",
                "content": "Used to verify unrelated writes stay responsive.",
                "due_date": (dt.date.today() + dt.timedelta(days=5)).isoformat(),
            },
            follow_redirects=False,
        )
        assert goal_resp.status_code == 303
        with session_scope() as s:
            goal = s.query(Goal).order_by(Goal.id.desc()).first()
            assert goal is not None
            goal_id = int(goal.id)

        create = await client.post(
            "/api/inspirations",
            json={
                "title": "Publish without global write lock",
                "initial_message": "Need a goal and one task.",
            },
        )
        assert create.status_code == 200
        space_id = int(create.json()["item"]["id"])

        draft_resp = await client.post(f"/api/inspirations/{space_id}/drafts/generate")
        assert draft_resp.status_code == 200
        draft_detail = await _wait_until_not_waiting(client, space_id)
        draft_messages = [
            msg for msg in draft_detail["messages"] if msg["kind"] == "draft_generated"
        ]
        assert draft_messages
        draft = draft_messages[-1]["payload"]["draft"]

        publish_resp = await client.post(
            f"/api/inspirations/{space_id}/publish",
            json={"draft_id": draft["id"]},
        )
        assert publish_resp.status_code == 200
        assert publish_resp.json()["queued"] is True

        assert await asyncio.to_thread(started.wait, 1.0)

        goals_page = await client.get("/goals")
        assert goals_page.status_code == 200

        t0 = time.perf_counter()
        create_task_resp = await client.post(
            f"/goals/{goal_id}/tasks",
            data={
                "title": "parallel task",
                "content": "should not wait for publish summary",
            },
            follow_redirects=False,
        )
        elapsed = time.perf_counter() - t0
        assert create_task_resp.status_code == 303
        assert elapsed < 0.5

        release.set()
        published = await _wait_until_published(client, space_id)
        assert published["item"]["status"] == "published"


@pytest.mark.anyio
async def test_inspiration_publish_and_fork_flow(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    from openfocus.app import app
    from openfocus.db import session_scope
    from openfocus.models import (
        Goal,
        InspirationDraft,
        InspirationPublishRecord,
        InspirationResource,
        InspirationSpace,
        Task,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/api/inspirations",
            json={
                "title": "Inspiration publish flow",
                "initial_message": "We need to turn this discussion into an actionable goal.",
            },
        )
        assert create.status_code == 200
        space_id = int(create.json()["item"]["id"])

        r = await client.post(
            f"/api/inspirations/{space_id}/resources",
            data={
                "type": "text",
                "name": "context",
                "text_content": "A supporting note for the planner.",
            },
        )
        assert r.status_code == 200

        draft_resp = await client.post(f"/api/inspirations/{space_id}/drafts/generate")
        assert draft_resp.status_code == 200
        assert draft_resp.json()["queued"] is True
        draft_detail = await _wait_until_not_waiting(client, space_id)
        draft_messages = [
            msg for msg in draft_detail["messages"] if msg["kind"] == "draft_generated"
        ]
        assert draft_messages
        draft = draft_messages[-1]["payload"]["draft"]
        assert draft["version"] == 1
        assert draft["tasks"]

        draft_page = await client.get(f"/inspirations/{space_id}")
        assert draft_page.status_code == 200
        assert "Publish this draft and lock this Inspiration space?" in draft_page.text
        assert "Publishing..." in draft_page.text
        assert "insp-publish-mask" in draft_page.text

        due_date = (dt.date.today() + dt.timedelta(days=9)).isoformat()
        publish_resp = await client.post(
            f"/api/inspirations/{space_id}/publish",
            json={
                "draft_id": draft["id"],
                "due_date": due_date,
            },
        )
        assert publish_resp.status_code == 200
        publish_data = publish_resp.json()
        assert publish_data["queued"] is True
        assert publish_data["status"] == "publishing"

        publishing_detail = await client.get(f"/api/inspirations/{space_id}")
        assert publishing_detail.status_code == 200
        publishing_data = publishing_detail.json()
        assert publishing_data["is_publishing"] in {True, False}

        detail_data = await _wait_until_published(client, space_id)
        goal_id = int(detail_data["item"]["published_goal_id"])
        assert detail_data["item"]["status"] == "published"
        assert detail_data["publish_records"]

        fork = await client.post(
            f"/api/inspirations/{space_id}/fork",
            json={
                "title": "Inspiration publish flow / follow-up",
                "include_all_resources": True,
            },
        )
        assert fork.status_code == 200
        fork_id = int(fork.json()["item"]["id"])

        page = await client.get(f"/inspirations/{space_id}")
        assert page.status_code == 200
        assert "Publish History" not in page.text

    with session_scope() as s:
        space = s.get(InspirationSpace, space_id)
        assert space is not None
        assert space.status == "published"
        assert space.published_goal_id == goal_id

        draft_row = s.get(InspirationDraft, int(draft["id"]))
        assert draft_row is not None

        goal = s.get(Goal, goal_id)
        assert goal is not None
        assert goal.source_inspiration_space_id == space_id
        assert goal.source_inspiration_draft_id == int(draft["id"])
        assert goal.due_date.isoformat() == due_date

        tasks = s.query(Task).filter(Task.goal_id == goal_id).all()
        assert len(tasks) == len(draft["tasks"])
        assert all(t.source_inspiration_space_id == space_id for t in tasks)
        assert all(t.source_inspiration_draft_id == int(draft["id"]) for t in tasks)

        summary_resource = (
            s.query(InspirationResource)
            .filter(InspirationResource.space_id == space_id)
            .filter(InspirationResource.type == "summary")
            .order_by(InspirationResource.id.desc())
            .first()
        )
        assert summary_resource is not None
        assert "Published tasks" in (summary_resource.text_content or "")

        record = (
            s.query(InspirationPublishRecord)
            .filter(InspirationPublishRecord.space_id == space_id)
            .order_by(InspirationPublishRecord.id.desc())
            .first()
        )
        assert record is not None
        assert record.created_goal_id == goal_id
        assert sorted(record.created_task_ids) == sorted(t.id for t in tasks)
        assert record.deferred_tasks == []

        forked = s.get(InspirationSpace, fork_id)
        assert forked is not None
        assert forked.forked_from_space_id == space_id
        fork_resources = (
            s.query(InspirationResource)
            .filter(InspirationResource.space_id == fork_id)
            .filter(InspirationResource.deleted_at.is_(None))
            .all()
        )
        assert any((res.type or "") == "summary" for res in fork_resources)

    audit_files = list((tmp_path / "memory" / "audit").glob("**/*.md"))
    assert audit_files
    audit_text = "\n".join(p.read_text(encoding="utf-8") for p in audit_files)
    assert "inspiration.published" in audit_text


@pytest.mark.anyio
async def test_inspiration_image_resource_raw_preview(monkeypatch):
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)

    from openfocus.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/api/inspirations",
            json={"title": "Image resource space"},
        )
        assert create.status_code == 200
        space_id = int(create.json()["item"]["id"])

        upload = await client.post(
            f"/api/inspirations/{space_id}/resources",
            data={"type": "image", "name": "diagram"},
            files={"file": ("diagram.png", b"fakepngcontent", "image/png")},
        )
        assert upload.status_code == 200
        resource_id = int(upload.json()["item"]["id"])

        detail = await client.get(f"/api/inspirations/{space_id}")
        assert detail.status_code == 200
        resources = detail.json()["resources"]
        image_resource = next(
            item for item in resources if int(item["id"]) == resource_id
        )
        assert image_resource["raw_url"].endswith(
            f"/api/inspirations/{space_id}/resources/{resource_id}/raw"
        )

        page = await client.get(f"/inspirations/{space_id}")
        assert page.status_code == 200
        assert 'class="resource-media-preview collapsed"' in page.text
        assert "data-media-preview" in page.text
        assert "data-resource-image" in page.text
        assert 'data-resource-toggle hidden data-collapse-threshold="84"' in page.text
        assert ">Expand</button>" in page.text

        raw = await client.get(
            f"/api/inspirations/{space_id}/resources/{resource_id}/raw"
        )
        assert raw.status_code == 200
        assert raw.content == b"fakepngcontent"
        assert raw.headers["content-type"].startswith("image/png")
