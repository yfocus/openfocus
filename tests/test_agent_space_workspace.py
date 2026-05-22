# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import datetime as dt
from types import SimpleNamespace

import pytest


class FakeWorkspaceRuntime:
    def __init__(self, *, fail_agent_terminate: bool = False) -> None:
        self.fail_agent_terminate = fail_agent_terminate
        self.stops: list[dict] = []
        self.agent_terminates: list[dict] = []

    async def request_terminal_stop(self, **kwargs):
        self.stops.append(dict(kwargs))

    async def request_agent_terminate(self, **kwargs):
        self.agent_terminates.append(dict(kwargs))
        if self.fail_agent_terminate:
            raise RuntimeError("agent terminate failed")


class FakeAgentSessionRuntime:
    def __init__(
        self,
        *,
        real_session_id: str = "",
        fail_start: bool = False,
        fail_terminate: bool = False,
    ) -> None:
        self.real_session_id = real_session_id
        self.fail_start = fail_start
        self.fail_terminate = fail_terminate
        self.starts: list[dict] = []
        self.terminates: list[dict] = []

    async def request_agent_start(self, **kwargs):
        self.starts.append(dict(kwargs))
        if self.fail_start:
            raise RuntimeError("agent start failed")
        return SimpleNamespace(session_id=self.real_session_id)

    async def request_agent_terminate(self, **kwargs):
        self.terminates.append(dict(kwargs))
        if self.fail_terminate:
            raise RuntimeError("agent terminate failed")


class FakeRuntimeTurnProjector:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, db_session, **kwargs):
        assert db_session is not None
        call = dict(kwargs)
        self.calls.append(call)
        return {"kind": call["kind"], "turn_id": call["turn_id"]}


def _seed_agent_session_for_send(
    tmp_path,
    *,
    session_id: str = "session-to-send",
    companion_id: int = 23,
    agent_type: str = "codex-cli",
):
    from openfocus.db import session_scope
    from openfocus.models import AgentSession, AgentSpace, Goal, Task

    with session_scope() as s:
        goal = Goal(title="g", content="d", due_date=dt.date.today())
        s.add(goal)
        s.flush()
        task = Task(goal_id=goal.id, title="t", content="d", status="todo")
        s.add(task)
        s.flush()
        task_public_id = str(task.public_id)
        space = AgentSpace(
            task_public_id=task_public_id,
            companion_id=companion_id,
            root_path=str(tmp_path),
            agent_type=agent_type,
        )
        s.add(space)
        s.flush()
        space_id = int(space.id)
        s.add(
            AgentSession(
                session_id=session_id,
                space_id=space_id,
                task_public_id=task_public_id,
                companion_id=companion_id,
                root_path=str(tmp_path),
                agent_type=agent_type,
                status="active",
            )
        )

    return SimpleNamespace(
        space_id=space_id,
        task_public_id=task_public_id,
        session_id=session_id,
        companion_id=companion_id,
        agent_type=agent_type,
    )


def _seed_workspace_task() -> str:
    from openfocus.db import session_scope
    from openfocus.models import Goal, Task

    with session_scope() as s:
        goal = Goal(title="g", content="d", due_date=dt.date.today())
        s.add(goal)
        s.flush()
        task = Task(goal_id=goal.id, title="t", content="d", status="todo")
        s.add(task)
        s.flush()
        return str(task.public_id)


def _seed_workspace_companion(
    device_id: str = "companion-one",
    *,
    status: str = "active",
    auth_token: str = "token",
) -> int:
    from openfocus.db import session_scope
    from openfocus.models import Companion

    with session_scope() as s:
        companion = Companion(
            device_id=device_id,
            name=device_id,
            base_url=f"http://{device_id}.local",
            status=status,
            auth_token=auth_token,
        )
        s.add(companion)
        s.flush()
        return int(companion.id)


def test_create_or_update_agent_space_for_task_creates_new_space(tmp_path):
    from openfocus.db import session_scope
    from openfocus.domains.agent_spaces import workspace
    from openfocus.models import AgentSpace

    task_public_id = _seed_workspace_task()
    companion_id = _seed_workspace_companion()

    result = workspace.create_or_update_agent_space_for_task(
        f" {task_public_id} ",
        companion_id=companion_id,
        root_path=f" {tmp_path / 'workspace'} ",
        start_agent_command=" coco -y ",
    )

    assert result.created is True
    assert result.space.task_public_id == task_public_id
    assert result.space.companion_id == companion_id
    assert result.space.root_path == str(tmp_path / "workspace")
    assert result.space.agent_type == "trae-cli"
    assert result.space.start_agent_command == "coco -y"
    assert result.to_dict()["space_id"] == result.space.id

    with session_scope() as s:
        row = s.get(AgentSpace, result.space.id)
        assert row is not None
        assert row.task_public_id == task_public_id
        assert row.companion_id == companion_id
        assert row.root_path == str(tmp_path / "workspace")
        assert row.agent_type == "trae-cli"
        assert row.start_agent_command == "coco -y"


