# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json
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
from . import presenters, publishing, resources, service


class InspirationWorkspaceError(Exception):
    """Base class for Inspiration workspace use-case errors."""


class InspirationWorkspaceNotFound(InspirationWorkspaceError):
    """Raised when an Inspiration workspace cannot be found."""


class InspirationWorkspaceValidationError(InspirationWorkspaceError):
    """Raised when a request is invalid for the workspace state."""


class InspirationWorkspaceConflict(InspirationWorkspaceError):
    """Raised when a valid workspace request conflicts with in-flight work."""


class InspirationWorkspaceResourceNotFound(InspirationWorkspaceError):
    """Raised when an Inspiration resource cannot be found."""


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


@dataclass(frozen=True)
class StoredResourceFile:
    seq_id: int
    path: Path
    uploaded_name: str


@dataclass(frozen=True)
class ResourceUploadSlot:
    space_id: int
    seq_id: int


@dataclass(frozen=True)
class ResourceMutationResult:
    space_id: int
    resource_id: int
    item: dict
    audit_kind: str | None = None
    audit_summary: str = ""
    audit_detail: str = ""
    audit_metadata: dict | None = None


@dataclass(frozen=True)
class ResourceDeleteResult:
    space_id: int
    resource_id: int


@dataclass(frozen=True)
class ResourceRawFileResult:
    path: Path
    media_type: str
    filename: str


@dataclass(frozen=True)
class ResourceSyncResult:
    space_id: int
    synced: bool
    items: list[dict]
    item: dict | None
    audit_kind: str
    audit_summary: str
    audit_detail: str
    audit_metadata: dict


@dataclass(frozen=True)
class DraftGenerationRequest:
    space_id: int
    prompt: str


@dataclass(frozen=True)
class WorkspacePublishPrepareResult:
    space_id: int
    draft_id: int
    previous_status: str
    due_date: str


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


def _ensure_open(space: InspirationSpace, message: str) -> None:
    if str(space.status or "open") != "open":
        raise InspirationWorkspaceValidationError(message)


def _resource_or_error(
    s, space_id: int, resource_id: int, *, file_lookup: bool = False
) -> InspirationResource:
    resource = s.get(InspirationResource, int(resource_id))
    if resource is None or int(resource.space_id) != int(space_id):
        raise InspirationWorkspaceResourceNotFound("Resource not found")
    if resource.deleted_at is not None:
        detail = "File resource not found" if file_lookup else "Resource not found"
        raise InspirationWorkspaceResourceNotFound(detail)
    return resource


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


def list_drafts(space_id: int) -> dict:
    with session_scope() as s:
        _space_or_error(s, int(space_id))
        drafts = (
            s.query(InspirationDraft)
            .filter(InspirationDraft.space_id == int(space_id))
            .order_by(InspirationDraft.version.desc(), InspirationDraft.id.desc())
            .all()
        )
        items = [presenters.draft_payload(draft) for draft in drafts]
    return {"ok": True, "items": items}


def prepare_draft_from_draft_summary(space_id: int) -> DraftGenerationRequest:
    with session_scope() as s:
        space = _space_or_error(s, int(space_id))
        try:
            summary_path = (
                resources.writable_resources_dir(space, int(space_id))
                / "draft_summary.md"
            )
        except resources.ResourceStorageError as exc:
            raise InspirationWorkspaceValidationError(str(exc)) from exc
        if not summary_path.is_file():
            raise InspirationWorkspaceValidationError("Summary is missing")
        try:
            item = resources.sync_draft_summary_file(s, space)
        except resources.EmptyDraftSummary as exc:
            raise InspirationWorkspaceValidationError(str(exc)) from exc
        except resources.DraftSummaryReadError as exc:
            raise InspirationWorkspaceValidationError(str(exc)) from exc
        except resources.ResourceStorageError as exc:
            raise InspirationWorkspaceValidationError(str(exc)) from exc
        if item is None:
            raise InspirationWorkspaceValidationError("Summary is missing")
    return DraftGenerationRequest(space_id=int(space_id), prompt="/plan")


def prepare_publish(
    space_id: int, draft_id: int | None, due_date: dt.date
) -> WorkspacePublishPrepareResult:
    try:
        prepared = publishing.prepare_publish(int(space_id), draft_id, due_date)
    except publishing.PublishConflict as exc:
        raise InspirationWorkspaceConflict(str(exc)) from exc
    except publishing.PublishUnavailable as exc:
        detail = str(exc)
        if detail == "Inspiration space not found":
            raise InspirationWorkspaceNotFound(detail) from exc
        raise InspirationWorkspaceValidationError(detail) from exc

    return WorkspacePublishPrepareResult(
        space_id=int(space_id),
        draft_id=int(prepared["draft_id"]),
        previous_status=str(prepared["previous_status"]),
        due_date=str(prepared["due_date"]),
    )


