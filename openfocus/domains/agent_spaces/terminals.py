# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from ...models import RemoteTerminalSession
from .constants import (
    TERMINAL_OWNER_AGENT_SPACE,
    TERMINAL_OWNER_INSPIRATION_SPACE,
    TERMINAL_STATUS_ACTIVE,
)
from .repository import TerminalRepository

TerminalOwnerKind = Literal["agent_space", "inspiration_space"]


class TerminalNotFound(LookupError):
    """Raised when a terminal does not belong to the requested owner."""


class TerminalNameConflict(ValueError):
    """Raised when a terminal name already exists for the owner."""


@dataclass(frozen=True)
class TerminalOwner:
    kind: TerminalOwnerKind
    id: int

    @property
    def db_space_id(self) -> int:
        """Legacy space_id representation retained only for old output rows."""

        if self.kind == TERMINAL_OWNER_AGENT_SPACE:
            return int(self.id)
        if self.kind == TERMINAL_OWNER_INSPIRATION_SPACE:
            return -int(self.id)
        raise ValueError(f"unsupported terminal owner kind: {self.kind}")

    @property
    def owner_type(self) -> str:
        return str(self.kind)

    @property
    def owner_id(self) -> int:
        return int(self.id)


def owner_for_agent_space(space_id: int) -> TerminalOwner:
    return TerminalOwner(kind=TERMINAL_OWNER_AGENT_SPACE, id=int(space_id))


def owner_for_inspiration_space(space_id: int) -> TerminalOwner:
    return TerminalOwner(kind=TERMINAL_OWNER_INSPIRATION_SPACE, id=int(space_id))


def list_terminals(s: Session, owner: TerminalOwner) -> list[RemoteTerminalSession]:
    return TerminalRepository(s).list_by_owner(
        owner_type=owner.owner_type, owner_id=owner.owner_id
    )


def terminal_payload(t: RemoteTerminalSession) -> dict:
    backend = str(getattr(t, "backend", "") or "ttyd").strip() or "ttyd"
    tid = str(t.terminal_id or "")
    return {
        "terminal_id": tid,
        "name": str(t.name or ""),
        "status": str(t.status or TERMINAL_STATUS_ACTIVE),
        "backend": backend,
        "created_at": t.created_at.isoformat()
        if hasattr(t.created_at, "isoformat")
        else str(t.created_at),
    }


def next_terminal_name(s: Session, owner: TerminalOwner) -> str:
    used = {
        str((t.name or "").strip())
        for t in TerminalRepository(s).list_by_owner(
            owner_type=owner.owner_type, owner_id=owner.owner_id, include_closed=True
        )
        if str((t.name or "").strip())
    }
    base = "terminal"
    if base not in used:
        return base
    i = 2
    while True:
        candidate = f"{base}-{i}"
        if candidate not in used:
            return candidate
        i += 1


def create_terminal_record(
    s: Session,
    *,
    owner: TerminalOwner,
    task_public_id: str,
    companion_id: int | None,
    root_path: str,
    terminal_id: str,
    backend: str,
    connect_url: str,
) -> RemoteTerminalSession:
    name = next_terminal_name(s, owner)
    terminal = RemoteTerminalSession(
        owner_type=owner.owner_type,
        owner_id=owner.owner_id,
        space_id=owner.db_space_id,
        task_public_id=str(task_public_id or "").strip() or None,
        companion_id=companion_id,
        root_path=str(root_path or ""),
        name=name,
        terminal_id=str(terminal_id or ""),
        backend=str(backend or "ttyd").strip() or "ttyd",
        connect_url=str(connect_url or "").strip(),
        status=TERMINAL_STATUS_ACTIVE,
    )
    return TerminalRepository(s).add(terminal)


def get_terminal_for_owner(
    s: Session, *, owner: TerminalOwner, terminal_id: str
) -> RemoteTerminalSession:
    tid = str(terminal_id or "").strip()
    terminal = TerminalRepository(s).get_by_owner_and_terminal_id(
        owner_type=owner.owner_type, owner_id=owner.owner_id, terminal_id=tid
    )
    if terminal is None:
        raise TerminalNotFound("Terminal not found")
    return terminal


def rename_terminal(
    s: Session, *, owner: TerminalOwner, terminal_id: str, name: str
) -> RemoteTerminalSession:
    raw_name = str(name or "").strip()
    terminal = get_terminal_for_owner(s, owner=owner, terminal_id=terminal_id)
    duplicate = (
        s.query(RemoteTerminalSession)
        .filter(RemoteTerminalSession.owner_type == owner.owner_type)
        .filter(RemoteTerminalSession.owner_id == owner.owner_id)
        .filter(RemoteTerminalSession.terminal_id != str(terminal_id or "").strip())
        .filter(RemoteTerminalSession.name == raw_name)
        .one_or_none()
    )
    if duplicate is not None:
        raise TerminalNameConflict("name already exists")
    terminal.name = raw_name
    s.add(terminal)
    return terminal


def delete_terminal_record(
    s: Session, *, owner: TerminalOwner, terminal_id: str
) -> None:
    repo = TerminalRepository(s)
    terminal = get_terminal_for_owner(s, owner=owner, terminal_id=terminal_id)
    tid = str(terminal.terminal_id or "").strip()
    repo.delete_outputs_by_terminal_ids([tid])
    repo.delete(terminal)


def delete_owner_terminal_records(s: Session, *, owner: TerminalOwner) -> None:
    repo = TerminalRepository(s)
    terminals = repo.list_by_owner(
        owner_type=owner.owner_type, owner_id=owner.owner_id, include_closed=True
    )
    terminal_ids = [
        str(t.terminal_id or "") for t in terminals if str(t.terminal_id or "")
    ]
    repo.delete_outputs_by_terminal_ids(terminal_ids)
    repo.delete_owner_sessions(owner_type=owner.owner_type, owner_id=owner.owner_id)