def test_create_or_update_agent_space_for_task_updates_existing_space(tmp_path):
    from openfocus.db import session_scope
    from openfocus.domains.agent_spaces import workspace
    from openfocus.models import AgentSpace

    task_public_id = _seed_workspace_task()
    first_companion_id = _seed_workspace_companion("companion-one")
    second_companion_id = _seed_workspace_companion("companion-two")

    first = workspace.create_or_update_agent_space_for_task(
        task_public_id,
        companion_id=first_companion_id,
        root_path=str(tmp_path / "one"),
        start_agent_command="coco -y",
    )
    second = workspace.create_or_update_agent_space_for_task(
        task_public_id,
        companion_id=second_companion_id,
        root_path=str(tmp_path / "two"),
        start_agent_command="trae-cli --yes",
    )

    assert first.created is True
    assert second.created is False
    assert second.space.id == first.space.id
    assert second.space.companion_id == second_companion_id
    assert second.space.root_path == str(tmp_path / "two")
    assert second.space.agent_type == "trae-cli"
    assert second.space.start_agent_command == "trae-cli --yes"

    with session_scope() as s:
        rows = s.query(AgentSpace).all()
        assert len(rows) == 1
        assert rows[0].id == first.space.id
        assert rows[0].companion_id == second_companion_id
        assert rows[0].root_path == str(tmp_path / "two")
        assert rows[0].start_agent_command == "trae-cli --yes"


def test_get_agent_space_for_task_represents_missing_space_consistently():
    from openfocus.domains.agent_spaces import workspace

    result = workspace.get_agent_space_for_task(" missing-task ")

    assert result.task_public_id == "missing-task"
    assert result.space is None
    assert result.to_dict() == {"ok": True, "space": None}


def test_start_agent_command_get_update_and_length_validation(tmp_path):
    from openfocus.domains.agent_spaces import workspace

    task_public_id = _seed_workspace_task()
    companion_id = _seed_workspace_companion()
    created = workspace.create_or_update_agent_space_for_task(
        task_public_id,
        companion_id=companion_id,
        root_path=str(tmp_path / "workspace"),
        start_agent_command="coco -y",
    )

    initial = workspace.get_start_agent_command(created.space.id)
    assert initial.to_dict() == {"ok": True, "start_agent_command": "coco -y"}

    updated = workspace.update_start_agent_command(
        created.space.id,
        " trae-cli --yes ",
    )
    assert updated.start_agent_command == "trae-cli --yes"
    assert (
        workspace.get_start_agent_command(created.space.id).start_agent_command
        == "trae-cli --yes"
    )

    with pytest.raises(
        workspace.AgentSpaceValidationError, match="command is too long"
    ):
        workspace.update_start_agent_command(created.space.id, "x" * 2001)
    assert (
        workspace.get_start_agent_command(created.space.id).start_agent_command
        == "trae-cli --yes"
    )

    with pytest.raises(workspace.AgentSpaceNotFound, match="AgentSpace not found"):
        workspace.get_start_agent_command(999999)


def test_start_agent_session_persists_after_runtime_start(tmp_path):
    async def _run() -> None:
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import agent_sessions
        from openfocus.models import AgentSession, AgentSpace, Goal, Task

        with session_scope() as s:
            goal = Goal(title="g", content="d", due_date=dt.date.today())
            s.add(goal)
            s.flush()
            task = Task(goal_id=goal.id, title="t", content="d", status="todo")
            s.add(task)
            s.flush()
            task_public_id = str(task.public_id)
            space = AgentSpace(
                task_public_id=task_public_id,
                companion_id=17,
                root_path=str(tmp_path),
                agent_type="codex-cli",
            )
            s.add(space)
            s.flush()
            space_id = int(space.id)

        runtime = FakeAgentSessionRuntime(real_session_id="runtime-session")

        result = await agent_sessions.start_agent_session(
            space_id,
            runtime=runtime,
            companion_id=17,
            session_id_factory=lambda: "generated-session",
        )

        assert result.session_id == "runtime-session"
        assert runtime.starts == [
            {
                "session_id": "generated-session",
                "root_path": str(tmp_path),
                "agent_type": "codex-cli",
                "task_public_id": task_public_id,
                "timeout_seconds": 10.0,
            }
        ]
        with session_scope() as s:
            row = (
                s.query(AgentSession)
                .filter(AgentSession.session_id == "runtime-session")
                .one()
            )
            assert int(row.space_id) == space_id
            assert row.task_public_id == task_public_id
            assert row.companion_id == 17
            assert row.root_path == str(tmp_path)
            assert row.agent_type == "codex-cli"
            assert row.status == "active"

    asyncio.run(_run())


