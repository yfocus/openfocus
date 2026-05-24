# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import datetime as dt
import os
import re

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = [
    pytest.mark.blackbox,
    pytest.mark.skipif(
        os.environ.get("OPENFOCUS_RUN_BLACKBOX") != "1",
        reason="black-box tests are opt-in; run `make test-blackbox`",
    ),
]


def _goal_id_from_location(location: str | None) -> int:
    match = re.search(r"[?&]goal=(\d+)", str(location or ""))
    assert match is not None, f"missing goal id in redirect: {location!r}"
    return int(match.group(1))


def _task_row_for_title(html: str, title: str) -> tuple[str, str]:
    for match in re.finditer(
        r'(<tr class="js-open-task"(?P<attrs>[^>]*)>.*?</tr>)', html, re.S
    ):
        attrs = match.group("attrs")
        if f'data-sort-title="{title}"' not in attrs:
            continue
        public_match = re.search(r'data-task="([^"]+)"', attrs)
        assert public_match is not None
        return public_match.group(1), match.group(1)
    raise AssertionError(f"task row for {title!r} not found")


def _template_html(html: str, template_id: str) -> str:
    match = re.search(
        rf'<template id="{re.escape(template_id)}">(.*?)</template>', html, re.S
    )
    assert match is not None, f"template {template_id!r} not found"
    return match.group(1)


def _task_form_id(html: str, task_public_id: str, action: str) -> int:
    template = _template_html(html, f"detail-task-{task_public_id}")
    match = re.search(rf'action="/tasks/(\d+)/{re.escape(action)}"', template)
    assert match is not None, f"{action!r} form for {task_public_id!r} not found"
    return int(match.group(1))


def _assert_task_row_status(html: str, title: str, status_label: str) -> str:
    task_public_id, row = _task_row_for_title(html, title)
    assert f"<td>{status_label}</td>" in row
    return task_public_id


