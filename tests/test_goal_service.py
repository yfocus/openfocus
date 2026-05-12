# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt


def test_goal_service_create_update_and_task_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    from openfocus.db import session_scope
    from openfocus.domains.goals import service
    from openfocus.models import Event, Goal, Task

    due = dt.date.today() + dt.timedelta(days=3)
    with session_scope() as s:
        goal = service.create_goal(
            s,
            title="service goal",
            content="goal content",
            due_date=due,
        )
        goal_id = int(goal.id)
        task = service.create_task(
            s,
            goal_id=goal_id,
            title="review service task",
            content="finish in 15 min",
        )
        task_id = int(task.id)
        task_public_id = str(task.public_id)

    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        task = s.get(Task, task_id)
        assert goal is not None
        assert goal.title == "service goal"
        assert task is not None
        assert task.task_type == "review"
        assert task.estimated_minutes == 15
        assert task.context_key.startswith(f"goal:{goal_id}:")
        assert s.query(Event).filter(Event.kind == "goal.created").count() == 1
        assert s.query(Event).filter(Event.kind == "task.created").count() == 1

    with session_scope() as s:
        service.update_task(
            s,
            task_id=task_id,
            title="sync service task",
            content="meeting follow-up",
        )
        service.mark_task_done(s, task_id=task_id)
        service.reopen_task(s, task_id=task_id)

    with session_scope() as s:
        task = s.get(Task, task_id)
        assert task is not None
        assert task_public_id == task.public_id
        assert task.status == "todo"
        assert task.completed_at is None
        assert task.task_type == "communication"
        assert s.query(Event).filter(Event.kind == "task.confirmed_done").count() == 1
        assert s.query(Event).filter(Event.kind == "task.reopened").count() == 1

    audit_files = list((tmp_path / "memory" / "audit").glob("**/*.md"))
    assert audit_files
    audit_text = "\n".join(path.read_text(encoding="utf-8") for path in audit_files)
    assert "goal.created" in audit_text
    assert "task.created" in audit_text
    assert "task.finished" in audit_text


def test_goal_service_missing_goal_or_task_raises_domain_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    from openfocus.db import session_scope
    from openfocus.domains.goals import service

    with session_scope() as s:
        try:
            service.create_task(s, goal_id=999999, title="missing", content="missing")
        except service.GoalTaskNotFound as exc:
            assert "Goal not found" in str(exc)
        else:
            raise AssertionError("missing goal should raise domain error")

        try:
            service.mark_task_done(s, task_id=999999)
        except service.GoalTaskNotFound as exc:
            assert "Task not found" in str(exc)
        else:
            raise AssertionError("missing task should raise domain error")


def test_goal_service_delete_cleans_agent_space_terminals(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    from openfocus.db import session_scope
    from openfocus.domains.agent_spaces import terminals as terminal_service
    from openfocus.domains.goals import service
    from openfocus.models import (
        AgentSpace,
        Goal,
        RemoteTerminalOutput,
        RemoteTerminalSession,
        Task,
    )

    with session_scope() as s:
        goal = service.create_goal(
            s,
            title="delete cleanup goal",
            content="cleanup",
            due_date=dt.date.today(),
        )
        task = service.create_task(
            s, goal_id=int(goal.id), title="cleanup task", content="cleanup"
        )
        space = AgentSpace(
            task_public_id=str(task.public_id),
            companion_id=None,
            root_path=str(tmp_path),
        )
        s.add(space)
        s.flush()
        owner = terminal_service.owner_for_agent_space(int(space.id))
        terminal = terminal_service.create_terminal_record(
            s,
            owner=owner,
            task_public_id=str(task.public_id),
            companion_id=None,
            root_path=str(tmp_path),
            terminal_id="cleanup-terminal",
            backend="ttyd",
            connect_url="http://127.0.0.1:12345",
        )
        s.add(
            RemoteTerminalOutput(
                space_id=int(space.id),
                terminal_id=str(terminal.terminal_id),
                data_b64="YQ==",
                nbytes=1,
            )
        )
        goal_id = int(goal.id)
        task_id = int(task.id)
        space_id = int(space.id)

    with session_scope() as s:
        service.delete_goal(s, goal_id=goal_id)

    with session_scope() as s:
        assert s.get(Goal, goal_id) is None
        assert s.get(Task, task_id) is None
        assert s.get(AgentSpace, space_id) is None
        assert s.query(RemoteTerminalSession).count() == 0
        assert s.query(RemoteTerminalOutput).count() == 0
