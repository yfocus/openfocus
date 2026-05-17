# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from ...db import session_scope
from ...domains.agent_spaces import terminals as terminal_service
from ...domains.companion import service as companion_service
from ...domains.memory import service as memory_service
from ...models import (
    InspirationDraft,
    InspirationMessage,
    InspirationResource,
    InspirationSpace,
    RemoteTerminalSession,
)
from . import drafts, presenters, publishing, resources, terminal_bridge


class InspirationError(Exception):
    """Base class for inspiration domain errors."""


class InspirationNotFound(InspirationError):
    """Raised when an inspiration space cannot be found."""


class InspirationValidationError(InspirationError):
    """Raised when a user request is invalid for the current space state."""


class InspirationConflict(InspirationError):
    """Raised when an operation conflicts with in-flight work."""


class InspirationTerminalError(InspirationError):
    """Raised when terminal operations cannot be performed."""


ProviderFactory = Callable[[], tuple[Any | None, str | None]]
SelectCompanion = Callable[[int | None], tuple[Any, Any]]
ReleaseTerminalMode = Callable[[str], None]


async def store_uploaded_resource_file(*, space_id: int, seq_id: int, file) -> tuple:
    content = await file.read()
    return resources.store_uploaded_resource_bytes(
        space_id=int(space_id),
        seq_id=int(seq_id),
        original_name=str(getattr(file, "filename", "") or "image"),
        content=content,
    )


def truncate_zh(text: str, n: int = 20) -> str:
    s = (text or "").strip()
    if len(s) <= n:
        return s
    return s[:n].rstrip() + "…"


def messages_page(
    s, space_id: int, *, before_id: int | None = None, page_size: int = 60
) -> tuple[list[InspirationMessage], int | None]:
    q = s.query(InspirationMessage).filter(InspirationMessage.space_id == int(space_id))
    if before_id:
        q = q.filter(InspirationMessage.id < int(before_id))
    rows = q.order_by(InspirationMessage.id.desc()).limit(int(page_size) + 1).all()
    has_more = len(rows) > int(page_size)
    rows = rows[: int(page_size)]
    rows.reverse()
    next_before = rows[0].id if rows and has_more else None
    return rows, next_before


def maybe_emit_phase_summary(space_id: int) -> None:
    with session_scope() as s:
        space = s.get(InspirationSpace, int(space_id))
        if space is None:
            return
        now = memory_service.utcnow()
        due_turns = (
            int(space.message_turn_count or 0) - int(space.last_phase_summary_turn or 0)
            >= 10
        )
        due_time = False
        if getattr(space, "last_phase_summary_at", None) is not None:
            last = space.last_phase_summary_at
            if getattr(last, "tzinfo", None) is None:
                last = last.replace(tzinfo=dt.timezone.utc)
            due_time = (now - last).total_seconds() >= 3600
        if not due_turns and not due_time:
            return
        messages = (
            s.query(InspirationMessage)
            .filter(InspirationMessage.space_id == int(space_id))
            .order_by(InspirationMessage.id.asc())
            .all()
        )
        active_resources = resources.non_deleted_resources(s, int(space_id))
        detail = drafts.make_phase_summary(space, messages, active_resources)
        space.last_phase_summary_turn = int(space.message_turn_count or 0)
        space.last_phase_summary_at = now
        memory_service.try_audit_memory(
            kind="inspiration.phase_summary",
            source="inspiration",
            summary=f"Inspiration space {int(space_id)} reached a phase-summary checkpoint.",
            detail=detail,
            metadata={
                "space_id": int(space_id),
                "message_turn_count": int(space.message_turn_count or 0),
            },
        )


def latest_draft(s, space_id: int) -> InspirationDraft | None:
    return (
        s.query(InspirationDraft)
        .filter(InspirationDraft.space_id == int(space_id))
        .order_by(InspirationDraft.version.desc(), InspirationDraft.id.desc())
        .first()
    )


def default_followup_title(title: str) -> str:
    base = str(title or "Inspiration").strip() or "Inspiration"
    return (base + " / Follow-up")[:120]


def space_or_error(s, space_id: int) -> InspirationSpace:
    space = s.get(InspirationSpace, int(space_id))
    if space is None:
        raise InspirationNotFound("Inspiration space not found")
    workspace = resources.workspace_path(space, int(space_id))
    if not str(getattr(space, "workspace_path", "") or "").strip():
        space.workspace_path = str(workspace)
    if not str(getattr(space, "mode", "") or "").strip():
        space.mode = "built_in"
    return space


