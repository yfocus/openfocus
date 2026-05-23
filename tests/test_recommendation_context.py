# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json

from openfocus.db import session_scope
from openfocus.domains.goals import service as goal_service
from openfocus.domains.goals.classification import (
    infer_context_key,
    infer_estimated_minutes,
    infer_task_type,
)
from openfocus.domains.recommendations.context_builder import (
    RecommendationContextBuilder,
)
from openfocus.models import AgentSpace, Event, NextMoveFeedback, Task


def _due(days: int = 1) -> dt.date:
    return dt.date.today() + dt.timedelta(days=days)


def _task_ids(context: dict) -> list[str]:
    return [
        str(t["public_id"]) for g in context["open_goals_and_tasks"] for t in g["tasks"]
    ]


def test_recommendation_context_filters_open_goals_and_candidate_tasks():
    with session_scope() as s:
        open_goal = goal_service.create_goal(
            s,
            title="Open goal",
            content="Candidates should come from here",
            due_date=_due(),
            audit=False,
        )
        done_goal = goal_service.create_goal(
            s,
            title="Done goal",
            content="Should be filtered",
            due_date=_due(),
            status="done",
            audit=False,
        )
        paused_goal = goal_service.create_goal(
            s,
            title="Paused goal",
            content="Should be filtered",
            due_date=_due(),
            status="paused",
            audit=False,
        )
        todo = goal_service.create_task(
            s,
            goal_id=int(open_goal.id),
            title="Todo task",
            content="allowed",
            audit=False,
        )
        in_progress = goal_service.create_task(
            s,
            goal_id=int(open_goal.id),
            title="In progress task",
            content="allowed",
            audit=False,
        )
        in_progress.status = "in_progress"
        blocked = goal_service.create_task(
            s,
            goal_id=int(open_goal.id),
            title="Blocked task",
            content="allowed",
            audit=False,
        )
        blocked.status = "blocked"
        done = goal_service.create_task(
            s,
            goal_id=int(open_goal.id),
            title="Done task",
            content="filtered",
            audit=False,
        )
        done.status = "done"
        goal_service.create_task(
            s,
            goal_id=int(done_goal.id),
            title="Done goal task",
            content="filtered",
            audit=False,
        )
        goal_service.create_task(
            s,
            goal_id=int(paused_goal.id),
            title="Paused goal task",
            content="filtered",
            audit=False,
        )
        allowed_ids = {todo.public_id, in_progress.public_id, blocked.public_id}

    context = RecommendationContextBuilder().build()

    assert {g["title"] for g in context["open_goals_and_tasks"]} == {"Open goal"}
    assert set(_task_ids(context)) == allowed_ids


def test_recommendation_context_honors_goal_id_filtering():
    with session_scope() as s:
        goal_a = goal_service.create_goal(
            s,
            title="Goal A",
            content="A",
            due_date=_due(1),
            audit=False,
        )
        goal_b = goal_service.create_goal(
            s,
            title="Goal B",
            content="B",
            due_date=_due(2),
            audit=False,
        )
        task_a = goal_service.create_task(
            s,
            goal_id=int(goal_a.id),
            title="Task A",
            content="A",
            audit=False,
        )
        task_b = goal_service.create_task(
            s,
            goal_id=int(goal_b.id),
            title="Task B",
            content="B",
            audit=False,
        )
        goal_b_id = int(goal_b.id)
        task_b_id = task_b.public_id
        assert task_a.public_id != task_b_id

    context = RecommendationContextBuilder(goal_id=goal_b_id).build()

    assert [g["id"] for g in context["open_goals_and_tasks"]] == [goal_b_id]
    assert _task_ids(context) == [task_b_id]