async def _wait_until_not_waiting(
    client: AsyncClient, space_id: int, *, timeout: float = 4.0
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await client.get(f"/api/inspirations/{space_id}")
        assert response.status_code == 200
        data = response.json()
        if data.get("is_waiting") is False:
            return data
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("inspiration follow-up did not finish in time")
        await asyncio.sleep(0.05)


async def _wait_until_published(
    client: AsyncClient, space_id: int, *, timeout: float = 4.0
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        response = await client.get(f"/api/inspirations/{space_id}")
        assert response.status_code == 200
        data = response.json()
        status = str((data.get("item") or {}).get("status") or "")
        if status == "published":
            return data
        if status == "error":
            raise AssertionError("inspiration publish failed")
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("inspiration publish did not finish in time")
        await asyncio.sleep(0.05)


@pytest.mark.anyio
async def test_dashboard_agent_report_human_review_and_calendar_flow(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENFOCUS_ARK_API_KEY", raising=False)
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    from openfocus.app import app

    goal_title = "Blackbox refactor safety goal"
    edited_goal_title = "Blackbox refactor safety goal v2"
    task_title = "Manual blackbox task"
    edited_task_title = "Manual blackbox task revised"
    due_date = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    ym = dt.date.today().strftime("%Y-%m")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        dashboard = await client.get("/goals")
        assert dashboard.status_code == 200
        assert "No goals yet." in dashboard.text

        create_goal = await client.post(
            "/goals",
            data={
                "title": goal_title,
                "content": "Exercise the public Dashboard flow during refactors.",
                "due_date": due_date,
            },
            follow_redirects=False,
        )
        assert create_goal.status_code == 303
        goal_id = _goal_id_from_location(create_goal.headers.get("location"))

        create_task = await client.post(
            f"/goals/{goal_id}/tasks",
            data={
                "title": task_title,
                "content": "Agent reports first; a human still confirms completion.",
            },
            follow_redirects=False,
        )
        assert create_task.status_code == 303

        goal_page = await client.get(f"/goals?goal={goal_id}")
        assert goal_page.status_code == 200
        task_public_id = _assert_task_row_status(
            goal_page.text, task_title, "Not started"
        )
        task_internal_id = _task_form_id(goal_page.text, task_public_id, "done")

        edit_goal = await client.post(
            f"/goals/{goal_id}/edit",
            data={
                "title": edited_goal_title,
                "content": "Edited through the public Dashboard route.",
                "due_date": due_date,
                "status": "active",
                "priority": "normal",
                "importance": "normal",
            },
            follow_redirects=False,
        )
        assert edit_goal.status_code == 303

        edit_task = await client.post(
            f"/tasks/{task_internal_id}/edit",
            data={
                "title": edited_task_title,
                "content": "Edited task content through the public Dashboard route.",
            },
            follow_redirects=False,
        )
        assert edit_task.status_code == 303

        edited_page = await client.get(f"/goals?task={task_public_id}")
        assert edited_page.status_code == 200
        assert edited_goal_title in edited_page.text
        assert "Edited through the public Dashboard route." in edited_page.text
        _assert_task_row_status(edited_page.text, edited_task_title, "Not started")
        assert (
            "Edited task content through the public Dashboard route."
            in _template_html(edited_page.text, f"detail-task-{task_public_id}")
        )

        agent_report = await client.post(
            "/api/agent/events",
            json={
                "kind": "task.completed",
                "agent": "codex",
                "task_id": task_public_id,
                "payload": {"percent": 100},
            },
        )
        assert agent_report.status_code == 200

        focus_report = await client.post(
            "/api/skills/focus_report",
            json={
                "agent": "coco",
                "task_name": edited_task_title,
                "status": "succeeded",
                "goal_id": goal_id,
                "task_public_id": task_public_id,
                "user_prompt": "Finish the task",
                "assistant_response": "Ready for review",
                "metadata": {"blackbox": True},
            },
        )
        assert focus_report.status_code == 200
        assert focus_report.json()["task_updated"] is None

        after_reports = await client.get(f"/goals?task={task_public_id}")
        assert after_reports.status_code == 200
        _assert_task_row_status(after_reports.text, edited_task_title, "Not started")
        assert "Completion reported (pending confirmation)" in after_reports.text
        assert (
            "Manual blackbox task revised · Done (pending confirmation)"
            in after_reports.text
        )
        assert "Source: Agent (codex)" in after_reports.text
        assert "Source: Agent (coco)" in after_reports.text

        recommendations = await client.get("/api/recommendations/next?limit=3")
        assert recommendations.status_code == 200
        rec_body = recommendations.json()
        feedback = await client.post(
            "/api/recommendations/feedback",
            json={
                "run_id": rec_body["run_id"],
                "task_public_id": task_public_id,
                "feedback_type": "dismiss",
                "reason_code": "not_for_now",
                "reason_text": "Verify event feedback survives refactors.",
            },
        )
        assert feedback.status_code == 200
        assert feedback.json()["task_public_id"] == task_public_id

        recent_events = await client.get("/api/events/recent?limit=20")
        assert recent_events.status_code == 200
        recent_items = recent_events.json()["items"]
        assert any(item["kind"] == "task.completed" for item in recent_items)
        assert any(item["kind"] == "skill.focus_report" for item in recent_items)
        assert any(item["kind"] == "next_move.not_for_now" for item in recent_items)

        finish = await client.post(
            f"/tasks/{task_internal_id}/done", follow_redirects=False
        )
        assert finish.status_code == 303

        done_page = await client.get(f"/goals?task={task_public_id}")
        assert done_page.status_code == 200
        _assert_task_row_status(done_page.text, edited_task_title, "Done")
        assert "Confirmed done by user" in done_page.text
        assert "Reopen" in _template_html(
            done_page.text, f"detail-task-{task_public_id}"
        )

        calendar = await client.get(f"/api/calendar/month?ym={ym}")
        assert calendar.status_code == 200
        day_items = [
            item
            for items in calendar.json()["days"].values()
            for item in items
            if item["task_public_id"] == task_public_id
        ]
        assert day_items
        assert day_items[0]["task_title"] == edited_task_title

        reopen = await client.post(
            f"/tasks/{task_internal_id}/reopen", follow_redirects=False
        )
        assert reopen.status_code == 303

        reopened_page = await client.get(f"/goals?task={task_public_id}")
        assert reopened_page.status_code == 200
        _assert_task_row_status(reopened_page.text, edited_task_title, "Not started")
        assert "Task reopened" in reopened_page.text

        delete_task = await client.post(
            f"/tasks/{task_internal_id}/delete", follow_redirects=False
        )
        assert delete_task.status_code == 303

        after_task_delete = await client.get(f"/goals?goal={goal_id}")
        assert after_task_delete.status_code == 200
        assert f'data-sort-title="{edited_task_title}"' not in after_task_delete.text

        delete_goal = await client.post(
            f"/goals/{goal_id}/delete", follow_redirects=False
        )
        assert delete_goal.status_code == 303

        after_goal_delete = await client.get("/goals")
        assert after_goal_delete.status_code == 200
        assert edited_goal_title not in after_goal_delete.text


@pytest.mark.anyio
async def test_inspiration_publish_read_only_and_fork_flow(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENFOCUS_ARK_API_KEY", raising=False)
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    from openfocus.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/api/inspirations",
            json={
                "title": "Blackbox publishable idea",
                "initial_message": "Shape this into one goal with reviewable tasks.",
            },
        )
        assert create.status_code == 200
        space_id = int(create.json()["item"]["id"])

        before_publish_goals = await client.get("/goals")
        assert before_publish_goals.status_code == 200
        assert "Blackbox publishable idea" not in before_publish_goals.text

        resource = await client.post(
            f"/api/inspirations/{space_id}/resources",
            data={
                "type": "text",
                "name": "Acceptance notes",
                "text_content": "The final workflow must keep human confirmation.",
            },
        )
        assert resource.status_code == 200

        draft_request = await client.post(
            f"/api/inspirations/{space_id}/drafts/generate"
        )
        assert draft_request.status_code == 200
        assert draft_request.json()["queued"] is True

        draft_detail = await _wait_until_not_waiting(client, space_id)
        draft_messages = [
            msg for msg in draft_detail["messages"] if msg["kind"] == "draft_generated"
        ]
        assert draft_messages
        draft = draft_messages[-1]["payload"]["draft"]
        assert draft["goal_title"] == "Blackbox publishable idea"
        assert len(draft["tasks"]) >= 3
        deferred_task_title = draft["tasks"][1]["title"]

        due_date = (dt.date.today() + dt.timedelta(days=14)).isoformat()
        publish = await client.post(
            f"/api/inspirations/{space_id}/publish",
            json={
                "draft_id": draft["id"],
                "due_date": due_date,
                "selected_task_indexes": [0, 2],
            },
        )
        assert publish.status_code == 200
        assert publish.json()["queued"] is True

        published = await _wait_until_published(client, space_id)
        goal_id = int(published["item"]["published_goal_id"])
        assert published["item"]["status"] == "published"
        assert published["publish_records"]
        publish_record = published["publish_records"][-1]
        assert len(publish_record["created_task_ids"]) == 2
        assert publish_record["deferred_tasks"]
        assert publish_record["deferred_tasks"][0]["title"] == deferred_task_title
        published_summary = next(
            item
            for item in published["resources"]
            if item["name"] == "Published Summary" and item["type"] == "summary"
        )
        assert "Published tasks" in published_summary["text_content"]
        assert "Rejected / deferred ideas" in published_summary["text_content"]
        assert deferred_task_title in published_summary["text_content"]
        assert any(
            item["name"] == "Published Summary" and item["type"] == "summary"
            for item in published["resources"]
        )

        goal_page = await client.get(f"/goals?goal={goal_id}")
        assert goal_page.status_code == 200
        assert "Blackbox publishable idea" in goal_page.text
        assert "Clarify the scope of Blackbox publishable idea" in goal_page.text
        assert "Review risks and open questions for Blackbox publishable idea" in (
            goal_page.text
        )
        assert deferred_task_title not in goal_page.text
        assert f'href="/inspirations/{space_id}"' in goal_page.text

        cannot_add = await client.post(
            f"/api/inspirations/{space_id}/resources",
            data={
                "type": "text",
                "name": "Late edit",
                "text_content": "Published spaces should be read-only.",
            },
        )
        assert cannot_add.status_code == 400

        cannot_close = await client.post(f"/api/inspirations/{space_id}/close")
        assert cannot_close.status_code == 400
        assert "Published spaces cannot be closed" in cannot_close.text

        fork = await client.post(
            f"/api/inspirations/{space_id}/fork",
            json={"title": "Blackbox publishable idea / follow-up"},
        )
        assert fork.status_code == 200
        fork_id = int(fork.json()["item"]["id"])

        fork_detail = await client.get(f"/api/inspirations/{fork_id}")
        assert fork_detail.status_code == 200
        assert fork_detail.json()["item"]["status"] == "open"
        assert any(
            item["type"] == "summary" for item in fork_detail.json()["resources"]
        )


@pytest.mark.anyio
async def test_unpublished_inspiration_close_reopen_and_delete_flow(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENFOCUS_ARK_API_KEY", raising=False)
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    from openfocus.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/api/inspirations", json={"title": "Blackbox parking lot"}
        )
        assert create.status_code == 200
        space_id = int(create.json()["item"]["id"])

        close = await client.post(f"/api/inspirations/{space_id}/close")
        assert close.status_code == 200
        assert close.json()["item"]["status"] == "closed"

        cannot_add = await client.post(
            f"/api/inspirations/{space_id}/resources",
            data={
                "type": "text",
                "name": "Closed edit",
                "text_content": "Closed spaces should reject resource edits.",
            },
        )
        assert cannot_add.status_code == 400

        reopen = await client.post(f"/api/inspirations/{space_id}/reopen")
        assert reopen.status_code == 200
        assert reopen.json()["item"]["status"] == "open"

        delete = await client.delete(f"/api/inspirations/{space_id}")
        assert delete.status_code == 200
        assert delete.json()["space_id"] == space_id

        missing = await client.get(f"/api/inspirations/{space_id}")
        assert missing.status_code == 404
