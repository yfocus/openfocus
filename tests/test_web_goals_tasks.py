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
                "description": "desc",
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
        r = await client.post(f"/goals/{goal_id}/tasks", data={"title": "task-1"}, follow_redirects=False)
        assert r.status_code == 303

        from openfocus.models import Task

        with session_scope() as s:
            t = s.query(Task).filter(Task.goal_id == goal_id).order_by(Task.id.desc()).first()
            assert t.title == "task-1"
            assert hasattr(t, "recommended_prompt") is False
            task_id = t.id
            task_public_id = t.public_id

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

        # mark done
        r = await client.post(f"/tasks/{task_id}/done", follow_redirects=False)
        assert r.status_code == 303
        with session_scope() as s:
            t = s.get(Task, task_id)
            assert t.status == "done"
            assert t.completed_at is not None

        # delete task
        r = await client.post(f"/tasks/{task_id}/delete", follow_redirects=False)
        assert r.status_code == 303
        with session_scope() as s:
            assert s.get(Task, task_id) is None

        # delete goal (should also delete tasks)
        r = await client.post(f"/goals/{goal_id}/delete", follow_redirects=False)
        assert r.status_code == 303
