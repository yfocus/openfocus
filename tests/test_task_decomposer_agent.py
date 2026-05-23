# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


def _audit_text(memory_root: Path) -> str:
    audit_files = sorted((memory_root / "audit").glob("**/*.md"))
    return "\n".join(path.read_text(encoding="utf-8") for path in audit_files)


def test_task_decomposer_run_records_task_created_event_without_audit_memory(
    monkeypatch, tmp_path
):
    memory_root = tmp_path / "memory"
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(memory_root))

    from openfocus.agent.agents.task_decomposer import TaskDecomposerAgent
    from openfocus.agent.llm.types import LLMCallResult
    from openfocus.db import session_scope
    from openfocus.domains.goals import service as goal_service
    from openfocus.models import Event, Task

    class FakeProvider:
        def __init__(self):
            self.calls = []

        def chat_completions(self, **kwargs):
            self.calls.append(kwargs)
            messages = kwargs.get("messages") or []
            user_text = str(messages[1].get("content") or "")
            assert "decompose public seam" in user_text
            assert "please split this goal" in user_text
            return LLMCallResult(
                content=json.dumps(
                    {
                        "tasks": [
                            {
                                "title": "Map implementation steps",
                                "rationale": "List the concrete work needed first.",
                                "estimate_minutes": 20,
                            },
                            {
                                "title": "Review task boundaries",
                                "rationale": "Confirm each task can be executed independently.",
                                "estimate_minutes": 15,
                            },
                        ]
                    }
                ),
                finish_reason="stop",
                usage={"total_tokens": 42},
                tool_calls=None,
            )

    class RecordingSink:
        def __init__(self):
            self.events = []

        def emit(self, kind, agent, payload=None, task_id=None):
            self.events.append(
                {
                    "kind": kind,
                    "agent": agent,
                    "payload": payload or {},
                    "task_id": task_id,
                }
            )

    with session_scope() as s:
        goal = goal_service.create_goal(
            s,
            title="decompose public seam",
            content="please split this goal",
            due_date=dt.date.today() + dt.timedelta(days=7),
            audit=False,
        )
        goal_id = int(goal.id)

    provider = FakeProvider()
    sink = RecordingSink()
    result = TaskDecomposerAgent(goal_id=goal_id, provider=provider).run(sink=sink)

    assert len(provider.calls) == 1
    assert [event["kind"] for event in sink.events] == [
        "agent.started",
        "agent.llm_call.started",
        "agent.llm_call.completed",
        "agent.completed",
    ]
    assert result["goal_id"] == goal_id
    assert [task["title"] for task in result["created_tasks"]] == [
        "Map implementation steps",
        "Review task boundaries",
    ]

    with session_scope() as s:
        tasks = (
            s.query(Task).filter(Task.goal_id == goal_id).order_by(Task.id.asc()).all()
        )
        assert [task.title for task in tasks] == [
            "Map implementation steps",
            "Review task boundaries",
        ]
        assert [task.status for task in tasks] == ["todo", "todo"]

        events = (
            s.query(Event)
            .filter(Event.kind == "task.created")
            .order_by(Event.id.asc())
            .all()
        )
        assert len(events) == 2
        events_by_title = {str(event.payload["title"]): event for event in events}
        for task in tasks:
            event = events_by_title[task.title]
            assert event.agent == "task_decomposer"
            assert event.task_id == task.public_id
            assert event.payload == {
                "goal_id": goal_id,
                "task_public_id": task.public_id,
                "title": task.title,
            }

    audit_files = sorted((memory_root / "audit").glob("**/*.md"))
    assert not audit_files or all(
        not path.read_text(encoding="utf-8").strip() for path in audit_files
    )
    assert _audit_text(memory_root).strip() == ""
