# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json

import pytest

from openfocus.agent.agents.attention_scheduler import AttentionSchedulerAgent
from openfocus.agent.llm.types import LLMCallResult
from openfocus.domains.goals import service as goal_service
from openfocus.domains.goals.classification import (
    infer_context_key,
    infer_estimated_minutes,
    infer_task_type,
)


@pytest.mark.parametrize(
    ("title", "description", "expected"),
    [
        ("Review PR", "approve the code review", "review"),
        ("Sync with design", "reply to the meeting thread", "communication"),
        ("Cleanup docs", "organize document updates", "admin"),
        ("Refactor architecture", "analyze service boundaries", "deep_work"),
        ("Ship small fix", "update the button label", "execution"),
    ],
)
def test_infer_task_type(title, description, expected):
    assert infer_task_type(title, description) == expected


def test_infer_estimated_minutes_uses_explicit_minutes_and_hours():
    assert infer_estimated_minutes("execution", "Task", "finish in 15 min") == 15
    assert infer_estimated_minutes("deep_work", "Task", "needs 5 hours") == 240
    assert infer_estimated_minutes("deep_work", "Task", "需要 5小时") == 240


def test_infer_estimated_minutes_uses_type_defaults():
    assert infer_estimated_minutes("review", "Review PR", "") == 25
    assert infer_estimated_minutes("communication", "Reply", "") == 20
    assert infer_estimated_minutes("admin", "Cleanup", "") == 15
    assert infer_estimated_minutes("deep_work", "Design", "") == 90
    assert infer_estimated_minutes("execution", "Ship", "") == 45


def test_infer_context_key_prefers_root_path():
    assert (
        infer_context_key(
            "Review openfocus/domains",
            "topic path exists but root path should win",
            goal_id=7,
            root_path="/Users/example/Project/OpenFocus",
        )
        == "space:openfocus"
    )


def test_infer_context_key_uses_topic_path():
    assert (
        infer_context_key(
            "Update OpenFocus/Domains",
            "keep this scoped",
            goal_id=7,
        )
        == "topic:openfocus/domains"
    )


def test_goal_service_legacy_classifier_imports_return_expected_values():
    title = "Review OpenFocus/Domains"
    description = "Check openfocus/domains extraction in 35 min"
    task_type = goal_service.infer_task_type(title, description)

    assert task_type == "review"
    assert goal_service.infer_estimated_minutes(task_type, title, description) == 35
    assert (
        goal_service.infer_context_key(
            title,
            description,
            goal_id=7,
            root_path="/Users/example/Project/OpenFocus",
        )
        == "space:openfocus"
    )


class _Sink:
    def __init__(self):
        self.events = []

    def emit(self, kind, agent, payload=None, task_id=None):
        self.events.append(
            {"kind": kind, "agent": agent, "payload": payload, "task_id": task_id}
        )


class _CapturingProvider:
    def __init__(self, *, task_public_id: str, goal_id: int, expected_root_path: str):
        self.task_public_id = task_public_id
        self.goal_id = goal_id
        self.expected_root_path = expected_root_path
        self.context = None

    def chat_completions(self, **kwargs):
        messages = kwargs.get("messages") or []
        user_payload = json.loads(messages[1]["content"])
        self.context = user_payload["context"]
        task = self.context["open_goals_and_tasks"][0]["tasks"][0]

        assert task["public_id"] == self.task_public_id
        assert task["task_type"] == infer_task_type(task["title"], task["content"])
        assert task["task_type"] == "review"
        assert task["estimated_minutes"] == infer_estimated_minutes(
            task["task_type"], task["title"], task["content"]
        )
        assert task["estimated_minutes"] == 35
        assert task["context_key"] == infer_context_key(
            task["title"],
            task["content"],
            goal_id=self.goal_id,
            root_path=self.expected_root_path,
        )
        assert task["context_key"] == "space:openfocus"

        return LLMCallResult(
            content=json.dumps(
                {
                    "recommendations": [
                        {
                            "task_public_id": self.task_public_id,
                            "goal_id": self.goal_id,
                            "reason": "Continue while the review context is loaded.",
                            "why": ["Keeps review context warm."],
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


def test_attention_scheduler_infers_missing_task_metadata_from_shared_classifier(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    from openfocus.db import session_scope
    from openfocus.models import AgentSpace, Task

    root_path = str(tmp_path / "OpenFocus")
    with session_scope() as s:
        goal = goal_service.create_goal(
            s,
            title="Scheduler classification goal",
            content="Keep next move context stable",
            due_date=dt.date.today() + dt.timedelta(days=1),
            audit=False,
        )
        task = goal_service.create_task(
            s,
            goal_id=int(goal.id),
            title="Review shared classifier",
            content="Check openfocus/domains extraction in 35 min",
            audit=False,
        )
        task.task_type = ""
        task.estimated_minutes = 0
        task.context_key = ""
        s.add(
            AgentSpace(
                task_public_id=str(task.public_id),
                companion_id=None,
                root_path=root_path,
            )
        )
        goal_id = int(goal.id)
        task_id = int(task.id)
        task_public_id = str(task.public_id)

    provider = _CapturingProvider(
        task_public_id=task_public_id,
        goal_id=goal_id,
        expected_root_path=root_path,
    )
    result = AttentionSchedulerAgent(provider=provider).run(sink=_Sink())

    assert provider.context is not None
    assert result["items"][0]["target"] == {
        "goal_id": goal_id,
        "task_public_id": task_public_id,
    }
    assert result["items"][0]["task_type"] == "review"
    assert result["items"][0]["expected_time_minutes"] == 35
    assert result["items"][0]["context_switch_cost"] == "low"

    with session_scope() as s:
        stored_task = s.get(Task, task_id)
        assert stored_task is not None
        assert stored_task.task_type == ""
        assert stored_task.estimated_minutes == 0
        assert stored_task.context_key == ""