def test_start_agent_session_runtime_failure_leaves_no_local_session(tmp_path):
    async def _run() -> None:
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import agent_sessions
        from openfocus.models import AgentSession, AgentSpace, Goal, Task

        with session_scope() as s:
            goal = Goal(title="g", content="d", due_date=dt.date.today())
            s.add(goal)
            s.flush()
            task = Task(goal_id=goal.id, title="t", content="d", status="todo")
            s.add(task)
            s.flush()
            space = AgentSpace(
                task_public_id=str(task.public_id),
                companion_id=17,
                root_path=str(tmp_path),
                agent_type="codex-cli",
            )
            s.add(space)
            s.flush()
            space_id = int(space.id)

        runtime = FakeAgentSessionRuntime(fail_start=True)

        with pytest.raises(RuntimeError, match="agent start failed"):
            await agent_sessions.start_agent_session(
                space_id,
                runtime=runtime,
                companion_id=17,
                session_id_factory=lambda: "generated-session",
            )

        with session_scope() as s:
            assert s.query(AgentSession).count() == 0

    asyncio.run(_run())


def test_terminate_agent_session_updates_status_after_runtime_success(tmp_path):
    async def _run() -> None:
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import agent_sessions
        from openfocus.models import AgentSession, AgentSpace, Goal, Task

        with session_scope() as s:
            goal = Goal(title="g", content="d", due_date=dt.date.today())
            s.add(goal)
            s.flush()
            task = Task(goal_id=goal.id, title="t", content="d", status="todo")
            s.add(task)
            s.flush()
            task_public_id = str(task.public_id)
            space = AgentSpace(
                task_public_id=task_public_id,
                companion_id=17,
                root_path=str(tmp_path),
                agent_type="codex-cli",
            )
            s.add(space)
            s.flush()
            space_id = int(space.id)
            s.add(
                AgentSession(
                    session_id="session-to-stop",
                    space_id=space_id,
                    task_public_id=task_public_id,
                    companion_id=17,
                    root_path=str(tmp_path),
                    agent_type="codex-cli",
                    status="active",
                )
            )

        runtime = FakeAgentSessionRuntime()

        result = await agent_sessions.terminate_agent_session(
            space_id,
            "session-to-stop",
            runtime=runtime,
        )

        assert result.status == "terminated"
        assert runtime.terminates == [
            {"session_id": "session-to-stop", "timeout_seconds": 10.0}
        ]
        with session_scope() as s:
            row = (
                s.query(AgentSession)
                .filter(AgentSession.session_id == "session-to-stop")
                .one()
            )
            assert row.status == "terminated"

    asyncio.run(_run())


def test_terminate_agent_session_runtime_failure_leaves_status_unchanged(tmp_path):
    async def _run() -> None:
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import agent_sessions
        from openfocus.models import AgentSession, AgentSpace, Goal, Task

        with session_scope() as s:
            goal = Goal(title="g", content="d", due_date=dt.date.today())
            s.add(goal)
            s.flush()
            task = Task(goal_id=goal.id, title="t", content="d", status="todo")
            s.add(task)
            s.flush()
            task_public_id = str(task.public_id)
            space = AgentSpace(
                task_public_id=task_public_id,
                companion_id=17,
                root_path=str(tmp_path),
                agent_type="codex-cli",
            )
            s.add(space)
            s.flush()
            space_id = int(space.id)
            s.add(
                AgentSession(
                    session_id="session-stays-active",
                    space_id=space_id,
                    task_public_id=task_public_id,
                    companion_id=17,
                    root_path=str(tmp_path),
                    agent_type="codex-cli",
                    status="active",
                )
            )

        runtime = FakeAgentSessionRuntime(fail_terminate=True)

        with pytest.raises(RuntimeError, match="agent terminate failed"):
            await agent_sessions.terminate_agent_session(
                space_id,
                "session-stays-active",
                runtime=runtime,
            )

        assert runtime.terminates == [
            {"session_id": "session-stays-active", "timeout_seconds": 10.0}
        ]
        with session_scope() as s:
            row = (
                s.query(AgentSession)
                .filter(AgentSession.session_id == "session-stays-active")
                .one()
            )
            assert row.status == "active"

    asyncio.run(_run())


