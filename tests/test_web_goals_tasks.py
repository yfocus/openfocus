from __future__ import annotations

import datetime as dt

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.anyio
async def test_goals_crud_and_task_flow(monkeypatch):
    from openfocus.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # list page
        r = await client.get("/goals")
        assert r.status_code == 200

        # create goal
        r = await client.post(
            "/goals",
            data={
                "content": "目标-单测",
                "description": "desc-必填",
                "due_date": (dt.date.today() + dt.timedelta(days=3)).isoformat(),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        # list contains goal
        r = await client.get("/goals")
        assert r.status_code == 200
        assert "目标-单测" in r.text

        # get latest goal id from DB
        from openfocus.db import session_scope
        from openfocus.models import Goal

        with session_scope() as s:
            goal_id = s.query(Goal).order_by(Goal.id.desc()).first().id

        # mark goal done
        r = await client.post(f"/goals/{goal_id}/done", follow_redirects=False)
        assert r.status_code == 303
        with session_scope() as s:
            g = s.get(Goal, goal_id)
            assert g is not None
            assert g.status == "done"

            # should create goal done event
            from openfocus.models import Event

            ev = (
                s.query(Event)
                .filter(Event.kind == "goal.confirmed_done_by_user")
                .order_by(Event.id.desc())
                .first()
            )
            assert ev is not None
            assert (ev.payload or {}).get("goal_id") == goal_id

        # goals page should include the human-facing label
        r = await client.get("/goals")
        assert r.status_code == 200
        assert "confirm done by user" in r.text

        # filter: completed should include the goal
        r = await client.get("/goals?gfilter=COMPLETED")
        assert r.status_code == 200
        assert "目标-单测" in r.text

        # filter: in progress should NOT include completed goal
        r = await client.get("/goals?gfilter=IN_PROGRESS")
        assert r.status_code == 200
        assert "目标-单测" not in r.text

        # reopen goal
        r = await client.post(f"/goals/{goal_id}/reopen", follow_redirects=False)
        assert r.status_code == 303
        with session_scope() as s:
            g = s.get(Goal, goal_id)
            assert g is not None
            assert g.status == "active"

            # should create goal reopen event
            from openfocus.models import Event

            ev2 = (
                s.query(Event)
                .filter(Event.kind == "goal.reopened_by_user")
                .order_by(Event.id.desc())
                .first()
            )
            assert ev2 is not None
            assert (ev2.payload or {}).get("goal_id") == goal_id

        # goals page should include the reopen label
        r = await client.get("/goals")
        assert r.status_code == 200
        assert "reopen by user" in r.text

        # goal detail
        r = await client.get(f"/goals/{goal_id}")
        assert r.status_code == 200
        assert "目标详情" in r.text

        # add task
        r = await client.post(
            f"/goals/{goal_id}/tasks",
            data={"title": "task-1", "description": "task desc"},
            follow_redirects=False,
        )
        assert r.status_code == 303

        from openfocus.models import Task

        with session_scope() as s:
            t = s.query(Task).filter(Task.goal_id == goal_id).order_by(Task.id.desc()).first()
            assert t.title == "task-1"
            assert t.description == "task desc"
            assert hasattr(t, "recommended_prompt") is False
            task_id = t.id
            task_public_id = t.public_id

        # edit task
        r = await client.post(
            f"/tasks/{task_id}/edit",
            data={"title": "task-1-edit", "description": "task desc edit"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        with session_scope() as s:
            t = s.get(Task, task_id)
            assert t is not None
            assert t.title == "task-1-edit"
            assert t.description == "task desc edit"

        # recommendations should include task
        r = await client.get("/api/recommendations/next?limit=5")
        assert r.status_code == 200
        data = r.json()
        items = data.get("items") or []
        assert any((it.get("title") == "task-1-edit") for it in items)

        # recent events should include something
        r = await client.get("/api/events/recent?limit=10")
        assert r.status_code == 200
        ev = r.json()
        assert isinstance(ev.get("items"), list)
        # 事件流是 best-effort：不要求每次 CRUD 都一定产生 events。

        # mark done
        r = await client.post(f"/tasks/{task_id}/done", follow_redirects=False)
        assert r.status_code == 303
        with session_scope() as s:
            t = s.get(Task, task_id)
            assert t.status == "done"
            assert t.completed_at is not None

        # reopen
        r = await client.post(f"/tasks/{task_id}/reopen", follow_redirects=False)
        assert r.status_code == 303
        with session_scope() as s:
            t = s.get(Task, task_id)
            assert t.status == "todo"
            assert t.completed_at is None

        # delete task
        r = await client.post(f"/tasks/{task_id}/delete", follow_redirects=False)
        assert r.status_code == 303
        with session_scope() as s:
            assert s.get(Task, task_id) is None

        # delete goal (should also delete tasks)
        r = await client.post(f"/goals/{goal_id}/delete", follow_redirects=False)
        assert r.status_code == 303


@pytest.mark.anyio
async def test_memory_page_and_save(monkeypatch, tmp_path):
    from openfocus.main import app

    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/memory")
        assert r.status_code == 200

        r = await client.post(
            "/memory/save",
            data={
                "user_card": "# User Card\n- likes: terminal ui\n",
                "user_memory": "# User Memory\n- prefers: fast feedback\n",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        r = await client.get("/memory")
        assert r.status_code == 200
        assert "User Card" in r.text
        assert "terminal ui" in r.text
        assert "User Memory" in r.text
        assert "fast feedback" in r.text
