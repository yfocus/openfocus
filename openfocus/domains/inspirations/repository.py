# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from sqlalchemy.orm import Session

from ...models import (
    InspirationDraft,
    InspirationMessage,
    InspirationPublishRecord,
    InspirationResource,
    InspirationSpace,
)


class InspirationSpaceRepository:
    def __init__(self, s: Session):
        self.s = s

    def get(self, space_id: int) -> InspirationSpace | None:
        return self.s.get(InspirationSpace, int(space_id))

    def add(self, space: InspirationSpace) -> InspirationSpace:
        self.s.add(space)
        self.s.flush()
        return space

    def delete(self, space: InspirationSpace) -> None:
        self.s.delete(space)


class InspirationMessageRepository:
    def __init__(self, s: Session):
        self.s = s

    def add(self, message: InspirationMessage) -> InspirationMessage:
        self.s.add(message)
        self.s.flush()
        return message

    def list_by_space(self, space_id: int) -> list[InspirationMessage]:
        return (
            self.s.query(InspirationMessage)
            .filter(InspirationMessage.space_id == int(space_id))
            .order_by(InspirationMessage.id.asc())
            .all()
        )


class InspirationResourceRepository:
    def __init__(self, s: Session):
        self.s = s

    def add(self, resource: InspirationResource) -> InspirationResource:
        self.s.add(resource)
        self.s.flush()
        return resource

    def next_seq(self, space_id: int) -> int:
        row = (
            self.s.query(InspirationResource.resource_seq_id)
            .filter(InspirationResource.space_id == int(space_id))
            .order_by(InspirationResource.resource_seq_id.desc())
            .first()
        )
        try:
            return int((row[0] if row else 0) or 0) + 1
        except Exception:
            return 1

    def list_active(
        self, space_id: int, *, include_summary: bool = True
    ) -> list[InspirationResource]:
        query = (
            self.s.query(InspirationResource)
            .filter(InspirationResource.space_id == int(space_id))
            .filter(InspirationResource.deleted_at.is_(None))
        )
        if not include_summary:
            query = query.filter(InspirationResource.type != "summary")
        return query.order_by(
            InspirationResource.updated_at.desc(), InspirationResource.id.desc()
        ).all()


class InspirationDraftRepository:
    def __init__(self, s: Session):
        self.s = s

    def get(self, draft_id: int) -> InspirationDraft | None:
        return self.s.get(InspirationDraft, int(draft_id))

    def add(self, draft: InspirationDraft) -> InspirationDraft:
        self.s.add(draft)
        self.s.flush()
        return draft


class InspirationPublishRecordRepository:
    def __init__(self, s: Session):
        self.s = s

    def add(self, record: InspirationPublishRecord) -> InspirationPublishRecord:
        self.s.add(record)
        self.s.flush()
        return record
