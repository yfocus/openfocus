# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import datetime as dt


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