def test_send_agent_session_message_persists_user_message_and_returns_context(
    tmp_path,
):
    from openfocus.db import session_scope
    from openfocus.domains.agent_spaces import agent_sessions
    from openfocus.models import AgentMessage, AgentSession, AgentSpace, Goal, Task

    with session_scope() as s:
        goal = Goal(title="g", content="d", due_date=dt.date.today())
        s.add(goal)
        s.flush()
        task = Task(goal_id=goal.id, title="t", content="d", status="todo")
        s.add(task)
        s.flush()
        task_public_id = str(task.public_id)
        space = AgentSpace(
            task_public_id=task_public_id,
            companion_id=23,
            root_path=str(tmp_path),
            agent_type="codex-cli",
        )
        s.add(space)
        s.flush()
        space_id = int(space.id)
        s.add(
            AgentSession(
                session_id="session-to-send",
                space_id=space_id,
                task_public_id=task_public_id,
                companion_id=23,
                root_path=str(tmp_path),
                agent_type="codex-cli",
                status="active",
            )
        )

    result = agent_sessions.send_agent_session_message(
        space_id,
        " session-to-send ",
        "hello agent",
        request_id_factory=lambda: "request-fixed",
    )

    assert result.session_id == "session-to-send"
    assert result.space_id == space_id
    assert result.request_id == "request-fixed"
    assert result.user_text == "hello agent"
    assert result.task_public_id == task_public_id
    assert result.agent_type == "codex-cli"
    assert result.companion_id == 23

    with session_scope() as s:
        messages = s.query(AgentMessage).order_by(AgentMessage.id.asc()).all()
        assert len(messages) == 1
        assert messages[0].session_id == "session-to-send"
        assert messages[0].role == "user"
        assert messages[0].content == "hello agent"
        assert messages[0].request_id == ""
        assert messages[0].done is True


def test_begin_agent_session_assistant_turn_persists_placeholder_and_projects_started(
    tmp_path,
):
    from openfocus.db import session_scope
    from openfocus.domains.agent_spaces import agent_sessions
    from openfocus.models import AgentMessage

    seeded = _seed_agent_session_for_send(tmp_path)
    send_result = agent_sessions.send_agent_session_message(
        seeded.space_id,
        seeded.session_id,
        "hello agent",
        request_id_factory=lambda: "request-fixed",
    )
    projector = FakeRuntimeTurnProjector()

    result = agent_sessions.begin_agent_session_assistant_turn(
        send_result,
        projector=projector,
    )

    assert result.session_id == seeded.session_id
    assert result.space_id == seeded.space_id
    assert result.request_id == "request-fixed"
    assert result.task_public_id == seeded.task_public_id
    assert result.agent_type == seeded.agent_type
    assert result.companion_id == seeded.companion_id
    assert result.projection == {
        "kind": "runtime.turn.started",
        "turn_id": "request-fixed",
    }
    assert projector.calls == [
        {
            "kind": "runtime.turn.started",
            "agent_runtime": "codex-cli",
            "session_id": seeded.session_id,
            "turn_id": "request-fixed",
            "task_public_id": seeded.task_public_id,
            "companion_id": 23,
            "source": "openfocus.agent_session.send",
            "payload": {"message": "Prompt submitted from OpenFocus AgentSpace."},
        }
    ]

    with session_scope() as s:
        messages = s.query(AgentMessage).order_by(AgentMessage.id.asc()).all()
        assert [message.role for message in messages] == ["user", "assistant"]
        assistant = messages[1]
        assert assistant.session_id == seeded.session_id
        assert assistant.request_id == "request-fixed"
        assert assistant.content == ""
        assert assistant.done is False
        assert assistant.error == ""


