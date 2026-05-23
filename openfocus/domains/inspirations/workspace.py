# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func

from ...db import session_scope
from ...domains.agent_spaces import terminals as terminal_service
from ...models import (
    Goal,
    InspirationDraft,
    InspirationMessage,
    InspirationPublishRecord,
    InspirationResource,
    InspirationSpace,
    RemoteTerminalSession,
)
from . import presenters, resources, service


class InspirationWorkspaceError(Exception):
    """Base class for Inspiration workspace use-case errors."""


class InspirationWorkspaceNotFound(InspirationWorkspaceError):
    """Raised when an Inspiration workspace cannot be found."""


class InspirationWorkspaceValidationError(InspirationWorkspaceError):
    """Raised when a request is invalid for the workspace state."""


@dataclass(frozen=True)
class WorkspaceLifecycleResult:
    space_id: int
    item: dict
    release_terminals: bool
    audit_kind: str
    audit_summary: str
    audit_detail: str
    audit_metadata: dict


@dataclass(frozen=True)
class WorkspaceDeleteResult:
    space_id: int
    audit_kind: str
    audit_summary: str
    audit_detail: str
    audit_metadata: dict


def _space_or_error(s, space_id: int) -> InspirationSpace:
    space = s.get(InspirationSpace, int(space_id))
    if space is None:
        raise InspirationWorkspaceNotFound("Inspiration space not found")
    workspace = resources.workspace_path(space, int(space_id))
    if not str(getattr(space, "workspace_path", "") or "").strip():
        space.workspace_path = str(workspace)
    if not str(getattr(space, "mode", "") or "").strip():
        space.mode = "built_in"
    return space


def _count_by_space(s, model, space_ids: list[int]) -> dict[int, int]:
    if not space_ids:
        return {}
    rows = (
        s.query(model.space_id, func.count(model.id))
        .filter(model.space_id.in_(space_ids))
        .group_by(model.space_id)
        .all()
    )
    return {int(space_id): int(count) for space_id, count in rows}


def _resource_counts(s, space_ids: list[int]) -> dict[int, int]:
    if not space_ids:
        return {}
    rows = (
        s.query(InspirationResource.space_id, func.count(InspirationResource.id))
        .filter(InspirationResource.space_id.in_(space_ids))
        .filter(InspirationResource.deleted_at.is_(None))
        .group_by(InspirationResource.space_id)
        .all()
    )
    return {int(space_id): int(count) for space_id, count in rows}


def _latest_drafts(s, space_ids: list[int]) -> dict[int, InspirationDraft]:
    latest: dict[int, InspirationDraft] = {}
    if not space_ids:
        return latest
    drafts = (
        s.query(InspirationDraft)
        .filter(InspirationDraft.space_id.in_(space_ids))
        .order_by(
            InspirationDraft.space_id.asc(),
            InspirationDraft.version.desc(),
            InspirationDraft.id.desc(),
        )
        .all()
    )
    for draft in drafts:
        sid = int(draft.space_id)
        if sid not in latest:
            latest[sid] = draft
    return latest


def _absolute_lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _safe_workspace_delete_path(space: InspirationSpace, space_id: int) -> Path | None:
    files_root = _absolute_lexical_path(resources.files_root())
    default_dir = _absolute_lexical_path(files_root / f"space_{int(space_id)}")
    raw_workspace = str(getattr(space, "workspace_path", "") or "").strip()
    candidate = Path(raw_workspace).expanduser() if raw_workspace else default_dir
    candidate = _absolute_lexical_path(candidate)
    if candidate != default_dir:
        return None
    if files_root.is_symlink() or default_dir.is_symlink():
        return None
    try:
        files_root_resolved = files_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved == files_root_resolved:
        return None
    try:
        resolved.relative_to(files_root_resolved)
    except ValueError:
        return None
    if not candidate.is_dir():
        return None
    return candidate


