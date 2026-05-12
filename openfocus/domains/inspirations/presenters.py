# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from ...models import (
    InspirationDraft,
    InspirationMessage,
    InspirationPublishRecord,
    InspirationResource,
    InspirationSpace,
)
from .resources import resource_preview, resource_reference


def space_payload(
    space: InspirationSpace,
    *,
    latest_draft: InspirationDraft | None = None,
    resource_count: int | None = None,
    draft_count: int | None = None,
    publish_count: int | None = None,
) -> dict:
    return {
        "id": int(space.id),
        "title": str(space.title or ""),
        "status": str(space.status or "open"),
        "mode": str(getattr(space, "mode", "") or "built_in"),
        "workspace_path": str(getattr(space, "workspace_path", "") or ""),
        "published_goal_id": (
            int(space.published_goal_id)
            if getattr(space, "published_goal_id", None)
            else None
        ),
        "forked_from_space_id": (
            int(space.forked_from_space_id)
            if getattr(space, "forked_from_space_id", None)
            else None
        ),
        "message_turn_count": int(space.message_turn_count or 0),
        "resource_count": int(resource_count or 0),
        "draft_count": int(draft_count or 0),
        "publish_count": int(publish_count or 0),
        "latest_draft_version": (
            int(latest_draft.version) if latest_draft is not None else None
        ),
        "created_at": space.created_at.isoformat() if space.created_at else None,
        "updated_at": space.updated_at.isoformat() if space.updated_at else None,
        "last_activity_at": (
            space.last_activity_at.isoformat()
            if getattr(space, "last_activity_at", None)
            else None
        ),
        "closed_at": space.closed_at.isoformat()
        if getattr(space, "closed_at", None)
        else None,
        "published_at": (
            space.published_at.isoformat()
            if getattr(space, "published_at", None)
            else None
        ),
    }


def message_payload(message: InspirationMessage) -> dict:
    return {
        "id": int(message.id),
        "space_id": int(message.space_id),
        "role": str(message.role or "assistant"),
        "kind": str(message.kind or "message"),
        "content": str(message.content or ""),
        "payload": message.payload or {},
        "draft_version": (
            int(message.draft_version)
            if getattr(message, "draft_version", None)
            else None
        ),
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def resource_payload(
    space_id: int, resource: InspirationResource, *, include_text: bool = False
) -> dict:
    file_path = str(resource.file_path or "").strip()
    return {
        "id": int(resource.id),
        "space_id": int(resource.space_id),
        "resource_seq_id": int(resource.resource_seq_id),
        "type": str(resource.type or "text"),
        "name": str(resource.name or f"resource-{int(resource.resource_seq_id or 0)}"),
        "preview": resource_preview(resource),
        "reference": resource_reference(resource),
        "url_content": str(resource.url_content or ""),
        "text_content": str(resource.text_content or "") if include_text else "",
        "external_path": str(getattr(resource, "external_path", "") or ""),
        "source": str(getattr(resource, "source", "") or "user"),
        "is_system_generated": bool(resource.is_system_generated),
        "has_file": bool(file_path),
        "raw_url": (
            f"/api/inspirations/{int(space_id)}/resources/{int(resource.id)}/raw"
            if file_path
            else None
        ),
        "created_at": resource.created_at.isoformat() if resource.created_at else None,
        "updated_at": resource.updated_at.isoformat() if resource.updated_at else None,
    }


def draft_payload(draft: InspirationDraft) -> dict:
    return {
        "id": int(draft.id),
        "space_id": int(draft.space_id),
        "version": int(draft.version),
        "goal_title": str(draft.goal_title or ""),
        "goal_description": str(draft.goal_description or ""),
        "tasks": draft.tasks or [],
        "open_questions": draft.open_questions or [],
        "rejected_or_deferred_ideas": draft.rejected_or_deferred_ideas or [],
        "source_message_id": (
            int(draft.source_message_id)
            if getattr(draft, "source_message_id", None)
            else None
        ),
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
    }


def publish_record_payload(record: InspirationPublishRecord) -> dict:
    return {
        "id": int(record.id),
        "space_id": int(record.space_id),
        "draft_id": int(record.draft_id),
        "created_goal_id": int(record.created_goal_id),
        "created_task_ids": [int(x) for x in (record.created_task_ids or [])],
        "deferred_tasks": record.deferred_tasks or [],
        "summary_resource_id": (
            int(record.summary_resource_id)
            if getattr(record, "summary_resource_id", None)
            else None
        ),
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }
