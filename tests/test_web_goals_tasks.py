# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json
import re

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.anyio
async def test_goals_crud_and_task_flow(monkeypatch, tmp_path):
    import openfocus.main as main_mod
    from openfocus.agent.llm.types import LLMCallResult

    app = main_mod.app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # list page
        r = await client.get("/goals")
        assert r.status_code == 200

        # create goal
        r = await client.post(
            "/goals",
            data={
                "title": "目标-单测",
                "content": "desc-必填",
                "due_date": (dt.date.today() + dt.timedelta(days=3)).isoformat(),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        # list contains goal
        r = await client.get("/goals")
        assert r.status_code == 200
        assert "目标-单测" in r.text
        assert '<option value="not_for_now">Not for now</option>' in r.text

        # get latest goal id from DB
        from openfocus.db import session_scope
        from openfocus.models import Goal

        with session_scope() as s:
            goal = s.query(Goal).order_by(Goal.id.desc()).first()
            assert goal is not None
            goal.source_inspiration_space_id = 42
            goal_id = goal.id

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
        assert "Goal confirmed done by user" in r.text

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
        assert "Goal reopened by user" in r.text

        # dashboard detail view
        r = await client.get(f"/goals?goal={goal_id}")
        assert r.status_code == 200
        assert "目标-单测" in r.text
        assert 'href="/inspirations/42"' in r.text
        assert ">Inspiration</a>" in r.text

        # add task
        r = await client.post(
            f"/goals/{goal_id}/tasks",
            data={"title": "task-1", "content": "task desc"},
            follow_redirects=False,
        )
        assert r.status_code == 303

        from openfocus.models import Task

        with session_scope() as s:
            t = (
                s.query(Task)
                .filter(Task.goal_id == goal_id)
                .order_by(Task.id.desc())
                .first()
            )
            assert t.title == "task-1"
            assert t.content == "task desc"
            assert hasattr(t, "recommended_prompt") is False
            task_id = t.id
            task_public_id = t.public_id

        # edit task
        r = await client.post(
            f"/tasks/{task_id}/edit",
            data={"title": "task-1-edit", "content": "task desc edit"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        with session_scope() as s:
            t = s.get(Task, task_id)
            assert t is not None
            assert t.title == "task-1-edit"
            assert t.content == "task desc edit"

        mem_dir = tmp_path / "memory"
        daily_dir = mem_dir / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / "MEMORY.md").write_text(
            "Long memory: prefer continuity and protect attention.", encoding="utf-8"
        )
        (daily_dir / "2026-05-12.md").write_text(
            "Daily memory: current focus is OpenFocus refactor.", encoding="utf-8"
        )
        monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(mem_dir))

        llm_calls = []

        class FakeNextMoveProvider:
            def chat_completions(self, **kwargs):
                llm_calls.append(kwargs)
                messages = kwargs.get("messages") or []
                system_text = str(messages[0].get("content") or "")
                user_text = str(messages[1].get("content") or "")
                assert "最多推荐 2 个" in system_text
                assert "前额叶" in system_text
                assert "工作记忆容量有限" in system_text
                assert "Long memory: prefer continuity" in user_text
                assert "list_daily_memory_files" in user_text
                assert "read_daily_memory_file" in user_text
                assert "recent_events_latest_100" in user_text
                assert "list_recent_events" in user_text
                assert "open_goals_and_tasks" in user_text
                assert "completed_last_7_days" in user_text
                return LLMCallResult(
                    content=json.dumps(
                        {
                            "recommendations": [
                                {
                                    "task_public_id": task_public_id,
                                    "goal_id": goal_id,
                                    "reason": "Continue the active refactor while context is warm.",
                                    "why": [
                                        "Keeps the current context warm.",
                                        "Reduces decision fatigue by choosing one next step.",
                                    ],
                                    "confidence": "high",
                                    "context_switch_cost": "low",
                                }
                            ],
                            "no_recommendation_reason": None,
                        }
                    ),
                    finish_reason="stop",
                    usage={},
                    tool_calls=None,
                )

        monkeypatch.setattr(
            main_mod,
            "_get_llm_provider_or_error",
            lambda: (FakeNextMoveProvider(), None),
        )

        # recommendations should include the LLM/agent-loop selected task and timestamp metadata
        r = await client.get("/api/recommendations/next?limit=5")
        assert r.status_code == 200
        assert "no-store" in (r.headers.get("cache-control") or "")
        data = r.json()
        items = data.get("items") or []
        assert len(items) == 1
        assert items[0].get("title") == "task-1-edit"
        assert (
            items[0].get("reason")
            == "Continue the active refactor while context is warm."
        )
        assert data.get("item") == items[0]
        assert data.get("generated_at")
        assert "last_event_id" in data
        assert len(llm_calls) == 1

        r = await client.get("/api/recommendations/latest")
        assert r.status_code == 200
        latest = r.json()
        assert latest.get("run_id") == data.get("run_id")
        assert latest.get("generated_at") == data.get("generated_at")
        assert latest.get("items") == data.get("items")

        run_id = int(data.get("run_id") or 0)
        assert run_id > 0

        # Not for now feedback should persist both feedback and an event used by the next run.
        r = await client.post(
            "/api/recommendations/feedback",
            json={
                "run_id": run_id,
                "task_public_id": task_public_id,
                "feedback_type": "dismiss",
                "reason_code": "not_for_now",
                "reason_text": "Need a smaller step first.",
            },
        )
        assert r.status_code == 200
        from openfocus.models import Event, NextMoveFeedback

        with session_scope() as s:
            fb = (
                s.query(NextMoveFeedback)
                .filter(NextMoveFeedback.task_public_id == task_public_id)
                .order_by(NextMoveFeedback.id.desc())
                .first()
            )
            assert fb is not None
            assert fb.reason_code == "not_for_now"
            ev = (
                s.query(Event)
                .filter(Event.kind == "next_move.not_for_now")
                .order_by(Event.id.desc())
                .first()
            )
            assert ev is not None
            assert ev.task_id == task_public_id
            assert (ev.payload or {}).get("reason_code") == "not_for_now"

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
        assert not any(
            (it.get("target") or {}).get("task_public_id") == task_public_id
            for it in (data.get("items") or [])
        )

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
                "title": "过期目标",
                "content": "需要调整DDL",
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
            data={"title": "等待处理", "content": "先挂起，不推进"},
            follow_redirects=False,
        )
        assert r.status_code == 303

        r = await client.get(f"/goals?goal={goal_id}")
        assert r.status_code == 200
        assert "status-dot red" in r.text

        r = await client.post(
            f"/goals/{goal_id}/edit",
            data={
                "title": "过期目标",
                "content": "需要调整DDL",
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
        assert "status-dot green" in r.text
        assert "status-dot red" not in r.text


@pytest.mark.anyio
async def test_dashboard_goal_detail_tasks_default_order_and_sort_controls(monkeypatch):
    from openfocus.db import session_scope
    from openfocus.main import app
    from openfocus.models import Event, Goal, Task

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/goals",
            data={
                "title": "任务排序目标",
                "content": "验证 dashboard goal detail 的默认排序与表头排序能力",
                "due_date": (dt.date.today() + dt.timedelta(days=7)).isoformat(),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        with session_scope() as s:
            goal = s.query(Goal).order_by(Goal.id.desc()).first()
            assert goal is not None
            goal_id = goal.id

        task_titles = ["todo-old", "todo-new", "doing-mid", "done-last"]
        for title in task_titles:
            r = await client.post(
                f"/goals/{goal_id}/tasks",
                data={"title": title, "content": f"desc for {title}"},
                follow_redirects=False,
            )
            assert r.status_code == 303

        now = dt.datetime.now(dt.timezone.utc)
        with session_scope() as s:
            rows = {
                t.title: t for t in s.query(Task).filter(Task.goal_id == goal_id).all()
            }
            rows["todo-old"].created_at = now - dt.timedelta(days=3)
            rows["todo-new"].created_at = now - dt.timedelta(days=1)
            rows["doing-mid"].created_at = now - dt.timedelta(days=2)
            rows["done-last"].created_at = now
            rows["done-last"].status = "done"
            rows["done-last"].completed_at = now
            s.add(
                Event(
                    kind="task.started",
                    agent="test",
                    task_id=rows["doing-mid"].public_id,
                    payload={"task_public_id": rows["doing-mid"].public_id},
                )
            )

        r = await client.get(f"/goals?goal={goal_id}")
        assert r.status_code == 200
        assert ">↻<" in r.text
        assert ">ref<" not in r.text

        matched = re.search(
            rf'<template id="detail-goal-{goal_id}">(?P<html>.*?)</template>',
            r.text,
            re.S,
        )
        assert matched is not None
        html = matched.group("html")

        assert 'data-sort-key="title"' in html
        assert 'data-sort-key="created-at"' in html
        assert 'data-sort-key="status"' in html

        order = [
            html.index("doing-mid"),
            html.index("todo-new"),
            html.index("todo-old"),
            html.index("done-last"),
        ]
        assert order == sorted(order)


@pytest.mark.anyio
async def test_next_move_returns_two_tasks_and_learns_feedback(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    import openfocus.main as main_mod
    from openfocus.agent.llm.types import LLMCallResult
    from openfocus.db import session_scope
    from openfocus.models import Goal, NextMoveFeedback, Task

    app = main_mod.app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/goals",
            data={
                "title": "Next Move 测试目标",
                "content": "验证三条推荐与反馈学习",
                "due_date": (dt.date.today() + dt.timedelta(days=1)).isoformat(),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        with session_scope() as s:
            goal = s.query(Goal).order_by(Goal.id.desc()).first()
            assert goal is not None
            goal_id = goal.id

        tasks = [
            ("Deep analysis refactor", "Need design + refactor for one module."),
            ("Review PR comments", "Review and reply to comments quickly."),
            ("Reply stakeholder message", "Send a short update message."),
            ("Document cleanup", "Cleanup docs and organize notes."),
        ]
        for title, description in tasks:
            r = await client.post(
                f"/goals/{goal_id}/tasks",
                data={"title": title, "content": description},
                follow_redirects=False,
            )
            assert r.status_code == 303

        with session_scope() as s:
            task_rows = (
                s.query(Task)
                .filter(Task.goal_id == goal_id)
                .order_by(Task.id.asc())
                .all()
            )
            first_pid = task_rows[0].public_id
            second_pid = task_rows[1].public_id
            third_pid = task_rows[2].public_id

        calls = []

        class FakeNextMoveProvider:
            def chat_completions(self, **kwargs):
                calls.append(kwargs)
                messages = kwargs.get("messages") or []
                user_text = str(messages[1].get("content") or "")
                if len(calls) == 1:
                    assert "recent_events_latest_100" in user_text
                    assert "open_goals_and_tasks" in user_text
                    recs = [
                        (
                            first_pid,
                            "Keep momentum on the most demanding refactor while context is loaded.",
                        ),
                        (
                            second_pid,
                            "Keep review work available as the next fallback option.",
                        ),
                    ]
                else:
                    assert "next_move.not_for_now" in user_text
                    assert "现在只想先做短任务" in user_text
                    recs = [
                        (
                            second_pid,
                            "Switch to review because the previous deep task was dismissed for now.",
                        ),
                        (
                            third_pid,
                            "Keep a short communication task as backup while the next refresh settles.",
                        ),
                    ]
                return LLMCallResult(
                    content=json.dumps(
                        {
                            "recommendations": [
                                {
                                    "task_public_id": pid,
                                    "goal_id": goal_id,
                                    "reason": reason,
                                    "why": [reason],
                                    "confidence": "medium",
                                    "context_switch_cost": "low",
                                }
                                for pid, reason in recs
                            ],
                            "no_recommendation_reason": None,
                        }
                    ),
                    finish_reason="stop",
                    usage={},
                    tool_calls=None,
                )

        monkeypatch.setattr(
            main_mod,
            "_get_llm_provider_or_error",
            lambda: (FakeNextMoveProvider(), None),
        )

        r = await client.get("/api/recommendations/next?limit=3&trigger=manual_refresh")
        assert r.status_code == 200
        data = r.json()
        items = data.get("items") or []
        assert len(items) == 2
        assert data.get("run_id")
        assert data.get("generated_at")
        assert "last_event_id" in data
        top = items[0]
        assert top.get("task_type")
        assert top.get("task_type_label")
        assert top.get("expected_time_minutes")
        assert top.get("context_switch_cost") in {"low", "medium", "high"}
        dismissed_pid = (top.get("target") or {}).get("task_public_id")
        assert dismissed_pid

        r = await client.post(
            "/api/recommendations/feedback",
            json={
                "run_id": data.get("run_id"),
                "task_public_id": dismissed_pid,
                "feedback_type": "dismiss",
                "reason_code": "too_long",
                "reason_text": "现在只想先做短任务",
            },
        )
        assert r.status_code == 200
        payload = r.json()
        assert payload.get("ok") is True

        with session_scope() as s:
            fb = s.query(NextMoveFeedback).order_by(NextMoveFeedback.id.desc()).first()
            assert fb is not None
            assert fb.task_public_id == dismissed_pid
            assert fb.reason_code == "too_long"

        r = await client.get(
            "/api/recommendations/next?limit=3&trigger=feedback_submitted"
        )
        assert r.status_code == 200
        data2 = r.json()
        items2 = data2.get("items") or []
        assert len(items2) == 2
        assert ((items2[0].get("target") or {}).get("task_public_id")) != dismissed_pid

        daily_path = tmp_path / "memory" / "daily" / f"{dt.date.today().isoformat()}.md"
        assert daily_path.exists()
        daily_text = daily_path.read_text(encoding="utf-8")
        assert "Next Move Feedback" in daily_text
        assert "现在只想先做短任务" in daily_text


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
        assert (
            'href="/goals" class="btn-ghost action-link" role="button">Dashboard</a>'
            not in r.text
        )

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

    from openfocus.domains.memory import service as memory_service
    from openfocus.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/goals",
            data={
                "title": "memory pipeline test goal",
                "content": "needs to trigger audit rotation",
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
            data={"title": "task-memory", "content": "task desc"},
            follow_redirects=False,
        )
        assert r.status_code == 303

        memory_service.maintenance(
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)
        )

        mem_dir = memory_service.memory_dir()
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

    from openfocus.domains.memory import service as memory_service
    from openfocus.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/goals",
            data={
                "title": "manual audit summary goal",
                "content": "create one audit file first",
                "due_date": (dt.date.today() + dt.timedelta(days=1)).isoformat(),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303

        mem_dir = memory_service.memory_dir()
        before_files = sorted((mem_dir / "audit").glob("**/*.md"))
        assert len(before_files) == 1

        r = await client.post("/memory/audit/summary", follow_redirects=False)
        assert r.status_code == 303

        after_files = sorted((mem_dir / "audit").glob("**/*.md"))
        assert len(after_files) == 2
        assert any(path.stat().st_size > 0 for path in after_files)

        daily_files = sorted((mem_dir / "daily").glob("*.md"))
        assert daily_files
        assert "manual audit summary goal" in daily_files[0].read_text(
            encoding="utf-8"
        ) or "Created goal" in daily_files[0].read_text(encoding="utf-8")

        r = await client.get("/memory?tab=audit")
        assert r.status_code == 200
        assert 'class="status-dot green"' in r.text
        assert 'class="status-dot red"' in r.text
        assert "Current" in r.text