def list_spaces(limit: int = 50) -> dict:
    normalized_limit = max(1, min(int(limit or 50), 200))
    with session_scope() as s:
        spaces = (
            s.query(InspirationSpace)
            .order_by(
                InspirationSpace.last_activity_at.desc(), InspirationSpace.id.desc()
            )
            .limit(normalized_limit)
            .all()
        )
        space_ids = [int(space.id) for space in spaces]
        resource_counts = _resource_counts(s, space_ids)
        draft_counts = _count_by_space(s, InspirationDraft, space_ids)
        publish_counts = _count_by_space(s, InspirationPublishRecord, space_ids)
        latest_drafts = _latest_drafts(s, space_ids)
        items = [
            presenters.space_payload(
                space,
                latest_draft=latest_drafts.get(int(space.id)),
                resource_count=resource_counts.get(int(space.id), 0),
                draft_count=draft_counts.get(int(space.id), 0),
                publish_count=publish_counts.get(int(space.id), 0),
            )
            for space in spaces
        ]
    return {"ok": True, "items": items}


def get_space_detail(
    space_id: int, *, before_id: int | None = None, page_size: int = 60
) -> dict:
    normalized_page_size = max(1, min(int(page_size or 60), 200))
    with session_scope() as s:
        space = _space_or_error(s, int(space_id))
        waiting = service.is_waiting(s, int(space_id))
        messages, next_before = service.messages_page(
            s,
            int(space_id),
            before_id=before_id,
            page_size=normalized_page_size,
        )
        active_resources = resources.non_deleted_resources(s, int(space_id))
        drafts = (
            s.query(InspirationDraft)
            .filter(InspirationDraft.space_id == int(space_id))
            .order_by(InspirationDraft.version.desc(), InspirationDraft.id.desc())
            .all()
        )
        records = (
            s.query(InspirationPublishRecord)
            .filter(InspirationPublishRecord.space_id == int(space_id))
            .order_by(InspirationPublishRecord.id.desc())
            .all()
        )
        latest_draft = drafts[0] if drafts else None
        item = presenters.space_payload(
            space,
            latest_draft=latest_draft,
            resource_count=len(active_resources),
            draft_count=len(drafts),
            publish_count=len(records),
        )
        return {
            "ok": True,
            "item": item,
            "is_waiting": waiting,
            "is_publishing": service.is_publishing(space),
            "messages": [presenters.message_payload(msg) for msg in messages],
            "next_before_id": next_before,
            "resources": [
                presenters.resource_payload(int(space_id), res, include_text=True)
                for res in active_resources
            ],
            "drafts": [presenters.draft_payload(draft) for draft in drafts],
            "publish_records": [
                presenters.publish_record_payload(record) for record in records
            ],
        }


def detail_page_context(
    space_id: int | None,
    *,
    has_online_companion: bool,
    default_due: dt.date | None = None,
) -> dict:
    with session_scope() as s:
        spaces = (
            s.query(InspirationSpace)
            .order_by(
                InspirationSpace.last_activity_at.desc(), InspirationSpace.id.desc()
            )
            .all()
        )
        space = _space_or_error(s, int(space_id)) if space_id is not None else None
        waiting = service.is_waiting(s, int(space_id)) if space is not None else False
        publishing = service.is_publishing(space)
        terminals: list[RemoteTerminalSession] = []
        inspiration_terminal: dict | None = None
        messages: list[InspirationMessage] = []
        active_resources: list[InspirationResource] = []
        published_goal: Goal | None = None
        if space is not None:
            messages = (
                s.query(InspirationMessage)
                .filter(InspirationMessage.space_id == int(space_id))
                .order_by(InspirationMessage.id.asc())
                .all()
            )
            active_resources = resources.non_deleted_resources(s, int(space_id))
            terminals = terminal_service.list_terminals(
                s, terminal_service.owner_for_inspiration_space(int(space_id))
            )
            if terminals:
                inspiration_terminal = service.terminal_payload(
                    int(space_id), terminals[0]
                )
            published_goal = (
                s.get(Goal, int(space.published_goal_id))
                if getattr(space, "published_goal_id", None)
                else None
            )
        due = default_due or (dt.date.today() + dt.timedelta(days=7))
        return {
            "spaces": spaces,
            "space": space,
            "is_waiting": waiting,
            "is_publishing": publishing,
            "messages": messages,
            "resources": active_resources,
            "inspiration_terminal": inspiration_terminal,
            "inspiration_terminal_count": len(terminals),
            "has_online_companion": bool(has_online_companion),
            "draft_summary_prompt": service.build_draft_summary_prompt(space)
            if space
            else "",
            "published_goal": published_goal,
            "default_due": due.isoformat(),
        }