def fork_space(
    space_id: int,
    *,
    title: str = "",
    include_all_resources: bool = False,
    resource_ids: set[int] | list[int] | tuple[int, ...] | None = None,
) -> WorkspaceLifecycleResult:
    selected_resource_ids: set[int] = set()
    for item in resource_ids or set():
        try:
            selected_resource_ids.add(int(item))
        except Exception:
            continue

    with session_scope() as s:
        source_space = _space_or_error(s, int(space_id))
        raw_title = str(title or "").strip()
        target_title = (
            raw_title[:512]
            if raw_title
            else service.default_followup_title(str(source_space.title or ""))
        )
        now = resources.utcnow()
        forked = InspirationSpace(
            title=target_title,
            status="open",
            mode=str(getattr(source_space, "mode", "") or "built_in"),
            forked_from_space_id=int(source_space.id),
            last_activity_at=now,
        )
        s.add(forked)
        s.flush()
        new_space_id = int(forked.id)
        forked.workspace_path = str(resources.workspace_path(forked, new_space_id))

        source_resources = resources.non_deleted_resources(s, int(space_id))
        seq_id = 1
        for resource in source_resources:
            if str(resource.type or "") == "summary":
                resources.clone_resource(
                    s=s,
                    source=resource,
                    target_space_id=new_space_id,
                    seq_id=seq_id,
                )
                seq_id += 1
                continue
            if include_all_resources or int(resource.id) in selected_resource_ids:
                resources.clone_resource(
                    s=s,
                    source=resource,
                    target_space_id=new_space_id,
                    seq_id=seq_id,
                )
                seq_id += 1

        s.add(
            InspirationMessage(
                space_id=new_space_id,
                role="assistant",
                kind="system",
                content=(
                    f"Forked from Inspiration space #{int(source_space.id)}. "
                    "The published summary is preserved here so you can continue exploring a follow-up direction."
                ),
            )
        )
        item = presenters.space_payload(forked)

    return WorkspaceLifecycleResult(
        space_id=int(new_space_id),
        item=item,
        release_terminals=False,
        audit_kind="inspiration.forked",
        audit_summary=(
            f"Forked inspiration space {int(space_id)} into {int(item['id'])}."
        ),
        audit_detail=str(item.get("title") or ""),
        audit_metadata={
            "space_id": int(space_id),
            "forked_space_id": int(item["id"]),
        },
    )


def prepare_draft_from_resource(
    space_id: int, resource_id: int | None
) -> DraftGenerationRequest:
    try:
        normalized_resource_id = int(resource_id or 0)
    except (TypeError, ValueError):
        normalized_resource_id = 0
    if normalized_resource_id <= 0:
        raise InspirationWorkspaceValidationError("resource_id is required")

    with session_scope() as s:
        _space_or_error(s, int(space_id))
        resource = _resource_or_error(s, int(space_id), normalized_resource_id)
        resource_ref = resources.resource_reference(resource)
    prompt = (
        "/plan\n"
        "Create a Goal and Tasks using this resource as the primary source. "
        "If it follows the OpenFocus bridge Markdown format, map the level-1 heading to the goal title, "
        "the content under it to the goal content, and each level-2 heading plus its body to one task.\n\n"
        f"{resource_ref}"
    )
    return DraftGenerationRequest(space_id=int(space_id), prompt=prompt)


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


def prepare_resource_upload(space_id: int) -> ResourceUploadSlot:
    with session_scope() as s:
        space = _space_or_error(s, int(space_id))
        _ensure_open(space, "Only open spaces accept new resources")
        try:
            resources.writable_resources_dir(space, int(space_id))
        except resources.ResourceStorageError as exc:
            raise InspirationWorkspaceValidationError(str(exc)) from exc
        seq_id = resources.next_resource_seq(s, int(space_id))
    return ResourceUploadSlot(space_id=int(space_id), seq_id=int(seq_id))


def prepare_resource_file_replacement(
    space_id: int, resource_id: int
) -> ResourceUploadSlot:
    with session_scope() as s:
        space = _space_or_error(s, int(space_id))
        _ensure_open(space, "Only open spaces can replace resource files")
        resource = _resource_or_error(s, int(space_id), int(resource_id))
        if str(resource.type or "") != "image":
            raise InspirationWorkspaceValidationError(
                "Only image resources support replace"
            )
        try:
            resources.writable_resources_dir(space, int(space_id))
        except resources.ResourceStorageError as exc:
            raise InspirationWorkspaceValidationError(str(exc)) from exc
        seq_id = int(resource.resource_seq_id or 0)
    return ResourceUploadSlot(space_id=int(space_id), seq_id=seq_id)