def test_fail_agent_session_assistant_turn_marks_error_and_projects_failed(tmp_path):
    from openfocus.db import session_scope
    from openfocus.domains.agent_spaces import agent_sessions
    from openfocus.models import AgentMessage

    seeded = _seed_agent_session_for_send(tmp_path)
    send_result = agent_sessions.send_agent_session_message(
        seeded.space_id,
        seeded.session_id,
        "hello agent",
        request_id_factory=lambda: "request-fixed",
    )
    projector = FakeRuntimeTurnProjector()
    agent_sessions.begin_agent_session_assistant_turn(
        send_result,
        projector=projector,
    )

    result = agent_sessions.fail_agent_session_assistant_turn(
        send_result,
        RuntimeError("runtime unavailable"),
        projector=projector,
    )

    assert result.session_id == seeded.session_id
    assert result.request_id == "request-fixed"
    assert result.error == "runtime unavailable"
    assert result.projection == {
        "kind": "runtime.turn.failed",
        "turn_id": "request-fixed",
    }
    assert len(projector.calls) == 2
    assert projector.calls[-1] == {
        "kind": "runtime.turn.failed",
        "agent_runtime": "codex-cli",
        "session_id": seeded.session_id,
        "turn_id": "request-fixed",
        "task_public_id": seeded.task_public_id,
        "companion_id": 23,
        "source": "openfocus.agent_session.send",
        "payload": {"error": "runtime unavailable"},
    }

    with session_scope() as s:
        assistant = (
            s.query(AgentMessage)
            .filter(AgentMessage.session_id == seeded.session_id)
            .filter(AgentMessage.request_id == "request-fixed")
            .filter(AgentMessage.role == "assistant")
            .one()
        )
        assert assistant.done is True
        assert assistant.error == "runtime unavailable"
        assert assistant.content == ""


def test_send_agent_session_message_rejects_empty_session_id():
    from openfocus.db import session_scope
    from openfocus.domains.agent_spaces import agent_sessions
    from openfocus.models import AgentMessage

    with pytest.raises(
        agent_sessions.AgentSessionValidationError, match="session_id is required"
    ):
        agent_sessions.send_agent_session_message(
            1,
            "  ",
            "hello agent",
            request_id_factory=lambda: "request-fixed",
        )

    with session_scope() as s:
        assert s.query(AgentMessage).count() == 0


def test_send_agent_session_message_rejects_missing_or_wrong_space_session(tmp_path):
    from openfocus.db import session_scope
    from openfocus.domains.agent_spaces import agent_sessions
    from openfocus.models import AgentMessage, AgentSession, AgentSpace, Goal, Task

    with session_scope() as s:
        goal = Goal(title="g", content="d", due_date=dt.date.today())
        s.add(goal)
        s.flush()
        task_one = Task(goal_id=goal.id, title="t1", content="d", status="todo")
        task_two = Task(goal_id=goal.id, title="t2", content="d", status="todo")
        s.add_all([task_one, task_two])
        s.flush()
        space_one = AgentSpace(
            task_public_id=str(task_one.public_id),
            companion_id=31,
            root_path=str(tmp_path / "one"),
            agent_type="codex-cli",
        )
        space_two = AgentSpace(
            task_public_id=str(task_two.public_id),
            companion_id=32,
            root_path=str(tmp_path / "two"),
            agent_type="codex-cli",
        )
        s.add_all([space_one, space_two])
        s.flush()
        space_one_id = int(space_one.id)
        space_two_id = int(space_two.id)
        s.add(
            AgentSession(
                session_id="session-in-space-one",
                space_id=space_one_id,
                task_public_id=str(task_one.public_id),
                companion_id=31,
                root_path=str(tmp_path / "one"),
                agent_type="codex-cli",
                status="active",
            )
        )

    with pytest.raises(
        agent_sessions.AgentSessionNotFound, match="Agent session not found"
    ):
        agent_sessions.send_agent_session_message(
            space_one_id,
            "missing-session",
            "hello agent",
            request_id_factory=lambda: "request-missing",
        )

    with pytest.raises(
        agent_sessions.AgentSessionNotFound, match="Agent session not found"
    ):
        agent_sessions.send_agent_session_message(
            space_two_id,
            "session-in-space-one",
            "hello agent",
            request_id_factory=lambda: "request-wrong-space",
        )

    with session_scope() as s:
        assert s.query(AgentMessage).count() == 0


