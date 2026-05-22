# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ...db import session_scope
from ...models import AgentSession, AgentSpace


class AgentSessionRuntimePort(Protocol):
    async def request_agent_start(self, **kwargs: Any) -> Any: ...

    async def request_agent_terminate(self, **kwargs: Any) -> Any: ...


class AgentSessionUseCaseError(RuntimeError):
    pass


class AgentSessionValidationError(AgentSessionUseCaseError):
    pass


class AgentSessionNotFound(AgentSessionUseCaseError):
    pass


@dataclass(frozen=True)
class AgentSessionStartResult:
    session_id: str
    space_id: int
    task_public_id: str
    companion_id: int | None
    root_path: str
    agent_type: str


@dataclass(frozen=True)
class AgentSessionTerminateResult:
    session_id: str
    space_id: int
    status: str


def _new_session_id(session_id_factory: Callable[[], object] | None) -> str:
    raw = session_id_factory() if session_id_factory is not None else uuid.uuid4()
    session_id = str(raw or "").strip()
    if not session_id:
        raise AgentSessionValidationError("generated session_id is empty")
    return session_id


async def start_agent_session(
    space_id: int,
    *,
    runtime: AgentSessionRuntimePort,
    companion_id: int | None = None,
    session_id_factory: Callable[[], object] | None = None,
    timeout_seconds: float = 10.0,
) -> AgentSessionStartResult:
    with session_scope() as s:
        space = s.get(AgentSpace, int(space_id))
        if space is None:
            raise AgentSessionNotFound("AgentSpace not found")

        resolved_space_id = int(space.id)
        task_public_id = str(space.task_public_id or "")
        resolved_companion_id = (
            int(companion_id)
            if companion_id is not None
            else (
                int(getattr(space, "companion_id", 0) or 0)
                if getattr(space, "companion_id", None)
                else None
            )
        )
        root_path = str(space.root_path or "")
        agent_type = str(space.agent_type or "trae-cli")

    requested_session_id = _new_session_id(session_id_factory)
    res = await runtime.request_agent_start(
        session_id=requested_session_id,
        root_path=root_path,
        agent_type=agent_type,
        task_public_id=task_public_id,
        timeout_seconds=timeout_seconds,
    )
    real_session_id = str(getattr(res, "session_id", "") or "").strip()
    if not real_session_id:
        real_session_id = requested_session_id

    with session_scope() as s:
        session = AgentSession(
            session_id=real_session_id,
            space_id=resolved_space_id,
            task_public_id=task_public_id,
            companion_id=resolved_companion_id,
            root_path=root_path,
            agent_type=agent_type,
            status="active",
        )
        s.add(session)
        s.flush()

    return AgentSessionStartResult(
        session_id=real_session_id,
        space_id=resolved_space_id,
        task_public_id=task_public_id,
        companion_id=resolved_companion_id,
        root_path=root_path,
        agent_type=agent_type,
    )


async def terminate_agent_session(
    space_id: int,
    session_id: str,
    *,
    runtime: AgentSessionRuntimePort,
    timeout_seconds: float = 10.0,
) -> AgentSessionTerminateResult:
    clean_session_id = str(session_id or "").strip()
    if not clean_session_id:
        raise AgentSessionValidationError("session_id is required")

    with session_scope() as s:
        session = (
            s.query(AgentSession)
            .filter(AgentSession.session_id == clean_session_id)
            .one_or_none()
        )
        if session is None or int(session.space_id) != int(space_id):
            raise AgentSessionNotFound("Agent session not found")

    await runtime.request_agent_terminate(
        session_id=clean_session_id,
        timeout_seconds=timeout_seconds,
    )

    final_status = "terminated"
    with session_scope() as s:
        session = (
            s.query(AgentSession)
            .filter(AgentSession.session_id == clean_session_id)
            .one_or_none()
        )
        if session is not None:
            session.status = final_status
            s.add(session)
        else:
            final_status = "missing"

    return AgentSessionTerminateResult(
        session_id=clean_session_id,
        space_id=int(space_id),
        status=final_status,
    )
