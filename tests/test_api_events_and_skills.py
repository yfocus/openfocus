# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.anyio
async def test_agent_events_persist_but_not_mark_task_done_without_human_confirm():
    from openfocus.app import app
    from openfocus.db import session_scope
    from openfocus.models import AttentionItem, Event, Goal, Task

    # seed goal + task
    with session_scope() as s:
        g = Goal(title="g", content="", due_date=dt.date.today())
        s.add(g)
        s.flush()
        t = Task(goal_id=g.id, title="t", content="d", status="todo")
        s.add(t)
        s.flush()
        public_id = t.public_id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/agent/events",
            json={
                "kind": "task.completed",
                "agent": "coco",
                "task_id": public_id,
                "payload": {"percent": 100},
            },
        )
        assert r.status_code == 200

    with session_scope() as s:
        t2 = s.query(Task).filter(Task.public_id == public_id).one()
        assert t2.status == "todo"
        ev = s.query(Event).order_by(Event.id.desc()).first()
        assert ev is not None
        assert ev.task_id == public_id
        item = s.query(AttentionItem).one()
        assert item.task_public_id == public_id
        assert item.item_type == "completion_reported"
        assert item.status == "active"


@pytest.mark.anyio
async def test_focus_report_persist_but_not_mark_task_done_without_human_confirm():
    from openfocus.app import app
    from openfocus.db import session_scope
    from openfocus.models import AttentionItem, Event, Goal, Task

    with session_scope() as s:
        g = Goal(title="g2", content="", due_date=dt.date.today())
        s.add(g)
        s.flush()
        t = Task(goal_id=g.id, title="skill-task", content="d", status="todo")
        s.add(t)
        s.flush()
        public_id = t.public_id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/skills/focus_report",
            json={
                "agent": "trae",
                "task_name": "skill-task",
                "status": "succeeded",
                "goal_id": g.id,
                "task_public_id": public_id,
                "user_prompt": "do",
                "assistant_response": "done",
                "metadata": {"k": "v"},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["task_updated"] is None

    with session_scope() as s:
        t2 = s.query(Task).filter(Task.public_id == public_id).one()
        assert t2.status == "todo"
        ev = s.query(Event).order_by(Event.id.desc()).first()
        assert ev.kind == "skill.focus_report"
        item = s.query(AttentionItem).one()
        assert item.task_public_id == public_id
        assert item.item_type == "completion_reported"
        assert "pending confirmation" in item.summary


@pytest.mark.anyio
async def test_attention_api_dismiss_and_acted_state():
    from openfocus.app import app
    from openfocus.db import session_scope
    from openfocus.models import AgentSpace, AttentionItem, Goal, Task

    with session_scope() as s:
        g = Goal(title="attention goal", content="", due_date=dt.date.today())
        s.add(g)
        s.flush()
        t = Task(goal_id=g.id, title="attention task", content="d", status="todo")
        s.add(t)
        s.flush()
        public_id = t.public_id
        s.add(AgentSpace(task_public_id=public_id, root_path="/tmp/openfocus-test"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/agent/events",
            json={
                "kind": "task.completed",
                "agent": "coco",
                "task_id": public_id,
                "payload": {"message": "done"},
            },
        )
        assert r.status_code == 200
        r = await client.get("/api/attention/items")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        item = body["items"][0]
        assert item["has_agent_space"] is True
        assert item["action"]["primary_target"] == "agent_space"
        assert item["action"]["primary_url"] == f"/tasks/{public_id}/agent_space"

        r = await client.post(f"/api/attention/items/{item['id']}/dismiss")
        assert r.status_code == 200
        assert r.json()["status"] == "dismissed"
        r = await client.get("/api/attention/items")
        assert r.status_code == 200
        assert r.json()["count"] == 0

        r = await client.post(
            "/api/agent/events",
            json={
                "kind": "task.completed",
                "agent": "coco",
                "task_id": public_id,
                "payload": {"message": "done again"},
            },
        )
        assert r.status_code == 200
        r = await client.get("/api/attention/items")
        item = r.json()["items"][0]
        r = await client.post(f"/api/attention/items/{item['id']}/acted")
        assert r.status_code == 200
        assert r.json()["status"] == "acted"

    with session_scope() as s:
        assert (
            s.query(AttentionItem).filter(AttentionItem.status == "active").count() == 0
        )


@pytest.mark.anyio
async def test_attention_tracks_running_then_finished_buckets():
    from openfocus.app import app
    from openfocus.db import session_scope
    from openfocus.models import AttentionItem, Goal, Task

    with session_scope() as s:
        g = Goal(title="agent state goal", content="", due_date=dt.date.today())
        s.add(g)
        s.flush()
        t = Task(goal_id=g.id, title="agent state task", content="d", status="todo")
        s.add(t)
        s.flush()
        public_id = t.public_id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/agent/events",
            json={
                "kind": "task.started",
                "agent": "coco",
                "task_id": public_id,
                "payload": {"status": "running", "message": "working"},
            },
        )
        assert r.status_code == 200
        r = await client.get("/api/attention/items")
        body = r.json()
        assert body["count"] == 1
        assert len(body["buckets"]["running"]) == 1
        running = body["buckets"]["running"][0]
        assert running["type"] == "running"
        assert running["bucket"] == "running"
        assert running["state_since"]
        assert isinstance(running["state_age_seconds"], int)

        r = await client.post(
            "/api/agent/events",
            json={
                "kind": "task.progress",
                "agent": "coco",
                "task_id": public_id,
                "payload": {"status": "running", "message": "still working"},
            },
        )
        assert r.status_code == 200
        r = await client.get("/api/attention/items")
        body = r.json()
        assert body["count"] == 1
        assert len(body["buckets"]["running"]) == 1
        assert body["buckets"]["running"][0]["summary"].startswith("still working")

        r = await client.post(
            "/api/agent/events",
            json={
                "kind": "task.completed",
                "agent": "coco",
                "task_id": public_id,
                "payload": {"status": "succeeded", "summary": "done"},
            },
        )
        assert r.status_code == 200
        r = await client.get("/api/attention/items")
        body = r.json()
        assert len(body["buckets"]["running"]) == 0
        assert len(body["buckets"]["completed"]) == 1
        assert body["buckets"]["completed"][0]["type"] == "completion_reported"

    with session_scope() as s:
        running_items = (
            s.query(AttentionItem)
            .filter(AttentionItem.task_public_id == public_id)
            .filter(AttentionItem.item_type == "running")
            .all()
        )
        assert running_items
        assert all(item.status == "acted" for item in running_items)


def test_agent_completed_next_move_event_sink_creates_attention_item(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    from openfocus.agent.storage.events import DbEventSink
    from openfocus.db import session_scope
    from openfocus.models import AttentionItem, Event, Goal, Task

    with session_scope() as s:
        g = Goal(title="next move goal", content="", due_date=dt.date.today())
        s.add(g)
        s.flush()
        t = Task(goal_id=g.id, title="recommended task", content="d", status="todo")
        s.add(t)
        s.flush()
        public_id = t.public_id
        goal_id = int(g.id)

    DbEventSink().emit(
        "agent.completed",
        "attention_scheduler",
        {
            "goal_id": None,
            "result": {
                "items": [
                    {
                        "type": "do_task",
                        "target": {"goal_id": goal_id, "task_public_id": public_id},
                        "goal_title": "next move goal",
                        "title": "recommended task",
                        "why": ["This is the best next move now."],
                    }
                ],
                "context_summary": {"candidate_count": 1},
            },
        },
    )

    with session_scope() as s:
        ev = s.query(Event).filter(Event.kind == "agent.completed").one()
        assert ev.agent == "attention_scheduler"
        item = s.query(AttentionItem).one()
        assert item.source_event_id == ev.id
        assert item.task_public_id == public_id
        assert item.item_type == "next_move"
        assert item.severity == "info"
        assert item.status == "active"
        assert "best next move" in item.summary

    audit_files = list(Path(tmp_path / "memory" / "audit").glob("**/*.md"))
    assert audit_files
    audit_text = audit_files[0].read_text(encoding="utf-8")
    assert "agent.completed" in audit_text


@pytest.mark.anyio
async def test_agent_and_skill_events_write_audit_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    from openfocus.app import app
    from openfocus.db import session_scope
    from openfocus.models import Goal, Task

    with session_scope() as s:
        g = Goal(title="g3", content="d", due_date=dt.date.today())
        s.add(g)
        s.flush()
        t = Task(goal_id=g.id, title="task-audit", content="d", status="todo")
        s.add(t)
        s.flush()
        public_id = t.public_id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/agent/events",
            json={
                "kind": "task.progress",
                "agent": "trae",
                "task_id": public_id,
                "payload": {"message": "working"},
            },
        )
        assert r.status_code == 200

        r = await client.post(
            "/api/skills/focus_report",
            json={
                "agent": "trae",
                "task_name": "task-audit",
                "status": "running",
                "goal_id": g.id,
                "task_public_id": public_id,
                "user_prompt": "do",
                "assistant_response": "doing",
                "metadata": {"step": 1},
            },
        )
        assert r.status_code == 200

    audit_files = list(Path(tmp_path / "memory" / "audit").glob("**/*.md"))
    assert audit_files
    audit_text = audit_files[0].read_text(encoding="utf-8")
    assert "task.progress" in audit_text
    assert "skill.focus_report" in audit_text


def test_agent_space_prompt_injects_event_spec():
    from openfocus.web.routes.agent_spaces import _inject_openfocus_prompt

    prompt = _inject_openfocus_prompt(
        base_url="http://127.0.0.1:8001",
        task_public_id="task_123",
        session_id="session_abc",
        user_prompt="do the work",
    )

    assert "taskId=task_123" in prompt
    assert "openfocusBaseUrl=http://127.0.0.1:8001" in prompt
    assert "POST /api/agent/events" in prompt
    assert "agent.started" in prompt
    assert "task.started" in prompt
    assert "task.progress" in prompt
    assert "task.completed" in prompt
    assert "task.failed" in prompt
    assert "task.blocked" in prompt
    assert "agent.completed" in prompt
    assert "status 合法值按 spec 使用" in prompt
    assert "focus_report.status 合法值按 spec 归一化" in prompt
    assert "Agent 一启动，立刻先上报 agent.started" in prompt
    assert "完成时必须上报 agent.completed" in prompt
    assert "POST /api/skills/focus_report" in prompt
    assert "task_public_id" in prompt
    assert "不要按 token/日志行/无意义心跳刷屏" in prompt
    assert prompt.endswith("do the work")


def test_agent_space_ttyd_prefix_injects_event_spec():
    from openfocus.web.routes.agent_spaces import _build_openfocus_ttyd_agent_prefix

    prefix = _build_openfocus_ttyd_agent_prefix(
        base_url="http://127.0.0.1:8001",
        task_public_id="task_123",
    )

    assert "taskId=task_123" in prefix
    assert "openfocusBaseUrl=http://127.0.0.1:8001" in prefix
    assert "POST http://127.0.0.1:8001/api/agent/events" in prefix
    assert "task.started" in prefix
    assert "task.progress" in prefix
    assert "task.completed" in prefix
    assert "task.failed" in prefix
    assert "task.blocked" in prefix
    assert "agent.started" in prefix
    assert "agent.completed" in prefix
    assert "status 按 spec 使用" in prefix
    assert "POST http://127.0.0.1:8001/api/skills/focus_report" in prefix
    assert "task_public_id" in prefix
    assert "不要按 token/日志行/无意义心跳刷屏" in prefix