def test_list_agent_sessions_returns_newest_first_with_route_payload_shape(tmp_path):
    from openfocus.db import session_scope
    from openfocus.domains.agent_spaces import agent_sessions
    from openfocus.models import AgentSession, AgentSpace, Goal, Task

    with session_scope() as s:
        goal = Goal(title="g", content="d", due_date=dt.date.today())
        s.add(goal)
        s.flush()
        task = Task(goal_id=goal.id, title="t", content="d", status="todo")
        s.add(task)
        s.flush()
        space = AgentSpace(
            task_public_id=str(task.public_id),
            companion_id=23,
            root_path=str(tmp_path),
            agent_type="codex-cli",
        )
        s.add(space)
        s.flush()
        space_id = int(space.id)
        older = AgentSession(
            session_id="older-session",
            space_id=space_id,
            task_public_id=str(task.public_id),
            companion_id=23,
            root_path=str(tmp_path),
            agent_type="codex-cli",
            status="terminated",
            created_at=dt.datetime(2026, 1, 1, 12, 0, 0),
            updated_at=dt.datetime(2026, 1, 1, 12, 1, 0),
        )
        newer = AgentSession(
            session_id="newer-session",
            space_id=space_id,
            task_public_id=str(task.public_id),
            companion_id=23,
            root_path=str(tmp_path),
            agent_type="trae-cli",
            status="active",
            created_at=dt.datetime(2026, 1, 2, 12, 0, 0),
            updated_at=dt.datetime(2026, 1, 2, 12, 1, 0),
        )
        s.add_all([older, newer])

    result = agent_sessions.list_agent_sessions(space_id)

    assert result.to_dict() == {
        "ok": True,
        "sessions": [
            {
                "session_id": "newer-session",
                "status": "active",
                "agent_type": "trae-cli",
                "created_at": "2026-01-02T12:00:00",
                "updated_at": "2026-01-02T12:01:00",
            },
            {
                "session_id": "older-session",
                "status": "terminated",
                "agent_type": "codex-cli",
                "created_at": "2026-01-01T12:00:00",
                "updated_at": "2026-01-01T12:01:00",
            },
        ],
    }


def test_get_agent_session_messages_requires_ownership_and_preserves_message_order(
    tmp_path,
):
    from openfocus.db import session_scope
    from openfocus.domains.agent_spaces import agent_sessions
    from openfocus.models import AgentMessage, AgentSession, AgentSpace, Goal, Task

    with session_scope() as s:
        goal = Goal(title="g", content="d", due_date=dt.date.today())
        s.add(goal)
        s.flush()
        task_one = Task(goal_id=goal.id, title="t1", content="d", status="todo")
        task_two = Task(goal_id=goal.id, title="t2", content="d", status="todo")
        s.add_all([task_one, task_two])
        s.flush()
        space_one = AgentSpace(
            task_public_id=str(task_one.public_id),
            companion_id=31,
            root_path=str(tmp_path / "one"),
            agent_type="codex-cli",
        )
        space_two = AgentSpace(
            task_public_id=str(task_two.public_id),
            companion_id=32,
            root_path=str(tmp_path / "two"),
            agent_type="codex-cli",
        )
        s.add_all([space_one, space_two])
        s.flush()
        space_one_id = int(space_one.id)
        space_two_id = int(space_two.id)
        s.add(
            AgentSession(
                session_id="owned-session",
                space_id=space_one_id,
                task_public_id=str(task_one.public_id),
                companion_id=31,
                root_path=str(tmp_path / "one"),
                agent_type="codex-cli",
                status="active",
            )
        )
        s.add_all(
            [
                AgentMessage(
                    session_id="owned-session",
                    role="user",
                    request_id="",
                    content="hello",
                    done=True,
                    created_at=dt.datetime(2026, 1, 3, 9, 0, 0),
                ),
                AgentMessage(
                    session_id="owned-session",
                    role="assistant",
                    request_id="request-1",
                    content="working",
                    done=False,
                    error="",
                    created_at=dt.datetime(2026, 1, 3, 9, 1, 0),
                ),
            ]
        )

    result = agent_sessions.get_agent_session_messages(
        space_one_id,
        " owned-session ",
    ).to_dict()

    assert result["ok"] is True
    assert result["session"] == {"session_id": "owned-session", "status": "active"}
    assert [message["role"] for message in result["messages"]] == [
        "user",
        "assistant",
    ]
    assert result["messages"] == [
        {
            "id": result["messages"][0]["id"],
            "role": "user",
            "request_id": "",
            "content": "hello",
            "done": True,
            "error": "",
            "created_at": "2026-01-03T09:00:00",
        },
        {
            "id": result["messages"][1]["id"],
            "role": "assistant",
            "request_id": "request-1",
            "content": "working",
            "done": False,
            "error": "",
            "created_at": "2026-01-03T09:01:00",
        },
    ]

    with pytest.raises(
        agent_sessions.AgentSessionNotFound, match="Agent session not found"
    ):
        agent_sessions.get_agent_session_messages(space_two_id, "owned-session")


def test_get_agent_session_messages_rejects_empty_session_id():
    from openfocus.domains.agent_spaces import agent_sessions

    with pytest.raises(
        agent_sessions.AgentSessionValidationError, match="session_id is required"
    ):
        agent_sessions.get_agent_session_messages(1, "  ")


def test_get_agent_session_messages_reports_missing_session():
    from openfocus.domains.agent_spaces import agent_sessions

    with pytest.raises(
        agent_sessions.AgentSessionNotFound, match="Agent session not found"
    ):
        agent_sessions.get_agent_session_messages(1, "missing-session")