def latest_pending_message(s, space_id: int) -> InspirationMessage | None:
    return (
        s.query(InspirationMessage)
        .filter(InspirationMessage.space_id == int(space_id))
        .filter(InspirationMessage.kind == "pending")
        .order_by(InspirationMessage.id.desc())
        .first()
    )


def is_waiting(s, space_id: int) -> bool:
    return latest_pending_message(s, int(space_id)) is not None


def command_kind(content: str) -> str:
    text = str(content or "").strip()
    if text in {"/summary_title", "/summary-title"}:
        return "summary_title"
    if text in {"/plan", "/draft_goal_tasks"} or text.startswith(
        ("/plan\n", "/draft_goal_tasks\n")
    ):
        return "plan"
    return "message"


def user_message_kind(content: str) -> str:
    return "command" if command_kind(content) != "message" else "message"


def pending_text(kind: str) -> str:
    if kind == "summary_title":
        return "Generating title suggestions…"
    if kind == "plan":
        return "Generating a publish-ready draft…"
    return "Thinking…"


def is_publishing(space: InspirationSpace | None) -> bool:
    return str(getattr(space, "status", "") or "") == "publishing"


def generate_followup_result(
    *,
    command_kind: str,
    provider: Any | None,
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
    user_text: str,
) -> dict:
    if command_kind == "summary_title":
        if provider is None:
            titles = drafts.fallback_title_suggestions(space, messages)
        else:
            try:
                titles = drafts.llm_title_suggestions(
                    provider, space=space, messages=messages, resources=resources
                )
            except Exception:
                titles = drafts.fallback_title_suggestions(space, messages)
        content = "Title suggestions:\n" + "\n".join(f"- {item}" for item in titles)
        return {
            "message_kind": "title_suggestions",
            "content": content,
            "payload": {"titles": titles},
            "audit_kind": "inspiration.title_suggestions",
            "audit_detail": content,
            "audit_metadata": {"titles": titles},
        }

    if command_kind == "plan":
        if provider is None:
            data = drafts.fallback_draft(space, messages, resources)
        else:
            try:
                data = drafts.llm_draft(
                    provider, space=space, messages=messages, resources=resources
                )
            except Exception:
                data = drafts.fallback_draft(space, messages, resources)
        return {
            "message_kind": "draft_generated",
            "draft_data": data,
            "audit_kind": "inspiration.draft_generated",
            "audit_detail": "",
            "audit_metadata": {},
        }

    if provider is None:
        reply = drafts.fallback_reply(space, user_text)
    else:
        try:
            reply = drafts.llm_reply(
                provider, space=space, messages=messages, resources=resources
            )
        except Exception:
            reply = drafts.fallback_reply(space, user_text)
    return {
        "message_kind": "message",
        "content": reply,
        "payload": {},
        "audit_kind": "inspiration.message",
        "audit_detail": user_text,
        "audit_metadata": {},
    }


