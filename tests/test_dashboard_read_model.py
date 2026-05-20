# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt


def test_dashboard_read_model_projects_runtime_activity_and_events():
    from openfocus.db import session_scope
    from openfocus.domains.dashboard import read_model
    from openfocus.domains.events import service as event_service
    from openfocus.domains.goals import service as goal_service
    from openfocus.models import AgentSpace, Task, TaskAgentActivity

    now = dt.datetime.now(dt.timezone.utc)
    long_title = "构建一个可生产运行的 Dashboard Read Model 并保持展示字段只读"

    with session_scope() as s:
        goal = goal_service.create_goal(
            s,
            title=long_title,
            content="Keep the Dashboard route shallow.",
            due_date=dt.date.today() + dt.timedelta(days=3),
            agent="ui",
            source="web",
        )
        task = goal_service.create_task(
            s,
            goal_id=int(goal.id),
            title="Refactor dashboard projection",
            content="Move query aggregation behind a small read-model interface.",
            agent="ui",
            source="web",
        )
        task_public_id = str(task.public_id)
        task_id = int(task.id)
        goal_id = int(goal.id)

        s.add(
            AgentSpace(
                task_public_id=task_public_id,
                companion_id=1,
                root_path="/tmp/openfocus-test",
                start_agent_command="codex",
            )
        )
        s.add(
            TaskAgentActivity(
                task_public_id=task_public_id,
                state="running",
                last_activity_at=now,
                state_started_at=now,
            )
        )
        event_service.record_event(
            s,
            kind="task.progress",
            agent="codex",
            task_id=task_public_id,
            payload={"message": "Projection extracted."},
            create_attention=False,
        )

    ctx = read_model.load_dashboard_context({"task": task_public_id})

    assert ctx["selected_task"].public_id == task_public_id
    assert ctx["task_meta"][task_public_id]["ui_status"] == "in_progress"
    assert ctx["task_meta"][task_public_id]["percent"] is None
    assert (
        ctx["agent_spaces_by_task"][task_public_id].root_path == "/tmp/openfocus-test"
    )
    assert ctx["last_start_agent_command"] == "codex"
    assert ctx["goal_display"][goal_id].endswith("…")
    assert ctx["task_events"][task_public_id][0]["kind_label"] == "Progress reported"
    assert ctx["task_events"][task_public_id][0]["summary"] == "Projection extracted."
    assert ctx["goal_events"][goal_id][0]["task_public_id"] == task_public_id

    with session_scope() as s:
        persisted_task = s.get(Task, task_id)
        assert persisted_task is not None
        assert persisted_task.status == "todo"


def test_dashboard_read_model_filters_completed_goals():
    from openfocus.db import session_scope
    from openfocus.domains.dashboard import read_model
    from openfocus.domains.goals import service as goal_service

    with session_scope() as s:
        active = goal_service.create_goal(
            s,
            title="Active goal",
            content="Still open.",
            due_date=dt.date.today() + dt.timedelta(days=1),
            agent="ui",
            source="web",
        )
        done = goal_service.create_goal(
            s,
            title="Done goal",
            content="Finished by a human.",
            due_date=dt.date.today() + dt.timedelta(days=1),
            agent="ui",
            source="web",
        )
        active_goal_id = int(active.id)
        done_goal_id = int(done.id)
        goal_service.mark_goal_done(s, goal_id=done_goal_id)

    completed = read_model.load_dashboard_context({"gfilter": "COMPLETED"})
    completed_ids = [int(g.id) for g in completed["goals"]]

    assert done_goal_id in completed_ids
    assert active_goal_id not in completed_ids

    in_progress = read_model.load_dashboard_context({"gfilter": "IN_PROGRESS"})
    in_progress_ids = [int(g.id) for g in in_progress["goals"]]

    assert active_goal_id in in_progress_ids
    assert done_goal_id not in in_progress_ids