def test_require_agent_session_returns_normalized_session_id_for_sse(tmp_path):
    from openfocus.db import session_scope
    from openfocus.domains.agent_spaces import agent_sessions
    from openfocus.models import AgentSession, AgentSpace, Goal, Task

    with session_scope() as s:
        goal = Goal(title="g", content="d", due_date=dt.date.today())
        s.add(goal)
        s.flush()
        task = Task(goal_id=goal.id, title="t", content="d", status="todo")
        s.add(task)
        s.flush()
        space = AgentSpace(
            task_public_id=str(task.public_id),
            companion_id=23,
            root_path=str(tmp_path),
            agent_type="codex-cli",
        )
        s.add(space)
        s.flush()
        space_id = int(space.id)
        s.add(
            AgentSession(
                session_id="sse-session",
                space_id=space_id,
                task_public_id=str(task.public_id),
                companion_id=23,
                root_path=str(tmp_path),
                agent_type="codex-cli",
                status="active",
            )
        )

    result = agent_sessions.require_agent_session(space_id, " sse-session ")

    assert result.session_id == "sse-session"
    assert result.space_id == space_id
    assert result.to_dict() == {"ok": True, "session": {"session_id": "sse-session"}}

    with pytest.raises(
        agent_sessions.AgentSessionValidationError, match="session_id is required"
    ):
        agent_sessions.require_agent_session(space_id, " ")

    with pytest.raises(
        agent_sessions.AgentSessionNotFound, match="Agent session not found"
    ):
        agent_sessions.require_agent_session(space_id, "missing-session")


def test_release_agent_space_for_task_is_idempotent_without_space():
    async def _run() -> None:
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import workspace
        from openfocus.models import Goal, Task

        missing_task_result = await workspace.release_agent_space_for_task(
            "missing-task"
        )

        assert missing_task_result.ok is True
        assert missing_task_result.released is False
        assert missing_task_result.terminal_ids == []
        assert missing_task_result.session_ids == []

        with session_scope() as s:
            goal = Goal(title="g", content="d", due_date=dt.date.today())
            s.add(goal)
            s.flush()
            task = Task(goal_id=goal.id, title="t", content="d", status="todo")
            s.add(task)
            s.flush()
            task_public_id = str(task.public_id)

        no_space_result = await workspace.release_agent_space_for_task(task_public_id)

        assert no_space_result.ok is True
        assert no_space_result.released is False
        assert no_space_result.task_public_id == task_public_id

    asyncio.run(_run())


def test_release_agent_space_for_task_deletes_workspace_records(tmp_path):
    async def _run() -> None:
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import terminals as terminal_records
        from openfocus.domains.agent_spaces import workspace
        from openfocus.models import (
            AgentMessage,
            AgentSession,
            AgentSpace,
            Goal,
            RemoteTerminalOutput,
            RemoteTerminalSession,
            Task,
        )

        with session_scope() as s:
            goal = Goal(title="g", content="d", due_date=dt.date.today())
            s.add(goal)
            s.flush()
            task = Task(goal_id=goal.id, title="t", content="d", status="todo")
            s.add(task)
            s.flush()
            task_public_id = str(task.public_id)
            space = AgentSpace(
                task_public_id=task_public_id,
                companion_id=7,
                root_path=str(tmp_path),
            )
            s.add(space)
            s.flush()
            space_id = int(space.id)
            owner = terminal_records.owner_for_agent_space(space_id)
            terminal_records.create_terminal_record(
                s,
                owner=owner,
                task_public_id=task_public_id,
                companion_id=None,
                root_path=str(tmp_path),
                terminal_id="term-1",
                backend="ttyd",
                connect_url="http://127.0.0.1:7681",
            )
            s.add(
                RemoteTerminalOutput(
                    space_id=owner.db_space_id,
                    terminal_id="term-1",
                    data_b64=base64.b64encode(b"output").decode("ascii"),
                    nbytes=6,
                )
            )
            s.add(
                AgentSession(
                    session_id="session-1",
                    space_id=space_id,
                    task_public_id=task_public_id,
                    companion_id=7,
                    root_path=str(tmp_path),
                    agent_type="trae-cli",
                )
            )
            s.add(
                AgentMessage(
                    session_id="session-1",
                    role="assistant",
                    request_id="request-1",
                    content="done",
                    done=True,
                )
            )

        runtime = FakeWorkspaceRuntime()

        result = await workspace.release_agent_space_for_task(
            task_public_id,
            runtime_resolver=lambda companion_id: (
                runtime if companion_id == 7 else None
            ),
        )

        assert result.ok is True
        assert result.released is True
        assert result.space_id == space_id
        assert result.terminal_ids == ["term-1"]
        assert result.session_ids == ["session-1"]
        assert [item["session_id"] for item in runtime.agent_terminates] == [
            "session-1"
        ]
        assert [item["terminal_id"] for item in runtime.stops] == ["term-1"]

        with session_scope() as s:
            assert s.get(AgentSpace, space_id) is None
            assert s.query(AgentSession).count() == 0
            assert s.query(AgentMessage).count() == 0
            assert s.query(RemoteTerminalSession).count() == 0
            assert s.query(RemoteTerminalOutput).count() == 0

    asyncio.run(_run())


