# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ...db import session_scope
from ...models import AgentMessage, AgentSession, AgentSpace, Companion, Task
from ..terminals import gateway as terminal_gateway
from . import terminals as terminal_records

MAX_START_AGENT_COMMAND_LENGTH = 2000
DEFAULT_AGENT_TYPE = "trae-cli"


class AgentSessionRuntimePort(Protocol):
    async def request_agent_terminate(self, **kwargs: Any) -> Any: ...


class AgentSpaceRuntimePort(
    terminal_gateway.TerminalRuntimePort, AgentSessionRuntimePort, Protocol
):
    pass


class AgentSpaceUseCaseError(RuntimeError):
    pass


class AgentSpaceValidationError(AgentSpaceUseCaseError):
    pass


class AgentSpaceTaskNotFound(AgentSpaceUseCaseError):
    pass


class AgentSpaceNotFound(AgentSpaceUseCaseError):
    pass


class AgentSpaceCompanionNotFound(AgentSpaceUseCaseError):
    pass


class AgentSpaceCompanionUnavailable(AgentSpaceUseCaseError):
    pass


@dataclass(frozen=True)
class AgentSpacePayload:
    id: int
    task_public_id: str
    companion_id: int | None
    root_path: str
    agent_type: str
    start_agent_command: str
    prompts_enabled: bool | None = None
    prompt_autosend_enabled: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "task_public_id": self.task_public_id,
            "companion_id": self.companion_id,
            "root_path": self.root_path,
            "start_agent_command": self.start_agent_command,
        }
        if self.prompts_enabled is not None:
            payload["prompts_enabled"] = self.prompts_enabled
        if self.prompt_autosend_enabled is not None:
            payload["prompt_autosend_enabled"] = self.prompt_autosend_enabled
        return payload


@dataclass(frozen=True)
class AgentSpaceLookupResult:
    task_public_id: str
    space: AgentSpacePayload | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "space": self.space.to_dict() if self.space is not None else None,
        }


@dataclass(frozen=True)
class AgentSpaceCreateOrUpdateResult:
    space: AgentSpacePayload
    created: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "created": self.created,
            "space_id": self.space.id,
            "space": self.space.to_dict(),
        }


@dataclass(frozen=True)
class StartAgentCommandResult:
    space_id: int
    start_agent_command: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "start_agent_command": self.start_agent_command,
        }


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


def _clean_task_public_id(task_public_id: str) -> str:
    return str(task_public_id or "").strip()


def _clean_required_root_path(root_path: str) -> str:
    clean_root_path = str(root_path or "").strip()
    if not clean_root_path:
        raise AgentSpaceValidationError("root_path is required")
    return clean_root_path


def _clean_start_agent_command(command: str) -> str:
    clean_command = str(command or "").strip()
    if len(clean_command) > MAX_START_AGENT_COMMAND_LENGTH:
        raise AgentSpaceValidationError("command is too long (<=2000)")
    return clean_command


def _clean_space_id(space_id: int) -> int:
    try:
        return int(space_id)
    except (TypeError, ValueError) as exc:
        raise AgentSpaceValidationError("invalid space_id") from exc


def _clean_companion_id(companion_id: int) -> int:
    try:
        return int(companion_id)
    except (TypeError, ValueError) as exc:
        raise AgentSpaceValidationError("invalid companion_id") from exc


def _optional_bool_attr(row: Any, attr_name: str) -> bool | None:
    if not hasattr(row, attr_name):
        return None
    value = getattr(row, attr_name, None)
    if value is None:
        return None
    return bool(value)


def _agent_space_payload(space: AgentSpace) -> AgentSpacePayload:
    companion_id = (
        int(getattr(space, "companion_id", 0) or 0)
        if getattr(space, "companion_id", None)
        else None
    )
    return AgentSpacePayload(
        id=int(space.id),
        task_public_id=str(space.task_public_id or ""),
        companion_id=companion_id,
        root_path=str(space.root_path or ""),
        agent_type=str(space.agent_type or DEFAULT_AGENT_TYPE),
        start_agent_command=str(getattr(space, "start_agent_command", "") or ""),
        prompts_enabled=_optional_bool_attr(space, "prompts_enabled"),
        prompt_autosend_enabled=_optional_bool_attr(space, "prompt_autosend_enabled"),
    )


def get_agent_space_for_task(task_public_id: str) -> AgentSpaceLookupResult:
    clean_task_public_id = _clean_task_public_id(task_public_id)
    with session_scope() as s:
        space = (
            s.query(AgentSpace)
            .filter(AgentSpace.task_public_id == clean_task_public_id)
            .one_or_none()
        )
        payload = _agent_space_payload(space) if space is not None else None

    return AgentSpaceLookupResult(task_public_id=clean_task_public_id, space=payload)


def create_or_update_agent_space_for_task(
    task_public_id: str,
    *,
    companion_id: int,
    root_path: str,
    start_agent_command: str = "",
    agent_type: str = DEFAULT_AGENT_TYPE,
) -> AgentSpaceCreateOrUpdateResult:
    clean_task_public_id = _clean_task_public_id(task_public_id)
    clean_companion_id = _clean_companion_id(companion_id)
    clean_root_path = _clean_required_root_path(root_path)
    clean_start_agent_command = _clean_start_agent_command(start_agent_command)
    clean_agent_type = (
        str(agent_type or DEFAULT_AGENT_TYPE).strip() or DEFAULT_AGENT_TYPE
    )

    with session_scope() as s:
        task = (
            s.query(Task).filter(Task.public_id == clean_task_public_id).one_or_none()
        )
        if task is None:
            raise AgentSpaceTaskNotFound("Task not found")

        companion = s.get(Companion, clean_companion_id)
        if companion is None:
            raise AgentSpaceCompanionNotFound("Companion not found")
        if (
            str(companion.status or "").strip() != "active"
            or not str(companion.auth_token or "").strip()
        ):
            raise AgentSpaceCompanionUnavailable(
                "Companion is not paired or unavailable"
            )

        space = (
            s.query(AgentSpace)
            .filter(AgentSpace.task_public_id == clean_task_public_id)
            .one_or_none()
        )
        created = space is None
        if space is None:
            space = AgentSpace(task_public_id=clean_task_public_id)

        space.companion_id = clean_companion_id
        space.root_path = clean_root_path
        space.agent_type = clean_agent_type
        space.start_agent_command = clean_start_agent_command
        s.add(space)
        s.flush()
        payload = _agent_space_payload(space)

    return AgentSpaceCreateOrUpdateResult(space=payload, created=created)


def get_start_agent_command(space_id: int) -> StartAgentCommandResult:
    clean_space_id = _clean_space_id(space_id)
    with session_scope() as s:
        space = s.get(AgentSpace, clean_space_id)
        if space is None:
            raise AgentSpaceNotFound("AgentSpace not found")
        command = str(getattr(space, "start_agent_command", "") or "")

    return StartAgentCommandResult(
        space_id=clean_space_id,
        start_agent_command=command,
    )


def update_start_agent_command(
    space_id: int,
    command: str,
) -> StartAgentCommandResult:
    clean_space_id = _clean_space_id(space_id)
    clean_command = _clean_start_agent_command(command)

    with session_scope() as s:
        space = s.get(AgentSpace, clean_space_id)
        if space is None:
            raise AgentSpaceNotFound("AgentSpace not found")
        space.start_agent_command = clean_command
        s.add(space)

    return StartAgentCommandResult(
        space_id=clean_space_id,
        start_agent_command=clean_command,
    )


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
