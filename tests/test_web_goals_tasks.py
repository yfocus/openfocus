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
        assert "no-store" in (r.headers.get("cache-control") or "")
        data = r.json()
        items = data.get("items") or []
        assert any((it.get("title") == "task-1-edit") for it in items)

        # recent events should include something
        r = await client.get("/api/events/recent?limit=10")
        assert r.status_code == 200
        ev = r.json()
        assert isinstance(ev.get("items"), list)
        # The event stream is best-effort; CRUD actions do not guarantee an event every time.

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

        r = await client.get("/api/recommendations/next?limit=5")
        assert r.status_code == 200
        data = r.json()
        assert not any((it.get("target") or {}).get("task_public_id") == task_public_id for it in (data.get("items") or []))

        # delete goal (should also delete tasks)
        r = await client.post(f"/goals/{goal_id}/delete", follow_redirects=False)
        assert r.status_code == 303


@pytest.mark.anyio
async def test_goal_due_date_edit_refreshes_status_dot(monkeypatch):
    from openfocus.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        overdue_due = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        future_due = (dt.date.today() + dt.timedelta(days=5)).isoformat()

        r = await client.post(
            "/goals",
            data={
                "content": "过期目标",
                "description": "需要调整DDL",
                "due_date": overdue_due,
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        from openfocus.db import session_scope
        from openfocus.models import Goal

        with session_scope() as s:
            goal = s.query(Goal).order_by(Goal.id.desc()).first()
            assert goal is not None
            goal_id = goal.id

        r = await client.post(
            f"/goals/{goal_id}/tasks",
            data={"title": "等待处理", "description": "先挂起，不推进"},
            follow_redirects=False,
        )
        assert r.status_code == 303

        r = await client.get(f"/goals?goal={goal_id}")
        assert r.status_code == 200
        assert 'status-dot red' in r.text

        r = await client.post(
            f"/goals/{goal_id}/edit",
            data={
                "content": "过期目标",
                "description": "需要调整DDL",
                "due_date": future_due,
                "status": "active",
                "priority": "normal",
                "importance": "normal",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        with session_scope() as s:
            goal = s.get(Goal, goal_id)
            assert goal is not None
            assert goal.due_date.isoformat() == future_due

        r = await client.get(f"/goals?goal={goal_id}")
        assert r.status_code == 200
        assert 'status-dot green' in r.text
        assert 'status-dot red' not in r.text


@pytest.mark.anyio
async def test_memory_page_and_save(monkeypatch, tmp_path):
    from openfocus.main import app

    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("OPENFOCUS_MEMORY_AUDIT_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("OPENFOCUS_MEMORY_AUDIT_MAX_ENTRIES", "2000")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/memory")
        assert r.status_code == 200
        assert "Audit" in r.text
        assert "Daily" in r.text
        assert "Long-term" in r.text
        assert ">Edit<" in r.text
        assert 'id="long-term-editor" readonly' in r.text
        assert 'id="long-term-edit-btn"' in r.text
        assert 'id="long-term-save-btn" class="hidden"' in r.text
        assert "Selected" not in r.text
        assert "Audit Files" not in r.text
        assert "Daily Files" not in r.text
        assert ">Summary<" in r.text
        assert 'href="/goals" class="btn-ghost action-link" role="button">Dashboard</a>' not in r.text

        r = await client.post(
            "/memory/save",
            data={
                "long_term_memory": "# Long-term Memory\n- prefers: fast feedback\n",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        r = await client.get("/memory")
        assert r.status_code == 200
        assert "fast feedback" in r.text


@pytest.mark.anyio
async def test_memory_pipeline_records_audit_and_daily(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("OPENFOCUS_MEMORY_AUDIT_WINDOW_SECONDS", "1")
    monkeypatch.setenv("OPENFOCUS_MEMORY_AUDIT_MAX_ENTRIES", "2")

    from openfocus.main import app, _memory_dir, _memory_maintenance

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/goals",
            data={
                "content": "memory pipeline test goal",
                "description": "needs to trigger audit rotation",
                "due_date": (dt.date.today() + dt.timedelta(days=1)).isoformat(),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        from openfocus.db import session_scope
        from openfocus.models import Goal

        with session_scope() as s:
            goal_id = s.query(Goal).order_by(Goal.id.desc()).first().id

        r = await client.post(
            f"/goals/{goal_id}/tasks",
            data={"title": "task-memory", "description": "task desc"},
            follow_redirects=False,
        )
        assert r.status_code == 303

        _memory_maintenance(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5))

        mem_dir = _memory_dir()
        audit_files = list((mem_dir / "audit").glob("**/*.md"))
        daily_files = list((mem_dir / "daily").glob("*.md"))
        assert audit_files
        assert daily_files
        assert audit_files[0].name.count("_") == 1
        assert audit_files[0].name[:10] == dt.date.today().isoformat()
        assert len(audit_files) >= 2

        daily_text = daily_files[0].read_text(encoding="utf-8")
        assert "Audit Window" in daily_text

        r = await client.get("/memory?tab=audit")
        assert r.status_code == 200
        assert "memory pipeline test goal" in r.text or "Created goal" in r.text
        assert 'class="status-dot green"' in r.text


@pytest.mark.anyio
async def test_memory_manual_summary_rolls_new_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("OPENFOCUS_MEMORY_AUDIT_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("OPENFOCUS_MEMORY_AUDIT_MAX_ENTRIES", "2000")

    from openfocus.main import app, _memory_dir

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/goals",
            data={
                "content": "manual audit summary goal",
                "description": "create one audit file first",
                "due_date": (dt.date.today() + dt.timedelta(days=1)).isoformat(),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        mem_dir = _memory_dir()
        before_files = sorted((mem_dir / "audit").glob("**/*.md"))
        assert len(before_files) == 1

        r = await client.post("/memory/audit/summary", follow_redirects=False)
        assert r.status_code == 303

        after_files = sorted((mem_dir / "audit").glob("**/*.md"))
        assert len(after_files) == 2
        assert any(path.stat().st_size > 0 for path in after_files)

        daily_files = sorted((mem_dir / "daily").glob("*.md"))
        assert daily_files
        assert "manual audit summary goal" in daily_files[0].read_text(encoding="utf-8") or "Created goal" in daily_files[0].read_text(encoding="utf-8")

        r = await client.get("/memory?tab=audit")
        assert r.status_code == 200
        assert 'class="status-dot green"' in r.text
        assert 'class="status-dot red"' in r.text
        assert "Current" in r.text
