# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ...db import session_scope
from ...models import AgentMessage, AgentSession, AgentSpace
from ..terminals import gateway as terminal_gateway
from . import terminals as terminal_records


class AgentSessionRuntimePort(Protocol):
    async def request_agent_terminate(self, **kwargs: Any) -> Any: ...


class AgentSpaceRuntimePort(
    terminal_gateway.TerminalRuntimePort, AgentSessionRuntimePort, Protocol
):
    pass


@dataclass(frozen=True)
class ReleaseAgentSpaceResult:
    ok: bool
    released: bool
    space_id: int | None = None
    task_public_id: str = ""
    terminal_ids: list[str] | None = None
    session_ids: list[str] | None = None

    def __post_init__(self) -> None:
        if self.terminal_ids is None:
            object.__setattr__(self, "terminal_ids", [])
        if self.session_ids is None:
            object.__setattr__(self, "session_ids", [])

    def to_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "released": bool(self.released),
            "space_id": self.space_id,
            "task_public_id": self.task_public_id,
            "terminal_ids": list(self.terminal_ids or []),
            "session_ids": list(self.session_ids or []),
        }


async def release_agent_space_for_task(
    task_public_id: str,
    *,
    runtime_resolver: Callable[[int], AgentSpaceRuntimePort | None] | None = None,
    terminal_ops: terminal_gateway.RemoteTerminalGateway | None = None,
    timeout_seconds: float = 5.0,
    agent_terminate_timeout_seconds: float = 10.0,
) -> ReleaseAgentSpaceResult:
    clean_task_public_id = str(task_public_id or "").strip()
    terminal_ops = terminal_ops or terminal_gateway.RemoteTerminalGateway()

    with session_scope() as s:
        space = (
            s.query(AgentSpace)
            .filter(AgentSpace.task_public_id == clean_task_public_id)
            .one_or_none()
        )
        if space is None:
            return ReleaseAgentSpaceResult(
                ok=True,
                released=False,
                task_public_id=clean_task_public_id,
            )

        space_id = int(space.id)
        space_companion_id = int(getattr(space, "companion_id", 0) or 0)
        owner = terminal_records.owner_for_agent_space(space_id)
        sessions = s.query(AgentSession).filter(AgentSession.space_id == space_id).all()
        session_ids = [
            str(session.session_id or "").strip()
            for session in sessions
            if str(session.session_id or "").strip()
        ]
        session_runtime_refs = [
            (
                str(session.session_id or "").strip(),
                int(getattr(session, "companion_id", 0) or 0) or space_companion_id,
            )
            for session in sessions
            if str(session.session_id or "").strip()
        ]

    fallback_runtime = None
    if runtime_resolver is not None and space_companion_id:
        with contextlib.suppress(Exception):
            fallback_runtime = runtime_resolver(space_companion_id)

    if runtime_resolver is not None:
        for session_id, companion_id in session_runtime_refs:
            if not companion_id:
                continue
            with contextlib.suppress(Exception):
                session_runtime = runtime_resolver(companion_id)
                if session_runtime is not None:
                    await session_runtime.request_agent_terminate(
                        session_id=session_id,
                        timeout_seconds=agent_terminate_timeout_seconds,
                    )

    terminal_ids = await terminal_ops.release_owner_terminals(
        owner=owner,
        runtime=fallback_runtime,
        runtime_resolver=None if fallback_runtime is not None else runtime_resolver,
        timeout_seconds=timeout_seconds,
        delete_local_records=False,
    )

    with session_scope() as s:
        space = (
            s.query(AgentSpace)
            .filter(AgentSpace.task_public_id == clean_task_public_id)
            .one_or_none()
        )
        if space is None:
            return ReleaseAgentSpaceResult(
                ok=True,
                released=False,
                space_id=space_id,
                task_public_id=clean_task_public_id,
                terminal_ids=terminal_ids,
                session_ids=session_ids,
            )

        if session_ids:
            s.query(AgentMessage).filter(
                AgentMessage.session_id.in_(session_ids)
            ).delete(synchronize_session=False)
            s.query(AgentSession).filter(
                AgentSession.session_id.in_(session_ids)
            ).delete(synchronize_session=False)

        terminal_records.delete_owner_terminal_records(s, owner=owner)
        s.delete(space)

    return ReleaseAgentSpaceResult(
        ok=True,
        released=True,
        space_id=space_id,
        task_public_id=clean_task_public_id,
        terminal_ids=terminal_ids,
        session_ids=session_ids,
    )
