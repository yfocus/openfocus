# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from sqlalchemy.orm import Session

from ...models import Event


class EventRepository:
    def __init__(self, s: Session):
        self.s = s

    def add(self, event: Event) -> Event:
        self.s.add(event)
        self.s.flush()
        return event

    def list_recent(self, *, limit: int) -> list[Event]:
        limit = max(1, int(limit or 1))
        return self.s.query(Event).order_by(Event.id.desc()).limit(limit).all()
