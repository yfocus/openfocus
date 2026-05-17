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
    from openfocus.models import AttentionItem, Event, Goal, Task, TaskAgentActivity

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
        assert s.query(AttentionItem).count() == 0
        assert s.query(TaskAgentActivity).count() == 0


@pytest.mark.anyio
async def test_focus_report_persist_but_not_mark_task_done_without_human_confirm():
    from openfocus.app import app
    from openfocus.db import session_scope
    from openfocus.models import AttentionItem, Event, Goal, Task, TaskAgentActivity

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
        assert s.query(AttentionItem).count() == 0
        assert s.query(TaskAgentActivity).count() == 0


@pytest.mark.anyio
async def test_attention_api_dismiss_and_acted_state():
    from openfocus.app import app
    from openfocus.db import session_scope
    from openfocus.domains.events import service as event_service
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
        event_service.record_event(
            s,
            kind="agent.completed",
            agent="attention_scheduler",
            payload={
                "result": {
                    "items": [
                        {
                            "target": {"task_public_id": public_id},
                            "title": "next move",
                            "why": ["do this next"],
                        }
                    ]
                }
            },
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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

        with session_scope() as s:
            event_service.record_event(
                s,
                kind="agent.completed",
                agent="attention_scheduler",
                payload={
                    "result": {
                        "items": [
                            {
                                "target": {"task_public_id": public_id},
                                "title": "next move again",
                                "why": ["do this next again"],
                            }
                        ]
                    }
                },
            )
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
async def test_runtime_activity_tracks_running_waiting_then_review_ready():
    from openfocus.app import app
    from openfocus.db import session_scope
    from openfocus.domains.agent_activity import service as agent_activity_service
    from openfocus.models import AgentTurn, Goal, Task, TaskAgentActivity

    with session_scope() as s:
        g = Goal(title="agent state goal", content="", due_date=dt.date.today())
        s.add(g)
        s.flush()
        t = Task(goal_id=g.id, title="agent state task", content="d", status="todo")
        s.add(t)
        s.flush()
        public_id = t.public_id

    def emit(kind: str, payload: dict) -> None:
        with session_scope() as s:
            result = agent_activity_service.handle_runtime_signal(
                s,
                kind=kind,
                agent_runtime="coco",
                turn_id="turn-1",
                task_public_id=public_id,
                source="test",
                payload=payload,
            )
            assert result["ok"] is True

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        emit("runtime.turn.started", {"message": "working"})
        r = await client.get("/api/agent_activity/summary")
        body = r.json()
        assert body["count"] == 1
        assert len(body["buckets"]["running"]) == 1
        running = body["buckets"]["running"][0]
        assert running["type"] == "running"
        assert running["bucket"] == "running"
        assert running["agent_runtime"] == "coco"
        assert running["agent_name"] == "coco"
        assert running["state_since"]
        assert isinstance(running["state_age_seconds"], int)

        emit("runtime.turn.waiting_for_approval", {"message": "needs approval"})
        r = await client.get("/api/agent_activity/summary")
        body = r.json()
        assert body["count"] == 1
        assert len(body["buckets"]["running"]) == 0
        assert len(body["buckets"]["waiting"]) == 1
        assert body["buckets"]["waiting"][0]["type"] == "waiting"
        assert body["buckets"]["waiting"][0]["waiting_kind"].endswith("approval")

        emit("runtime.turn.completed", {"status": "succeeded", "summary": "done"})
        r = await client.get("/api/agent_activity/summary")
        body = r.json()
        assert len(body["buckets"]["running"]) == 0
        assert len(body["buckets"]["waiting"]) == 1
        assert body["buckets"]["waiting"][0]["type"] == "review_ready"

    with session_scope() as s:
        turn = s.query(AgentTurn).filter(AgentTurn.turn_id == "turn-1").one()
        assert turn.state == "completed"
        activity = (
            s.query(TaskAgentActivity)
            .filter(TaskAgentActivity.task_public_id == public_id)
            .one()
        )
        assert activity.state == "review_ready"


@pytest.mark.anyio
async def test_runtime_session_start_does_not_create_activity_and_can_correlate_turn():
    from openfocus.app import app
    from openfocus.db import session_scope
    from openfocus.domains.agent_activity import service as agent_activity_service
    from openfocus.models import AgentRuntimeSession, Goal, Task, TaskAgentActivity

    with session_scope() as s:
        g = Goal(title="runtime session goal", content="", due_date=dt.date.today())
        s.add(g)
        s.flush()
        t = Task(goal_id=g.id, title="runtime session task", content="d", status="todo")
        s.add(t)
        s.flush()
        public_id = t.public_id

    with session_scope() as s:
        result = agent_activity_service.handle_runtime_signal(
            s,
            raw_kind="SessionStart",
            agent_runtime="codex",
            session_id="sess-rt-1",
            task_public_id=public_id,
            source="test",
            payload={"message": "attached"},
        )
        assert result["state"] == "ignored_for_activity"
        assert s.query(TaskAgentActivity).count() == 0
        sess = (
            s.query(AgentRuntimeSession)
            .filter(AgentRuntimeSession.session_id == "sess-rt-1")
            .one()
        )
        assert sess.state == "idle"
        assert sess.task_public_id == public_id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with session_scope() as s:
            result = agent_activity_service.handle_runtime_signal(
                s,
                raw_kind="UserPromptSubmit",
                agent_runtime="codex",
                session_id="sess-rt-1",
                source="test",
                payload={"message": "work on it"},
            )
            assert result["activity_state"] == "running"

        r = await client.get("/api/agent_activity/summary")
        body = r.json()
        assert body["counts"]["running"] == 1
        assert body["buckets"]["running"][0]["task_public_id"] == public_id


def test_runtime_activity_signal_without_active_turn_stays_journal_only():
    from openfocus.db import session_scope
    from openfocus.domains.agent_activity import service as agent_activity_service
    from openfocus.models import AgentTurn, Event, Goal, Task, TaskAgentActivity

    with session_scope() as s:
        g = Goal(title="activity goal", content="", due_date=dt.date.today())
        s.add(g)
        s.flush()
        t = Task(goal_id=g.id, title="activity task", content="d", status="todo")
        s.add(t)
        s.flush()
        result = agent_activity_service.handle_runtime_signal(
            s,
            raw_kind="PreToolUse",
            agent_runtime="coco",
            task_public_id=t.public_id,
            source="test",
            payload={"message": "tool starting"},
        )
        assert result["state"] == "activity_without_turn"
        assert s.query(AgentTurn).count() == 0
        assert s.query(TaskAgentActivity).count() == 0
        assert s.query(Event).filter(Event.kind == "runtime.turn.activity").count() == 1


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
    assert "task.progress" in prompt
    assert "步骤启动或完成" in prompt
    assert "每约 5 分钟同步一次进展" in prompt
    assert "不要为了 agent 启动、任务启动、任务结束、成功或失败而上报" in prompt
    assert "agent.started" not in prompt
    assert "task.started" not in prompt
    assert "task.completed" not in prompt
    assert "task.failed" not in prompt
    assert "agent.completed" not in prompt
    assert "status 合法值按 spec 使用" in prompt
    assert "POST /api/skills/focus_report" not in prompt
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
    assert "task.progress" in prefix
    assert "某个步骤启动/完成" in prefix
    assert "每约 5 分钟同步一次进展" in prefix
    assert "不要为了 agent 启动、任务启动、任务结束、成功或失败而上报" in prefix
    assert "task.started" not in prefix
    assert "task.completed" not in prefix
    assert "task.failed" not in prefix
    assert "task.blocked" not in prefix
    assert "agent.started" not in prefix
    assert "agent.completed" not in prefix
    assert "POST http://127.0.0.1:8001/api/skills/focus_report" not in prefix
    assert "不要按 token/日志行/无意义心跳刷屏" in prefix
