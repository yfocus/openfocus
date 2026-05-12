# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from sqlalchemy.orm import Session

from ...models import AgentSpace, Companion


class CompanionRepository:
    def __init__(self, s: Session):
        self.s = s

    def get(self, companion_id: int) -> Companion | None:
        return self.s.get(Companion, int(companion_id))

    def get_by_device_id(self, device_id: str) -> Companion | None:
        return (
            self.s.query(Companion)
            .filter(Companion.device_id == str(device_id or ""))
            .one_or_none()
        )

    def list_recent(self, *, limit: int) -> list[Companion]:
        limit = max(1, int(limit or 1))
        return self.s.query(Companion).order_by(Companion.id.desc()).limit(limit).all()

    def list_all_recent(self) -> list[Companion]:
        return self.s.query(Companion).order_by(Companion.id.desc()).all()

    def add(self, companion: Companion) -> Companion:
        self.s.add(companion)
        self.s.flush()
        return companion

    def delete(self, companion: Companion) -> None:
        self.s.delete(companion)


class CompanionAgentSpaceRepository:
    def __init__(self, s: Session):
        self.s = s

    def list_by_companion_ids(self, companion_ids: list[int]) -> list[AgentSpace]:
        if not companion_ids:
            return []
        return (
            self.s.query(AgentSpace)
            .filter(AgentSpace.companion_id.in_([int(x) for x in companion_ids]))
            .order_by(AgentSpace.id.desc())
            .all()
        )

    def list_by_companion_id(self, companion_id: int) -> list[AgentSpace]:
        return (
            self.s.query(AgentSpace)
            .filter(AgentSpace.companion_id == int(companion_id))
            .all()
        )