def create_resource(
    space_id: int,
    *,
    resource_type: str,
    name: str | None = None,
    text_content: str | None = None,
    url_content: str | None = None,
    stored_file: StoredResourceFile | None = None,
) -> ResourceMutationResult:
    normalized_type = str(resource_type or "").strip().lower()
    if normalized_type not in {"url", "image", "text", "summary"}:
        raise InspirationWorkspaceValidationError("unsupported resource type")

    with session_scope() as s:
        space = _space_or_error(s, int(space_id))
        _ensure_open(space, "Only open spaces accept new resources")
        seq_id = (
            int(stored_file.seq_id)
            if stored_file is not None
            else resources.next_resource_seq(s, int(space_id))
        )
        resource_name = str(name or "").strip()
        now = resources.utcnow()
        resource = InspirationResource(
            space_id=int(space_id),
            resource_seq_id=seq_id,
            type=normalized_type,
            name=resource_name or f"resource-{seq_id}",
            text_content="",
            url_content="",
            file_path="",
            is_system_generated=False,
        )
        if normalized_type == "url":
            url_text = str(url_content or "").strip()
            if not url_text:
                raise InspirationWorkspaceValidationError("url_content is required")
            resource.url_content = url_text[:4000]
            resource.source = "user"
            if not resource_name:
                resource.name = url_text[:512]
        elif normalized_type in {"text", "summary"}:
            body = str(text_content or "").strip()
            if not body:
                raise InspirationWorkspaceValidationError("text_content is required")
            resource.text_content = body[:20000]
            resource.source = "user"
            if normalized_type == "summary":
                resource.is_system_generated = True
                resource.source = "built_in_agent"
            if not resource_name:
                resource.name = f"{normalized_type}-{seq_id}"
        else:
            if stored_file is None:
                raise InspirationWorkspaceValidationError(
                    "file is required for image resources"
                )
            safe_file_path = resources.safe_resource_file_path(
                space, int(space_id), stored_file.path
            )
            if safe_file_path is None:
                raise InspirationWorkspaceValidationError("unsafe resource file path")
            resource.file_path = str(safe_file_path)
            try:
                resource.external_path = str(
                    safe_file_path.relative_to(
                        resources.workspace_path(space, int(space_id))
                    )
                )
            except Exception:
                resource.external_path = str(safe_file_path)
            resource.source = "user"
            resource.name = resource_name or str(stored_file.uploaded_name or "image")
        s.add(resource)
        if normalized_type in {"url", "text", "summary"}:
            try:
                resources.write_resource_file(resource, space)
            except resources.ResourceStorageError as exc:
                raise InspirationWorkspaceValidationError(str(exc)) from exc
        space.last_activity_at = now
        s.flush()
        payload = presenters.resource_payload(
            int(space_id), resource, include_text=True
        )

    return ResourceMutationResult(
        space_id=int(space_id),
        resource_id=int(payload["id"]),
        item=payload,
        audit_kind="inspiration.resource_added",
        audit_summary=(
            f"Added a {normalized_type} resource to inspiration space {int(space_id)}."
        ),
        audit_detail=str(payload.get("reference") or ""),
        audit_metadata={
            "space_id": int(space_id),
            "resource_id": int(payload["id"]),
            "resource_type": normalized_type,
        },
    )


def update_resource(
    space_id: int, resource_id: int, payload: dict
) -> ResourceMutationResult:
    if not isinstance(payload, dict):
        raise InspirationWorkspaceValidationError("invalid payload")

    with session_scope() as s:
        space = _space_or_error(s, int(space_id))
        _ensure_open(space, "Only open spaces can edit resources")
        resource = _resource_or_error(s, int(space_id), int(resource_id))
        if "name" in payload:
            name = str(payload.get("name") or "").strip()
            if name:
                resource.name = name[:512]
        if str(resource.type or "") == "url" and "url_content" in payload:
            url_text = str(payload.get("url_content") or "").strip()
            if not url_text:
                raise InspirationWorkspaceValidationError("url_content is required")
            resource.url_content = url_text[:4000]
        if (
            str(resource.type or "") in {"text", "summary"}
            and "text_content" in payload
        ):
            body = str(payload.get("text_content") or "").strip()
            if not body:
                raise InspirationWorkspaceValidationError("text_content is required")
            resource.text_content = body[:20000]
        if str(resource.type or "") in {"url", "text", "summary"}:
            try:
                resources.write_resource_file(resource, space)
            except resources.ResourceStorageError as exc:
                raise InspirationWorkspaceValidationError(str(exc)) from exc
        space.last_activity_at = resources.utcnow()
        payload_out = presenters.resource_payload(
            int(space_id), resource, include_text=True
        )
    return ResourceMutationResult(
        space_id=int(space_id), resource_id=int(resource_id), item=payload_out
    )


