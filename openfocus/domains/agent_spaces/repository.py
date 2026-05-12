# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from sqlalchemy.orm import Session

from ...models import RemoteTerminalOutput, RemoteTerminalSession
from .constants import TERMINAL_STATUS_CLOSED


class TerminalRepository:
    def __init__(self, s: Session):
        self.s = s

    def list_by_owner(
        self, *, owner_type: str, owner_id: int, include_closed: bool = False
    ) -> list[RemoteTerminalSession]:
        query = (
            self.s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.owner_type == str(owner_type))
            .filter(RemoteTerminalSession.owner_id == int(owner_id))
        )
        if not include_closed:
            query = query.filter(RemoteTerminalSession.status != TERMINAL_STATUS_CLOSED)
        return query.order_by(RemoteTerminalSession.id.asc()).all()

    def get_by_owner_and_terminal_id(
        self, *, owner_type: str, owner_id: int, terminal_id: str
    ) -> RemoteTerminalSession | None:
        return (
            self.s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == str(terminal_id or "").strip())
            .filter(RemoteTerminalSession.owner_type == str(owner_type))
            .filter(RemoteTerminalSession.owner_id == int(owner_id))
            .one_or_none()
        )

    def add(self, terminal: RemoteTerminalSession) -> RemoteTerminalSession:
        self.s.add(terminal)
        self.s.flush()
        return terminal

    def delete(self, terminal: RemoteTerminalSession) -> None:
        self.s.delete(terminal)

    def delete_outputs_by_terminal_ids(self, terminal_ids: list[str]) -> None:
        clean_ids = [
            str(tid or "").strip() for tid in terminal_ids if str(tid or "").strip()
        ]
        if clean_ids:
            self.s.query(RemoteTerminalOutput).filter(
                RemoteTerminalOutput.terminal_id.in_(clean_ids)
            ).delete(synchronize_session=False)

    def delete_owner_sessions(self, *, owner_type: str, owner_id: int) -> None:
        self.s.query(RemoteTerminalSession).filter(
            RemoteTerminalSession.owner_type == str(owner_type)
        ).filter(RemoteTerminalSession.owner_id == int(owner_id)).delete(
            synchronize_session=False
        )
