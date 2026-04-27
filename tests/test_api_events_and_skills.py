from __future__ import annotations

import datetime as dt

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.anyio
async def test_agent_events_persist_but_not_mark_task_done_without_human_confirm():
    from openfocus.main import app
    from openfocus.db import session_scope
    from openfocus.models import Event, Goal, Task

    # seed goal + task
    with session_scope() as s:
        g = Goal(content="g", description="", due_date=dt.date.today())
        s.add(g)
        s.flush()
        t = Task(goal_id=g.id, title="t", description="d", status="todo")
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


@pytest.mark.anyio
async def test_focus_report_persist_but_not_mark_task_done_without_human_confirm():
    from openfocus.main import app
    from openfocus.db import session_scope
    from openfocus.models import Goal, Event, Task

    with session_scope() as s:
        g = Goal(content="g2", description="", due_date=dt.date.today())
        s.add(g)
        s.flush()
        t = Task(goal_id=g.id, title="skill-task", description="d", status="todo")
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
