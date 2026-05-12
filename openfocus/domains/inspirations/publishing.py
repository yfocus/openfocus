# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable

from sqlalchemy.orm import Session

from ...db import session_scope
from ...domains.goals import service as goal_service
from ...models import (
    Goal,
    InspirationDraft,
    InspirationMessage,
    InspirationPublishRecord,
    InspirationResource,
    InspirationSpace,
    Task,
)
from . import presenters, resources


class PublishError(Exception):
    """Base class for inspiration publishing domain errors."""


class PublishUnavailable(PublishError):
    """Raised when the space/draft cannot be published."""


class PublishConflict(PublishError):
    """Raised when a publish is blocked by an in-flight operation."""


def latest_draft(s: Session, space_id: int) -> InspirationDraft | None:
    return (
        s.query(InspirationDraft)
        .filter(InspirationDraft.space_id == int(space_id))
        .order_by(InspirationDraft.version.desc(), InspirationDraft.id.desc())
        .first()
    )


def is_waiting(s: Session, space_id: int) -> bool:
    return (
        s.query(InspirationMessage.id)
        .filter(InspirationMessage.space_id == int(space_id))
        .filter(InspirationMessage.kind == "pending")
        .first()
        is not None
    )


def space_or_error(s: Session, space_id: int) -> InspirationSpace:
    space = s.get(InspirationSpace, int(space_id))
    if space is None:
        raise PublishUnavailable("Inspiration space not found")
    workspace = resources.workspace_path(space, int(space_id))
    if not str(getattr(space, "workspace_path", "") or "").strip():
        space.workspace_path = str(workspace)
    if not str(getattr(space, "mode", "") or "").strip():
        space.mode = "built_in"
    return space


def _publishable_tasks(draft: InspirationDraft) -> list[dict]:
    picked_tasks: list[dict] = []
    for raw in draft.tasks or []:
        if not isinstance(raw, dict):
            continue
        item = {
            "title": str(raw.get("title") or "").strip()[:512],
            "description": str(raw.get("description") or "").strip()[:4000],
        }
        if item["title"]:
            picked_tasks.append(item)
    return picked_tasks


def build_published_summary(
    *,
    space: InspirationSpace,
    draft: InspirationDraft,
    goal: Goal,
    created_tasks: list[Task],
    deferred_tasks: list[dict],
) -> str:
    lines = [
        "Idea",
        str(space.title or "").strip() or str(goal.title or "").strip(),
        "",
        "Why now",
        str(goal.content or "").strip()
        or "Captured from the latest inspiration discussion.",
        "",
        "Goal",
        str(goal.title or "").strip(),
        "",
        "Published tasks",
    ]
    for task in created_tasks:
        lines.append(f"- {str(task.title or '').strip()}")
    if not created_tasks:
        lines.append("- No tasks were published.")
    lines.extend(["", "Open questions"])
    for item in draft.open_questions or []:
        if str(item or "").strip():
            lines.append(f"- {str(item).strip()}")
    if len(lines) and lines[-1] == "Open questions":
        lines.append("- None")
    lines.extend(["", "Rejected / deferred ideas"])
    deferred_titles = [
        str((it or {}).get("title") or "").strip()
        for it in (deferred_tasks or [])
        if isinstance(it, dict)
    ]
    combined = deferred_titles + [
        str(item or "").strip()
        for item in (draft.rejected_or_deferred_ideas or [])
        if str(item or "").strip()
    ]
    seen: set[str] = set()
    for item in combined:
        if not item or item in seen:
            continue
        seen.add(item)
        lines.append(f"- {item}")
    if len(lines) and lines[-1] == "Rejected / deferred ideas":
        lines.append("- None")
    return "\n".join(lines).strip()


def prepare_publish(space_id: int, draft_id: int | None, due_date: dt.date) -> dict:
    with session_scope() as s:
        space = space_or_error(s, int(space_id))
        current_status = str(space.status or "open")
        if current_status not in {"open", "closed"}:
            raise PublishUnavailable("This space cannot be published")
        if is_waiting(s, int(space_id)):
            raise PublishConflict("Agent is still responding")
        draft: InspirationDraft | None
        if draft_id is None:
            draft = latest_draft(s, int(space_id))
        else:
            draft = s.get(InspirationDraft, int(draft_id))
            if draft is not None and int(draft.space_id) != int(space_id):
                draft = None
        if draft is None:
            raise PublishUnavailable("No draft is available for publishing")
        if not _publishable_tasks(draft):
            raise PublishUnavailable("The draft does not contain publishable tasks")
        space.status = "publishing"
        space.last_activity_at = resources.utcnow()
        return {
            "draft_id": int(draft.id),
            "previous_status": current_status,
            "due_date": due_date.isoformat(),
        }


def load_publish_snapshot(space_id: int, draft_id: int) -> dict:
    with session_scope() as s:
        space = space_or_error(s, int(space_id))
        if str(space.status or "") != "publishing":
            raise RuntimeError("Inspiration space is not in publishing state")
        draft = s.get(InspirationDraft, int(draft_id))
        if draft is None or int(draft.space_id) != int(space_id):
            raise RuntimeError("Draft not found during publishing")

        picked_tasks = _publishable_tasks(draft)
        if not picked_tasks:
            raise RuntimeError("The draft does not contain publishable tasks")

        return {
            "space_title": str(space.title or "").strip(),
            "goal_title": str(draft.goal_title or space.title).strip()[:2000]
            or space.title,
            "goal_description": str(draft.goal_description or "").strip()[:4000],
            "picked_tasks": picked_tasks,
            "draft_payload": presenters.draft_payload(draft),
        }