async def kickoff_followup(
    *,
    space_id: int,
    user_message_id: int,
    pending_message_id: int,
    provider_factory: ProviderFactory,
) -> None:
    audit_kind = "inspiration.message"
    audit_detail = ""
    audit_metadata: dict = {
        "space_id": int(space_id),
        "user_message_id": int(user_message_id),
    }
    try:
        with session_scope() as s:
            space = s.get(InspirationSpace, int(space_id))
            user_message = s.get(InspirationMessage, int(user_message_id))
            pending_message = s.get(InspirationMessage, int(pending_message_id))
            if space is None or user_message is None or pending_message is None:
                return
            messages = (
                s.query(InspirationMessage)
                .filter(InspirationMessage.space_id == int(space_id))
                .filter(InspirationMessage.kind != "pending")
                .order_by(InspirationMessage.id.asc())
                .all()
            )
            active_resources = resources.non_deleted_resources(s, int(space_id))
            kind = command_kind(str(user_message.content or ""))

        provider, _err = provider_factory()
        user_text = str(user_message.content or "")
        try:
            timeout_seconds = float(
                os.environ.get("OPENFOCUS_INSPIRATION_AGENT_TIMEOUT_SECONDS") or 120
            )
        except Exception:
            timeout_seconds = 120.0
        result = await asyncio.wait_for(
            asyncio.to_thread(
                generate_followup_result,
                command_kind=kind,
                provider=provider,
                space=space,
                messages=messages,
                resources=active_resources,
                user_text=user_text,
            ),
            timeout=max(10.0, timeout_seconds),
        )
        audit_kind = str(result.get("audit_kind") or audit_kind)
        audit_detail = str(result.get("audit_detail") or audit_detail)
        audit_metadata.update(result.get("audit_metadata") or {})

        if kind == "summary_title":
            with session_scope() as s:
                pending = s.get(InspirationMessage, int(pending_message_id))
                current_space = s.get(InspirationSpace, int(space_id))
                if pending is None or current_space is None:
                    return
                pending.kind = str(result.get("message_kind") or "title_suggestions")
                pending.content = str(result.get("content") or "")
                pending.payload = result.get("payload") or {}
                current_space.last_activity_at = memory_service.utcnow()
        elif kind == "plan":
            data = result.get("draft_data") or {}
            with session_scope() as s:
                pending = s.get(InspirationMessage, int(pending_message_id))
                current_space = s.get(InspirationSpace, int(space_id))
                if pending is None or current_space is None:
                    return
                latest = latest_draft(s, int(space_id))
                version = int(latest.version if latest is not None else 0) + 1
                draft = InspirationDraft(
                    space_id=int(space_id),
                    version=version,
                    goal_title=str(
                        data.get("goal_title") or current_space.title
                    ).strip()[:2000],
                    goal_description=str(data.get("goal_description") or "").strip()[
                        :20000
                    ],
                    tasks=data.get("tasks") or [],
                    open_questions=data.get("open_questions") or [],
                    rejected_or_deferred_ideas=data.get("rejected_or_deferred_ideas")
                    or [],
                    source_message_id=int(user_message_id),
                )
                s.add(draft)
                s.flush()
                draft_payload = presenters.draft_payload(draft)
                pending.kind = "draft_generated"
                pending.content = f"Draft v{int(draft.version)} is ready. Review it in the chat stream and decide whether to publish."
                pending.payload = {"draft": draft_payload}
                pending.draft_version = int(draft.version)
                current_space.last_activity_at = memory_service.utcnow()
            audit_detail = json.dumps(draft_payload, ensure_ascii=False, indent=2)
            audit_metadata["draft_id"] = int(draft_payload["id"])
            audit_metadata["version"] = int(draft_payload["version"])
        else:
            with session_scope() as s:
                pending = s.get(InspirationMessage, int(pending_message_id))
                current_space = s.get(InspirationSpace, int(space_id))
                if pending is None or current_space is None:
                    return
                pending.kind = str(result.get("message_kind") or "message")
                pending.content = str(result.get("content") or "")
                pending.payload = result.get("payload") or {}
                current_space.last_activity_at = memory_service.utcnow()

        await asyncio.to_thread(maybe_emit_phase_summary, int(space_id))
        await asyncio.to_thread(
            memory_service.try_audit_memory,
            kind=audit_kind,
            source="inspiration",
            summary=f"Inspiration space {int(space_id)} completed a {kind} turn.",
            detail=audit_detail,
            metadata=audit_metadata,
        )
    except Exception as e:
        with session_scope() as s:
            pending = s.get(InspirationMessage, int(pending_message_id))
            current_space = s.get(InspirationSpace, int(space_id))
            if pending is None:
                return
            pending.kind = "error"
            pending.content = f"Failed to generate a response: {str(e)}"
            pending.payload = {"error": str(e)}
            if current_space is not None:
                current_space.last_activity_at = memory_service.utcnow()
        await asyncio.to_thread(
            memory_service.try_audit_memory,
            kind="inspiration.error",
            source="inspiration",
            summary=f"Inspiration space {int(space_id)} failed to generate a response.",
            detail=str(e),
            metadata={
                "space_id": int(space_id),
                "user_message_id": int(user_message_id),
            },
        )


async def enqueue_turn(
    space_id: int,
    content: str,
    *,
    provider_factory: ProviderFactory,
    kickoff_func: Callable[..., Awaitable[None]] | None = None,
) -> dict:
    text = str(content or "").strip()
    if not text:
        raise InspirationValidationError("content is required")
    if len(text) > 20000:
        text = text[:20000]
    with session_scope() as s:
        space = space_or_error(s, int(space_id))
        if str(space.status or "open") != "open":
            raise InspirationValidationError("Only open spaces accept new messages")
        if is_waiting(s, int(space_id)):
            raise InspirationConflict("Agent is still responding")
        now = memory_service.utcnow()
        kind = command_kind(text)
        user_message = InspirationMessage(
            space_id=int(space_id),
            role="user",
            kind=user_message_kind(text),
            content=text,
        )
        pending_message = InspirationMessage(
            space_id=int(space_id),
            role="assistant",
            kind="pending",
            content=pending_text(kind),
            payload={"command": kind},
        )
        s.add(user_message)
        s.add(pending_message)
        space.message_turn_count = int(space.message_turn_count or 0) + 1
        space.last_activity_at = now
        s.flush()
        user_payload = presenters.message_payload(user_message)
        pending_payload = presenters.message_payload(pending_message)
        user_message_id = int(user_message.id)
        pending_message_id = int(pending_message.id)

    try:
        if kickoff_func is None:
            followup_coro = kickoff_followup(
                space_id=int(space_id),
                user_message_id=int(user_message_id),
                pending_message_id=int(pending_message_id),
                provider_factory=provider_factory,
            )
        else:
            followup_coro = kickoff_func(
                space_id=int(space_id),
                user_message_id=int(user_message_id),
                pending_message_id=int(pending_message_id),
            )
        asyncio.get_running_loop().create_task(followup_coro)
    except RuntimeError:
        pass

    return {
        "ok": True,
        "queued": True,
        "command_kind": kind,
        "user_message": user_payload,
        "assistant_message": pending_payload,
        "is_waiting": True,
    }


