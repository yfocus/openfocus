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

        # recommendations should include task
        r = await client.get("/api/recommendations/next?limit=5")
        assert r.status_code == 200
        data = r.json()
        items = data.get("items") or []
        assert any((it.get("title") == "task-1") for it in items)

        # on-demand generate recommended prompt (must call agent loop via provider)
        import re

        from openfocus.agent.llm.types import LLMCallResult

        class FakeProvider:
            def __init__(self):
                self.calls = []

            def chat_completions(self, *, messages, temperature, max_tokens, tools=None, response_format=None):
                self.calls.append(
                    {
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "tools": tools,
                        "response_format": response_format,
                    }
                )
                user = "\n".join([m.get("content") or "" for m in messages if m.get("role") == "user"])
                m = re.search(r"taskId:\s*([0-9a-fA-F\-]{36})", user)
                tid = (m.group(1) if m else "")
                content = (
                    '{"prompt": "任务执行提示。taskId=' + tid + '\\n'
                    + '请定期上报到 /api/agent/events 并在结束时上报 /api/skills/focus_report"}'
                )
                return LLMCallResult(content=content, finish_reason="stop", usage={"total_tokens": 1}, tool_calls=None)

        fake = FakeProvider()
        import openfocus.main as main

        monkeypatch.setattr(main, "_get_llm_provider_or_error", lambda: (fake, None))

        r = await client.get(f"/api/tasks/{task_public_id}/recommended_prompt")
        assert r.status_code == 200
        body = r.json()
        assert body["task_public_id"] == task_public_id
        assert task_public_id in body["prompt"]
        assert "/api/agent/events" in body["prompt"]
        assert "/api/skills/focus_report" in body["prompt"]
        assert len(fake.calls) >= 1

        # prompt history should contain the generated prompt with timestamp
        r = await client.get(f"/api/tasks/{task_public_id}/recommended_prompt_history?limit=5")
        assert r.status_code == 200
        hist = r.json()
        assert hist["task_public_id"] == task_public_id
        items = hist.get("items") or []
        assert len(items) >= 1
        assert isinstance(items[0].get("created_at"), str)
        assert task_public_id in (items[0].get("prompt") or "")

        # recent events should include something
        r = await client.get("/api/events/recent?limit=10")
        assert r.status_code == 200
        ev = r.json()
        assert isinstance(ev.get("items"), list)
        assert len(ev["items"]) >= 1

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