def test_recommendation_context_includes_memory_events_completed_and_recent_dismisses(
    monkeypatch, tmp_path
):
    mem_dir = tmp_path / "memory"
    daily_dir = mem_dir / "daily"
    daily_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text(
        "Long memory: prefer continuity.", encoding="utf-8"
    )
    (daily_dir / "2026-05-23.md").write_text(
        "Daily memory: current repo is OpenFocus.", encoding="utf-8"
    )
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(mem_dir))

    now = dt.datetime.now(dt.timezone.utc)
    with session_scope() as s:
        goal = goal_service.create_goal(
            s,
            title="Memory goal",
            content="Use context",
            due_date=_due(),
            audit=False,
        )
        task = goal_service.create_task(
            s,
            goal_id=int(goal.id),
            title="Current task",
            content="Candidate",
            audit=False,
        )
        older_dismissed_task = goal_service.create_task(
            s,
            goal_id=int(goal.id),
            title="Older dismissed",
            content="Candidate",
            audit=False,
        )
        done_task = goal_service.create_task(
            s,
            goal_id=int(goal.id),
            title="Completed recently",
            content="Done",
            audit=False,
        )
        done_task.status = "done"
        done_task.completed_at = now - dt.timedelta(days=2)
        s.add(
            Event(
                kind="task.progress",
                agent="test",
                task_id=task.public_id,
                payload={"note": "recent progress"},
                created_at=now,
            )
        )
        s.add(
            Event(
                kind="goal.confirmed_done_by_user",
                agent="test",
                payload={"goal_id": int(goal.id)},
                created_at=now,
            )
        )
        s.add(
            NextMoveFeedback(
                task_public_id=task.public_id,
                feedback_type="dismiss",
                reason_code="not_for_now",
                created_at=now - dt.timedelta(hours=1),
            )
        )
        s.add(
            NextMoveFeedback(
                task_public_id=older_dismissed_task.public_id,
                feedback_type="dismiss",
                reason_code="not_for_now",
                created_at=now - dt.timedelta(hours=2),
            )
        )
        s.add(
            NextMoveFeedback(
                task_public_id=task.public_id,
                feedback_type="dismiss",
                reason_code="not_for_now",
                created_at=now - dt.timedelta(hours=3),
            )
        )
        s.add(
            NextMoveFeedback(
                task_public_id="old-dismiss",
                feedback_type="dismiss",
                reason_code="not_for_now",
                created_at=now - dt.timedelta(hours=25),
            )
        )
        s.add(
            NextMoveFeedback(
                task_public_id="accepted-task",
                feedback_type="accept",
                reason_code="",
                created_at=now - dt.timedelta(hours=1),
            )
        )
        task_public_id = task.public_id
        older_task_public_id = older_dismissed_task.public_id
        done_public_id = done_task.public_id

    context = RecommendationContextBuilder().build()

    json.dumps(context, ensure_ascii=False)
    assert context["long_memory_full_content"] == "Long memory: prefer continuity."
    assert context["daily_memory_access"]["available_files_preview"][0]["rel_path"] == (
        "daily/2026-05-23.md"
    )
    assert context["event_access"]["recent_events_included"] == 100
    assert any(
        ev["kind"] == "task.progress" and ev["payload"] == {"note": "recent progress"}
        for ev in context["recent_events_latest_100"]
    )
    assert context["recent_not_for_now_task_public_ids"] == [
        task_public_id,
        older_task_public_id,
    ]
    assert done_public_id in {
        t["public_id"] for t in context["completed_last_7_days"]["tasks"]
    }
    assert any(
        fb["task_public_id"] == task_public_id
        for fb in context["recent_next_move_feedback"]
    )


def test_recommendation_context_infers_missing_metadata_without_writing_db(tmp_path):
    root_path = str(tmp_path / "OpenFocus")
    with session_scope() as s:
        goal = goal_service.create_goal(
            s,
            title="Inference goal",
            content="Metadata should be inferred in snapshot only",
            due_date=_due(),
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
                task_public_id=task.public_id,
                companion_id=None,
                root_path=root_path,
            )
        )
        goal_id = int(goal.id)
        task_id = int(task.id)
        task_public_id = task.public_id

    context = RecommendationContextBuilder().build()
    task_payload = next(
        t
        for g in context["open_goals_and_tasks"]
        for t in g["tasks"]
        if t["public_id"] == task_public_id
    )

    assert task_payload["task_type"] == infer_task_type(
        task_payload["title"], task_payload["content"]
    )
    assert task_payload["estimated_minutes"] == infer_estimated_minutes(
        task_payload["task_type"], task_payload["title"], task_payload["content"]
    )
    assert task_payload["context_key"] == infer_context_key(
        task_payload["title"],
        task_payload["content"],
        goal_id=goal_id,
        root_path=root_path,
    )
    assert task_payload["task_type"] == "review"
    assert task_payload["estimated_minutes"] == 35
    assert task_payload["context_key"] == "space:openfocus"

    with session_scope() as s:
        stored_task = s.get(Task, task_id)
        assert stored_task is not None
        assert stored_task.task_type == ""
        assert stored_task.estimated_minutes == 0
        assert stored_task.context_key == ""