def test_release_agent_space_for_task_cleans_locally_when_agent_terminate_fails(
    tmp_path,
):
    async def _run() -> None:
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import workspace
        from openfocus.models import (
            AgentMessage,
            AgentSession,
            AgentSpace,
            Goal,
            Task,
        )

        with session_scope() as s:
            goal = Goal(title="g", content="d", due_date=dt.date.today())
            s.add(goal)
            s.flush()
            task = Task(goal_id=goal.id, title="t", content="d", status="todo")
            s.add(task)
            s.flush()
            task_public_id = str(task.public_id)
            space = AgentSpace(
                task_public_id=task_public_id,
                companion_id=13,
                root_path=str(tmp_path),
            )
            s.add(space)
            s.flush()
            space_id = int(space.id)
            s.add(
                AgentSession(
                    session_id="session-fails-terminate",
                    space_id=space_id,
                    task_public_id=task_public_id,
                    companion_id=13,
                    root_path=str(tmp_path),
                    agent_type="trae-cli",
                )
            )
            s.add(
                AgentMessage(
                    session_id="session-fails-terminate",
                    role="assistant",
                    request_id="request-1",
                    content="still running",
                    done=False,
                )
            )

        runtime = FakeWorkspaceRuntime(fail_agent_terminate=True)

        result = await workspace.release_agent_space_for_task(
            task_public_id,
            runtime_resolver=lambda companion_id: (
                runtime if companion_id == 13 else None
            ),
        )

        assert result.ok is True
        assert result.released is True
        assert result.session_ids == ["session-fails-terminate"]
        assert [item["session_id"] for item in runtime.agent_terminates] == [
            "session-fails-terminate"
        ]
        with session_scope() as s:
            assert s.get(AgentSpace, space_id) is None
            assert s.query(AgentSession).count() == 0
            assert s.query(AgentMessage).count() == 0

    asyncio.run(_run())


def test_release_agent_space_for_task_cleans_locally_when_runtime_is_unavailable(
    tmp_path,
):
    async def _run() -> None:
        from openfocus.db import session_scope
        from openfocus.domains.agent_spaces import terminals as terminal_records
        from openfocus.domains.agent_spaces import workspace
        from openfocus.models import (
            AgentSpace,
            Goal,
            RemoteTerminalOutput,
            RemoteTerminalSession,
            Task,
        )

        with session_scope() as s:
            goal = Goal(title="g", content="d", due_date=dt.date.today())
            s.add(goal)
            s.flush()
            task = Task(goal_id=goal.id, title="t", content="d", status="todo")
            s.add(task)
            s.flush()
            task_public_id = str(task.public_id)
            space = AgentSpace(
                task_public_id=task_public_id,
                companion_id=11,
                root_path=str(tmp_path),
            )
            s.add(space)
            s.flush()
            space_id = int(space.id)
            owner = terminal_records.owner_for_agent_space(space_id)
            terminal_records.create_terminal_record(
                s,
                owner=owner,
                task_public_id=task_public_id,
                companion_id=11,
                root_path=str(tmp_path),
                terminal_id="offline-term",
                backend="ttyd",
                connect_url="http://127.0.0.1:7681",
            )
            s.add(
                RemoteTerminalOutput(
                    space_id=owner.db_space_id,
                    terminal_id="offline-term",
                    data_b64=base64.b64encode(b"output").decode("ascii"),
                    nbytes=6,
                )
            )

        def offline_resolver(_companion_id: int):
            raise RuntimeError("companion offline")

        result = await workspace.release_agent_space_for_task(
            task_public_id,
            runtime_resolver=offline_resolver,
        )

        assert result.ok is True
        assert result.released is True
        assert result.terminal_ids == ["offline-term"]
        with session_scope() as s:
            assert s.get(AgentSpace, space_id) is None
            assert s.query(RemoteTerminalSession).count() == 0
            assert s.query(RemoteTerminalOutput).count() == 0

    asyncio.run(_run())
