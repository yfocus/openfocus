# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from ...models import RemoteTerminalOutput, RemoteTerminalSession

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
        if self.kind == "agent_space":
            return int(self.id)
        if self.kind == "inspiration_space":
            return -int(self.id)
        raise ValueError(f"unsupported terminal owner kind: {self.kind}")


def owner_for_agent_space(space_id: int) -> TerminalOwner:
    return TerminalOwner(kind="agent_space", id=int(space_id))


def owner_for_inspiration_space(space_id: int) -> TerminalOwner:
    return TerminalOwner(kind="inspiration_space", id=int(space_id))


def list_terminals(s: Session, owner: TerminalOwner) -> list[RemoteTerminalSession]:
    return (
        s.query(RemoteTerminalSession)
        .filter(RemoteTerminalSession.space_id == owner.db_space_id)
        .filter(RemoteTerminalSession.status != "closed")
        .order_by(RemoteTerminalSession.id.asc())
        .all()
    )


def terminal_payload(t: RemoteTerminalSession) -> dict:
    backend = str(getattr(t, "backend", "") or "ttyd").strip() or "ttyd"
    tid = str(t.terminal_id or "")
    return {
        "terminal_id": tid,
        "name": str(t.name or ""),
        "status": str(t.status or "active"),
        "backend": backend,
        "created_at": t.created_at.isoformat()
        if hasattr(t.created_at, "isoformat")
        else str(t.created_at),
    }


def next_terminal_name(s: Session, owner: TerminalOwner) -> str:
    used = {
        str((t.name or "").strip())
        for t in s.query(RemoteTerminalSession)
        .filter(RemoteTerminalSession.space_id == owner.db_space_id)
        .all()
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
        space_id=owner.db_space_id,
        task_public_id=str(task_public_id or ""),
        companion_id=companion_id,
        root_path=str(root_path or ""),
        name=name,
        terminal_id=str(terminal_id or ""),
        backend=str(backend or "ttyd").strip() or "ttyd",
        connect_url=str(connect_url or "").strip(),
        status="active",
    )
    s.add(terminal)
    s.flush()
    return terminal


def get_terminal_for_owner(
    s: Session, *, owner: TerminalOwner, terminal_id: str
) -> RemoteTerminalSession:
    tid = str(terminal_id or "").strip()
    terminal = (
        s.query(RemoteTerminalSession)
        .filter(RemoteTerminalSession.terminal_id == tid)
        .one_or_none()
    )
    if terminal is None or int(terminal.space_id) != owner.db_space_id:
        raise TerminalNotFound("Terminal not found")
    return terminal


def rename_terminal(
    s: Session, *, owner: TerminalOwner, terminal_id: str, name: str
) -> RemoteTerminalSession:
    raw_name = str(name or "").strip()
    terminal = get_terminal_for_owner(s, owner=owner, terminal_id=terminal_id)
    duplicate = (
        s.query(RemoteTerminalSession)
        .filter(RemoteTerminalSession.space_id == owner.db_space_id)
        .filter(RemoteTerminalSession.terminal_id != str(terminal_id or "").strip())
        .filter(RemoteTerminalSession.name == raw_name)
        .one_or_none()
    )
    if duplicate is not None:
        raise TerminalNameConflict("name already exists")
    terminal.name = raw_name
    s.add(terminal)
    return terminal


def delete_terminal_record(s: Session, *, terminal_id: str) -> None:
    tid = str(terminal_id or "").strip()
    s.query(RemoteTerminalSession).filter(
        RemoteTerminalSession.terminal_id == tid
    ).delete(synchronize_session=False)
    s.query(RemoteTerminalOutput).filter(
        RemoteTerminalOutput.terminal_id == tid
    ).delete(synchronize_session=False)


def delete_owner_terminal_records(s: Session, *, owner: TerminalOwner) -> None:
    s.query(RemoteTerminalSession).filter(
        RemoteTerminalSession.space_id == owner.db_space_id
    ).delete(synchronize_session=False)
    s.query(RemoteTerminalOutput).filter(
        RemoteTerminalOutput.space_id == owner.db_space_id
    ).delete(synchronize_session=False)