def replace_resource_file(
    space_id: int,
    resource_id: int,
    *,
    stored_file: StoredResourceFile,
    name: str | None = None,
) -> ResourceMutationResult:
    old_file_to_delete: Path | None = None
    with session_scope() as s:
        space = _space_or_error(s, int(space_id))
        _ensure_open(space, "Only open spaces can replace resource files")
        resource = _resource_or_error(s, int(space_id), int(resource_id))
        if str(resource.type or "") != "image":
            raise InspirationWorkspaceValidationError(
                "Only image resources support replace"
            )
        new_path_obj = resources.safe_resource_file_path(
            space, int(space_id), stored_file.path
        )
        if new_path_obj is None:
            raise InspirationWorkspaceValidationError("unsafe resource file path")
        old_path_raw = str(resource.file_path or "").strip()
        if old_path_raw:
            old_path = resources.safe_resource_file_path(
                space, int(space_id), old_path_raw
            )
            if old_path is not None and old_path != new_path_obj:
                old_file_to_delete = old_path
        resource.file_path = str(new_path_obj)
        try:
            resource.external_path = str(
                new_path_obj.relative_to(resources.workspace_path(space, int(space_id)))
            )
        except Exception:
            resource.external_path = str(new_path_obj)
        next_name = str(name or "").strip()
        if next_name:
            resource.name = next_name[:512]
        elif not str(resource.name or "").strip():
            resource.name = str(stored_file.uploaded_name or "image")
        space.last_activity_at = resources.utcnow()
        payload_out = presenters.resource_payload(
            int(space_id), resource, include_text=True
        )

    if old_file_to_delete is not None:
        try:
            if not old_file_to_delete.is_symlink() and old_file_to_delete.is_file():
                old_file_to_delete.unlink()
        except Exception:
            pass
    return ResourceMutationResult(
        space_id=int(space_id), resource_id=int(resource_id), item=payload_out
    )


def delete_resource(space_id: int, resource_id: int) -> ResourceDeleteResult:
    file_to_delete: Path | None = None
    with session_scope() as s:
        space = _space_or_error(s, int(space_id))
        _ensure_open(space, "Only open spaces can delete resources")
        resource = s.get(InspirationResource, int(resource_id))
        if resource is None or int(resource.space_id) != int(space_id):
            raise InspirationWorkspaceResourceNotFound("Resource not found")
        if resource.deleted_at is None:
            raw_file_path = str(resource.file_path or "").strip()
            if raw_file_path:
                file_to_delete = resources.safe_resource_file_path(
                    space, int(space_id), raw_file_path
                )
            resource.deleted_at = resources.utcnow()
            space.last_activity_at = resources.utcnow()
    if file_to_delete is not None:
        try:
            if not file_to_delete.is_symlink() and file_to_delete.is_file():
                file_to_delete.unlink()
        except Exception:
            pass
    return ResourceDeleteResult(space_id=int(space_id), resource_id=int(resource_id))


def raw_resource_file(space_id: int, resource_id: int) -> ResourceRawFileResult:
    with session_scope() as s:
        space = _space_or_error(s, int(space_id))
        resource = _resource_or_error(
            s, int(space_id), int(resource_id), file_lookup=True
        )
        if not str(resource.file_path or "").strip():
            raise InspirationWorkspaceResourceNotFound("File resource not found")
        file_path = resources.safe_resource_file_path(
            space, int(space_id), str(resource.file_path or "")
        )
        if file_path is None:
            raise InspirationWorkspaceResourceNotFound("File resource not found")
        return ResourceRawFileResult(
            path=file_path,
            media_type=resources.guess_media_type(file_path),
            filename=str(resource.name or file_path.name),
        )


def sync_resources(space_id: int) -> ResourceSyncResult:
    with session_scope() as s:
        space = _space_or_error(s, int(space_id))
        if str(space.status or "open") == "published":
            raise InspirationWorkspaceValidationError("Published spaces are read-only")
        items = resources.sync_resources_dir(s, space)
        payloads = [
            presenters.resource_payload(int(space_id), item, include_text=True)
            for item in items
        ]
        draft_item = next(
            (
                item
                for item in payloads
                if item.get("external_path") == "resources/draft_summary.md"
            ),
            None,
        )
    return ResourceSyncResult(
        space_id=int(space_id),
        synced=bool(payloads),
        items=payloads,
        item=draft_item,
        audit_kind="inspiration.resources_synced",
        audit_summary=(
            f"Synced resources directory for inspiration space {int(space_id)}."
        ),
        audit_detail=json.dumps(
            [item.get("external_path") for item in payloads], ensure_ascii=False
        )[:4000],
        audit_metadata={"space_id": int(space_id), "resource_count": len(payloads)},
    )


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