def publish_sync(
    *,
    space_id: int,
    draft_id: int,
    due_date_iso: str,
    previous_status: str,
    load_snapshot: Callable[[int, int], dict] | None = None,
    audit: Callable[..., None] | None = None,
) -> None:
    due_date = dt.date.fromisoformat(str(due_date_iso))
    created_goal_id = 0
    created_task_ids: list[int] = []
    draft_payload: dict | None = None
    load_snapshot_func = load_snapshot or load_publish_snapshot
    try:
        publish_snapshot = load_snapshot_func(int(space_id), int(draft_id))
        picked_tasks = list(publish_snapshot.get("picked_tasks") or [])
        draft_payload = publish_snapshot.get("draft_payload") or None
        goal_title = str(publish_snapshot.get("goal_title") or "").strip()
        goal_content = str(publish_snapshot.get("goal_description") or "").strip()

        with session_scope() as s:
            space = space_or_error(s, int(space_id))
            if str(space.status or "") != "publishing":
                raise RuntimeError("Inspiration space is not in publishing state")
            draft = s.get(InspirationDraft, int(draft_id))
            if draft is None or int(draft.space_id) != int(space_id):
                raise RuntimeError("Draft not found during publishing")
            goal = goal_service.create_goal(
                s,
                title=goal_title,
                content=goal_content,
                due_date=due_date,
                agent="inspiration",
                source="inspiration",
                source_inspiration_space_id=int(space_id),
                source_inspiration_draft_id=int(draft.id),
            )
            created_goal_id = int(goal.id)

            created_tasks: list[Task] = []
            for item in picked_tasks:
                title = str(item.get("title") or "").strip()
                description = str(item.get("description") or "").strip()
                task = goal_service.create_task(
                    s,
                    goal_id=int(goal.id),
                    title=title,
                    content=description,
                    agent="inspiration",
                    source="inspiration",
                    source_inspiration_space_id=int(space_id),
                    source_inspiration_draft_id=int(draft.id),
                )
                created_tasks.append(task)
                created_task_ids.append(int(task.id))

            summary_text = build_published_summary(
                space=space,
                draft=draft,
                goal=goal,
                created_tasks=created_tasks,
                deferred_tasks=[],
            )
            seq_id = resources.next_resource_seq(s, int(space_id))
            summary_resource = InspirationResource(
                space_id=int(space_id),
                resource_seq_id=int(seq_id),
                type="summary",
                name="Published Summary",
                text_content=summary_text,
                url_content="",
                file_path="",
                source="system",
                is_system_generated=True,
            )
            s.add(summary_resource)
            resources.write_resource_file(summary_resource, space)
            s.flush()
            record = InspirationPublishRecord(
                space_id=int(space_id),
                draft_id=int(draft.id),
                created_goal_id=int(goal.id),
                created_task_ids=created_task_ids,
                deferred_tasks=[],
                summary_resource_id=int(summary_resource.id),
            )
            s.add(record)
            space.status = "publishing_releasing"
            space.published_goal_id = int(goal.id)
            space.published_at = resources.utcnow()
            space.last_activity_at = resources.utcnow()
            s.add(
                InspirationMessage(
                    space_id=int(space_id),
                    role="assistant",
                    kind="published",
                    content=f"Published draft v{int(draft.version)} into Goal #{int(goal.id)} with {len(created_tasks)} tasks.",
                    payload={
                        "draft_id": int(draft.id),
                        "created_goal_id": int(goal.id),
                        "created_task_ids": created_task_ids,
                    },
                    draft_version=int(draft.version),
                )
            )
    except Exception as e:
        with session_scope() as s:
            space = s.get(InspirationSpace, int(space_id))
            if space is not None:
                space.status = previous_status
                space.last_activity_at = resources.utcnow()
                s.add(
                    InspirationMessage(
                        space_id=int(space_id),
                        role="assistant",
                        kind="error",
                        content=f"Failed to publish the draft: {str(e)}",
                        payload={"error": str(e), "draft_id": int(draft_id)},
                    )
                )
        if audit is not None:
            audit(
                kind="inspiration.publish_error",
                source="web",
                summary=f"Failed publishing inspiration space {int(space_id)}.",
                detail=str(e),
                metadata={"space_id": int(space_id), "draft_id": int(draft_id)},
            )
        return

    if audit is not None:
        audit(
            kind="inspiration.published",
            source="web",
            summary=f"Published inspiration space {int(space_id)} into goal {int(created_goal_id)}.",
            detail=json.dumps(
                {
                    "draft": draft_payload or {},
                    "created_goal_id": created_goal_id,
                    "created_task_ids": created_task_ids,
                },
                ensure_ascii=False,
                indent=2,
            ),
            goal_id=created_goal_id,
            metadata={"space_id": int(space_id), "created_task_ids": created_task_ids},
        )
