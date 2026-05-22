# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ...db import session_scope
from ...domains.agent_activity import service as agent_activity_service
from ...models import AgentMessage, AgentSession, AgentSpace


class AgentSessionRuntimePort(Protocol):
    async def request_agent_start(self, **kwargs: Any) -> Any: ...

    async def request_agent_terminate(self, **kwargs: Any) -> Any: ...


class RuntimeTurnProjector(Protocol):
    def __call__(
        self,
        db_session: Any,
        *,
        kind: str,
        agent_runtime: str,
        session_id: str,
        turn_id: str,
        task_public_id: str,
        companion_id: int | None,
        source: str,
        payload: dict[str, Any],
    ) -> Any: ...


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


@dataclass(frozen=True)
class AgentSessionSendResult:
    session_id: str
    space_id: int
    request_id: str
    user_text: str
    task_public_id: str
    agent_type: str
    companion_id: int | None


@dataclass(frozen=True)
class AgentSessionAssistantTurnResult:
    session_id: str
    space_id: int
    request_id: str
    task_public_id: str
    agent_type: str
    companion_id: int | None
    projection: Any = None


@dataclass(frozen=True)
class AgentSessionAssistantTurnFailureResult:
    session_id: str
    request_id: str
    error: str
    projection: Any = None


def _new_session_id(session_id_factory: Callable[[], object] | None) -> str:
    raw = session_id_factory() if session_id_factory is not None else uuid.uuid4()
    session_id = str(raw or "").strip()
    if not session_id:
        raise AgentSessionValidationError("generated session_id is empty")
    return session_id


def _new_request_id(request_id_factory: Callable[[], object] | None) -> str:
    raw = request_id_factory() if request_id_factory is not None else uuid.uuid4()
    request_id = str(raw or "").strip()
    if not request_id:
        raise AgentSessionValidationError("generated request_id is empty")
    return request_id


def _default_runtime_turn_projector(
    db_session: Any,
    *,
    kind: str,
    agent_runtime: str,
    session_id: str,
    turn_id: str,
    task_public_id: str,
    companion_id: int | None,
    source: str,
    payload: dict[str, Any],
) -> Any:
    return agent_activity_service.handle_runtime_signal(
        db_session,
        kind=kind,
        agent_runtime=agent_runtime,
        session_id=session_id,
        turn_id=turn_id,
        task_public_id=task_public_id,
        companion_id=companion_id,
        source=source,
        payload=payload,
    )


def _project_runtime_turn(
    db_session: Any,
    *,
    projector: RuntimeTurnProjector | None,
    kind: str,
    send_context: AgentSessionSendResult,
    payload: dict[str, Any],
) -> Any:
    selected_projector = projector or _default_runtime_turn_projector
    return selected_projector(
        db_session,
        kind=kind,
        agent_runtime=send_context.agent_type,
        session_id=send_context.session_id,
        turn_id=send_context.request_id,
        task_public_id=send_context.task_public_id,
        companion_id=send_context.companion_id,
        source="openfocus.agent_session.send",
        payload=payload,
    )


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


def send_agent_session_message(
    space_id: int,
    session_id: str,
    user_text: str,
    *,
    request_id_factory: Callable[[], object] | None = None,
) -> AgentSessionSendResult:
    clean_session_id = str(session_id or "").strip()
    if not clean_session_id:
        raise AgentSessionValidationError("session_id is required")

    clean_user_text = str(user_text or "")
    if not clean_user_text.strip():
        raise AgentSessionValidationError("text is required")

    resolved_space_id = int(space_id)

    with session_scope() as s:
        session = (
            s.query(AgentSession)
            .filter(AgentSession.session_id == clean_session_id)
            .one_or_none()
        )
        if session is None or int(session.space_id) != resolved_space_id:
            raise AgentSessionNotFound("Agent session not found")

        task_public_id = str(session.task_public_id or "")
        agent_type = str(session.agent_type or "agent")
        companion_id = int(session.companion_id) if session.companion_id else None
        request_id = _new_request_id(request_id_factory)

        s.add(
            AgentMessage(
                session_id=clean_session_id,
                role="user",
                content=clean_user_text,
                request_id="",
                done=True,
            )
        )
        s.flush()

    return AgentSessionSendResult(
        session_id=clean_session_id,
        space_id=resolved_space_id,
        request_id=request_id,
        user_text=clean_user_text,
        task_public_id=task_public_id,
        agent_type=agent_type,
        companion_id=companion_id,
    )


def begin_agent_session_assistant_turn(
    send_context: AgentSessionSendResult,
    *,
    projector: RuntimeTurnProjector | None = None,
) -> AgentSessionAssistantTurnResult:
    with session_scope() as s:
        s.add(
            AgentMessage(
                session_id=send_context.session_id,
                role="assistant",
                content="",
                request_id=send_context.request_id,
                done=False,
            )
        )
        projection = _project_runtime_turn(
            s,
            projector=projector,
            kind="runtime.turn.started",
            send_context=send_context,
            payload={"message": "Prompt submitted from OpenFocus AgentSpace."},
        )
        s.flush()

    return AgentSessionAssistantTurnResult(
        session_id=send_context.session_id,
        space_id=send_context.space_id,
        request_id=send_context.request_id,
        task_public_id=send_context.task_public_id,
        agent_type=send_context.agent_type,
        companion_id=send_context.companion_id,
        projection=projection,
    )


def fail_agent_session_assistant_turn(
    send_context: AgentSessionSendResult,
    error: object,
    *,
    projector: RuntimeTurnProjector | None = None,
) -> AgentSessionAssistantTurnFailureResult:
    error_text = str(error or "")
    with session_scope() as s:
        message = (
            s.query(AgentMessage)
            .filter(AgentMessage.session_id == send_context.session_id)
            .filter(AgentMessage.request_id == send_context.request_id)
            .filter(AgentMessage.role == "assistant")
            .order_by(AgentMessage.id.desc())
            .first()
        )
        if message is not None:
            message.done = True
            message.error = error_text
            s.add(message)

        projection = _project_runtime_turn(
            s,
            projector=projector,
            kind="runtime.turn.failed",
            send_context=send_context,
            payload={"error": error_text},
        )

    return AgentSessionAssistantTurnFailureResult(
        session_id=send_context.session_id,
        request_id=send_context.request_id,
        error=error_text,
        projection=projection,
    )
