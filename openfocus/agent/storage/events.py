from __future__ import annotations

from dataclasses import dataclass

from ...db import session_scope
from ...models import Event
from ..core.types import EventSink, Json


@dataclass
class DbEventSink(EventSink):
    default_task_id: str | None = None

    def emit(
        self,
        kind: str,
        agent: str,
        payload: Json | None = None,
        task_id: str | None = None,
    ) -> None:
        with session_scope() as s:
            s.add(
                Event(
                    kind=kind,
                    agent=agent,
                    task_id=task_id or self.default_task_id,
                    payload=payload or {},
                )
            )