def prepare_publish(space_id: int, draft_id: int | None, due_date: dt.date) -> dict:
    return publishing.prepare_publish(int(space_id), draft_id, due_date)


def publish_sync(
    *, space_id: int, draft_id: int, due_date_iso: str, previous_status: str
) -> None:
    publishing.publish_sync(
        space_id=int(space_id),
        draft_id=int(draft_id),
        due_date_iso=str(due_date_iso),
        previous_status=str(previous_status or "open"),
        load_snapshot=publishing.load_publish_snapshot,
        audit=memory_service.try_audit_memory,
    )


async def kickoff_publish(
    *,
    space_id: int,
    draft_id: int,
    due_date_iso: str,
    previous_status: str,
    release_terminals: Callable[[int], Awaitable[int]],
) -> None:
    await asyncio.to_thread(
        publish_sync,
        space_id=int(space_id),
        draft_id=int(draft_id),
        due_date_iso=str(due_date_iso),
        previous_status=str(previous_status or "open"),
    )
    await complete_publish_release(
        space_id=int(space_id), release_terminals=release_terminals
    )


async def complete_publish_release(
    *,
    space_id: int,
    release_terminals: Callable[[int], Awaitable[int]],
) -> None:
    with session_scope() as s:
        space = s.get(InspirationSpace, int(space_id))
        should_release = space is not None and str(space.status or "") in {
            "published",
            "publishing_releasing",
        }
    if should_release:
        await release_terminals(int(space_id))
        with session_scope() as s:
            space = s.get(InspirationSpace, int(space_id))
            if space is not None and str(space.status or "") == "publishing_releasing":
                space.status = "published"
                space.last_activity_at = memory_service.utcnow()


def terminal_payload(space_id: int, t: RemoteTerminalSession) -> dict:
    return terminal_bridge.terminal_payload(
        int(space_id), t, embed_path=_inspiration_ttyd_embed_path
    )


def _inspiration_ttyd_embed_path(space_id: int, terminal_id: str) -> str:
    import urllib.parse

    tid = urllib.parse.quote(str(terminal_id or ""), safe="")
    return f"/api/inspirations/{int(space_id)}/terminals/{tid}/ttyd/"


def build_draft_summary_prompt(space: InspirationSpace) -> str:
    return terminal_bridge.draft_summary_prompt(
        space, base_url=str(os.environ.get("OPENFOCUS_BASE_URL") or "").strip()
    )


def terminal_conn(companion_id: int | None, *, select_online: SelectCompanion):
    comp_id = int(companion_id or 0)
    if not comp_id:
        raise InspirationTerminalError("Terminal has no Companion")
    _comp, conn = select_online(comp_id)
    return conn


async def release_terminals(
    space_id: int,
    *,
    select_online: SelectCompanion,
    clear_ttyd_auto_prompts: ReleaseTerminalMode,
) -> int:
    owner = terminal_service.owner_for_inspiration_space(int(space_id))
    with session_scope() as s:
        terms = terminal_service.list_terminals(s, owner)
        term_infos = [
            {
                "terminal_id": str(t.terminal_id or ""),
                "companion_id": int(t.companion_id or 0),
            }
            for t in terms
        ]
    for info in term_infos:
        tid = str(info.get("terminal_id") or "").strip()
        comp_id = int(info.get("companion_id") or 0)
        if not tid or not comp_id:
            continue
        try:
            conn = terminal_conn(comp_id, select_online=select_online)
            await conn.request_terminal_stop(terminal_id=tid, timeout_seconds=5.0)
        except Exception:
            pass
    with session_scope() as s:
        terminal_service.delete_owner_terminal_records(s, owner=owner)
    for info in term_infos:
        clear_ttyd_auto_prompts(str(info.get("terminal_id") or ""))
    return len(term_infos)


def select_online_companion(grpc_server, companion_id: int | None = None):
    return companion_service.select_online(grpc_server, companion_id)


def has_online_companion(grpc_server) -> bool:
    return companion_service.has_online(grpc_server)