def close_space(space_id: int) -> WorkspaceLifecycleResult:
    with session_scope() as s:
        space = _space_or_error(s, int(space_id))
        status = str(space.status or "open")
        if status == "published":
            raise InspirationWorkspaceValidationError(
                "Published spaces cannot be closed"
            )
        if status != "open":
            raise InspirationWorkspaceValidationError("Only open spaces can be closed")
        now = resources.utcnow()
        space.status = "closed"
        space.closed_at = now
        space.last_activity_at = now
        s.query(InspirationMessage).filter(
            InspirationMessage.space_id == int(space_id),
            InspirationMessage.kind == "draft_generated",
        ).delete(synchronize_session=False)
        s.query(InspirationDraft).filter(
            InspirationDraft.space_id == int(space_id)
        ).delete(synchronize_session=False)
        s.add(
            InspirationMessage(
                space_id=int(space_id),
                role="assistant",
                kind="system",
                content="This Inspiration space is now closed. Reopen it to continue editing.",
            )
        )
        item = presenters.space_payload(space)
    return WorkspaceLifecycleResult(
        space_id=int(space_id),
        item=item,
        release_terminals=True,
        audit_kind="inspiration.closed",
        audit_summary=f"Closed inspiration space {int(space_id)}.",
        audit_detail="User closed the inspiration space.",
        audit_metadata={"space_id": int(space_id)},
    )


def reopen_space(space_id: int) -> WorkspaceLifecycleResult:
    with session_scope() as s:
        space = _space_or_error(s, int(space_id))
        if str(space.status or "open") != "closed":
            raise InspirationWorkspaceValidationError(
                "Only closed spaces can be reopened"
            )
        now = resources.utcnow()
        space.status = "open"
        space.closed_at = None
        space.last_activity_at = now
        s.add(
            InspirationMessage(
                space_id=int(space_id),
                role="assistant",
                kind="system",
                content="This Inspiration space is open again. You can continue the discussion.",
            )
        )
        item = presenters.space_payload(space)
    return WorkspaceLifecycleResult(
        space_id=int(space_id),
        item=item,
        release_terminals=False,
        audit_kind="inspiration.reopened",
        audit_summary=f"Reopened inspiration space {int(space_id)}.",
        audit_detail="User reopened the inspiration space.",
        audit_metadata={"space_id": int(space_id)},
    )


def delete_space(space_id: int) -> WorkspaceDeleteResult:
    removed_files_dir: Path | None
    with session_scope() as s:
        space = s.get(InspirationSpace, int(space_id))
        if space is None:
            raise InspirationWorkspaceNotFound("Inspiration space not found")
        if str(space.status or "open") == "published":
            raise InspirationWorkspaceValidationError(
                "Published spaces cannot be deleted"
            )
        removed_files_dir = _safe_workspace_delete_path(space, int(space_id))
        s.query(InspirationPublishRecord).filter(
            InspirationPublishRecord.space_id == int(space_id)
        ).delete(synchronize_session=False)
        s.query(InspirationDraft).filter(
            InspirationDraft.space_id == int(space_id)
        ).delete(synchronize_session=False)
        s.query(InspirationMessage).filter(
            InspirationMessage.space_id == int(space_id)
        ).delete(synchronize_session=False)
        s.query(InspirationResource).filter(
            InspirationResource.space_id == int(space_id)
        ).delete(synchronize_session=False)
        s.delete(space)
    if removed_files_dir is not None:
        try:
            shutil.rmtree(removed_files_dir, ignore_errors=True)
        except Exception:
            pass
    return WorkspaceDeleteResult(
        space_id=int(space_id),
        audit_kind="inspiration.deleted",
        audit_summary=f"Deleted inspiration space {int(space_id)}.",
        audit_detail="User deleted the inspiration space before publication.",
        audit_metadata={"space_id": int(space_id)},
    )
