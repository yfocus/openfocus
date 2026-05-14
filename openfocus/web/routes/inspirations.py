# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from ...companion.grpc import CompanionGrpcError
from ...db import session_scope
from ...domains.agent_spaces import terminals as terminal_service
from ...domains.inspirations import publishing as inspiration_publishing
from ...domains.inspirations import resources as inspiration_resources
from ...domains.inspirations import service as inspiration_service
from ...domains.memory import service as memory_service
from ...models import (
    Goal,
    InspirationDraft,
    InspirationMessage,
    InspirationPublishRecord,
    InspirationResource,
    InspirationSpace,
    RemoteTerminalSession,
)


def _inspiration_ttyd_embed_path(space_id: int, terminal_id: str) -> str:
    tid = urllib.parse.quote(str(terminal_id or ""), safe="")
    return f"/api/inspirations/{int(space_id)}/terminals/{tid}/ttyd/"


def _ttyd_target_url(base_url: str, tail: str, query: str) -> str:
    base = str(base_url or "").rstrip("/") + "/"
    tail = str(tail or "")
    if tail:
        base = urllib.parse.urljoin(base, tail.lstrip("/"))
    if query:
        base = base + "?" + query
    return base


def _ttyd_bridge_script() -> str:
    return r"""
<script>
(function(){
  if(window.__openfocusTtydBridgeInstalled) return;
  window.__openfocusTtydBridgeInstalled = true;
  const state = { enabled: false, prefix: '', injectUrl: '' };
  function disableBeforeUnload(){
    try{ window.onbeforeunload = null; }catch(_){ }
  }
  try{
    const rawAdd = window.addEventListener.bind(window);
    window.addEventListener = function(type, listener, options){
      if(String(type || '').toLowerCase() === 'beforeunload') return;
      return rawAdd(type, listener, options);
    };
  }catch(_){ }
  disableBeforeUnload();
  try{ setInterval(disableBeforeUnload, 1000); }catch(_){ }

  window.addEventListener('message', function(ev){
    const d = ev && ev.data ? ev.data : {};
    if(!d || d.type !== 'openfocus:ttyd-agent-mode') return;
    state.enabled = !!d.enabled;
    state.prefix = String(d.prefix || '');
    state.injectUrl = String(d.injectUrl || '');
  }, true);
})();
</script>
"""


def _maybe_inject_ttyd_bridge(data: bytes, media_type: str) -> bytes:
    mt = str(media_type or "").lower()
    if "text/html" not in mt:
        return data
    try:
        html = bytes(data or b"").decode("utf-8")
    except Exception:
        return data
    if "__openfocusTtydBridgeInstalled" in html:
        return data
    script = _ttyd_bridge_script()
    lower = html.lower()
    i = lower.find("<head>")
    if i >= 0:
        j = i + len("<head>")
        html = html[:j] + script + html[j:]
    else:
        html = script + html
    return html.encode("utf-8")


def create_router(*, templates: Jinja2Templates, deps) -> APIRouter:
    router = APIRouter()

    def _space_or_404(s, space_id: int) -> InspirationSpace:
        try:
            return deps.inspiration_space_or_404(s, int(space_id))
        except inspiration_service.InspirationNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))

    async def _enqueue_turn(space_id: int, content: str) -> dict:
        try:
            return await deps.inspiration_enqueue_turn(int(space_id), content)
        except inspiration_service.InspirationNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        except inspiration_service.InspirationConflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        except inspiration_service.InspirationValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

    def _prepare_publish(
        space_id: int, draft_id: int | None, due_date: dt.date
    ) -> dict:
        try:
            return deps.inspiration_prepare_publish(int(space_id), draft_id, due_date)
        except inspiration_publishing.PublishConflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        except inspiration_publishing.PublishUnavailable as e:
            detail = str(e)
            status_code = 404 if detail == "Inspiration space not found" else 400
            raise HTTPException(status_code=status_code, detail=detail)

    async def _store_uploaded_resource_file(
        *, space_id: int, seq_id: int, file: UploadFile
    ):
        try:
            return await deps.inspiration_store_uploaded_resource_file(
                space_id=int(space_id), seq_id=int(seq_id), file=file
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    def _sync_draft_summary_file(s, space: InspirationSpace):
        try:
            return deps.inspiration_sync_draft_summary_file(s, space)
        except inspiration_resources.EmptyDraftSummary as e:
            raise HTTPException(status_code=400, detail=str(e))
        except inspiration_resources.DraftSummaryReadError as e:
            raise HTTPException(status_code=400, detail=str(e))

    def _terminal_conn(companion_id: int | None):
        try:
            return deps.inspiration_terminal_conn(companion_id)
        except inspiration_service.InspirationTerminalError as e:
            raise HTTPException(status_code=400, detail=str(e))

    def _load_inspiration_ttyd_terminal(
        space_id: int, terminal_id: str
    ) -> tuple[InspirationSpace, str]:
        tid = str(terminal_id or "").strip()
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")
        with session_scope() as s:
            space = _space_or_404(s, int(space_id))
            owner = terminal_service.owner_for_inspiration_space(int(space_id))
            try:
                t = terminal_service.get_terminal_for_owner(
                    s, owner=owner, terminal_id=tid
                )
            except terminal_service.TerminalNotFound:
                raise HTTPException(status_code=404, detail="Terminal not found")
            backend = str(getattr(t, "backend", "") or "ttyd").strip()
            connect_url = str(getattr(t, "connect_url", "") or "").strip()
        if backend != "ttyd" or not connect_url:
            raise HTTPException(status_code=404, detail="ttyd terminal not found")
        return space, connect_url.rstrip("/")

    @router.get("/api/inspirations")
    def inspirations_list_api(limit: int = 50) -> dict:
        limit = max(1, min(int(limit or 50), 200))
        with session_scope() as s:
            spaces = (
                s.query(InspirationSpace)
                .order_by(
                    InspirationSpace.last_activity_at.desc(), InspirationSpace.id.desc()
                )
                .limit(limit)
                .all()
            )
            space_ids = [int(sp.id) for sp in spaces]
            resource_counts = {sid: 0 for sid in space_ids}
            draft_counts = {sid: 0 for sid in space_ids}
            publish_counts = {sid: 0 for sid in space_ids}
            latest_drafts: dict[int, InspirationDraft] = {}
            if space_ids:
                for sid, count in (
                    s.query(InspirationResource.space_id, InspirationResource.id)
                    .filter(InspirationResource.space_id.in_(space_ids))
                    .filter(InspirationResource.deleted_at.is_(None))
                    .all()
                ):
                    resource_counts[int(sid)] = resource_counts.get(int(sid), 0) + 1
                for sid, count in (
                    s.query(InspirationDraft.space_id, InspirationDraft.id)
                    .filter(InspirationDraft.space_id.in_(space_ids))
                    .all()
                ):
                    draft_counts[int(sid)] = draft_counts.get(int(sid), 0) + 1
                for sid, count in (
                    s.query(
                        InspirationPublishRecord.space_id, InspirationPublishRecord.id
                    )
                    .filter(InspirationPublishRecord.space_id.in_(space_ids))
                    .all()
                ):
                    publish_counts[int(sid)] = publish_counts.get(int(sid), 0) + 1
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
                    if sid not in latest_drafts:
                        latest_drafts[sid] = draft
        return {
            "ok": True,
            "items": [
                deps.inspiration_space_payload(
                    space,
                    latest_draft=latest_drafts.get(int(space.id)),
                    resource_count=resource_counts.get(int(space.id), 0),
                    draft_count=draft_counts.get(int(space.id), 0),
                    publish_count=publish_counts.get(int(space.id), 0),
                )
                for space in spaces
            ],
        }

    @router.post("/api/inspirations")
    def inspirations_create_api(payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        title = str(payload.get("title") or "").strip()
        mode = (
            str(payload.get("mode") or payload.get("surface") or "built_in")
            .strip()
            .lower()
        )
        if mode in {"bring_your_own_agent", "byo", "remote_terminal"}:
            mode = "terminal"
        if mode not in {"built_in", "terminal"}:
            mode = "built_in"
        initial_message = str(
            payload.get("initial_message") or payload.get("message") or ""
        ).strip()
        if not title and not initial_message:
            raise HTTPException(
                status_code=400, detail="title or initial_message is required"
            )
        if not title:
            title = (
                deps.truncate_zh(initial_message.replace("\n", " "), 40)
                or "Inspiration"
            )
        title = title[:512]

        space_id = 0
        created_payload: dict | None = None
        with session_scope() as s:
            now = deps.utcnow()
            space = InspirationSpace(
                title=title,
                status="open",
                mode=mode,
                last_activity_at=now,
            )
            s.add(space)
            s.flush()
            space_id = int(space.id)
            workspace = deps.inspiration_workspace_path(space, space_id)
            space.workspace_path = str(workspace)
            deps.inspiration_create_initial_note_resource(
                s, space, title=title, first_note=initial_message
            )
            s.add(
                InspirationMessage(
                    space_id=space_id,
                    role="assistant",
                    kind="system",
                    content="This is your Inspiration space. Keep exploring here, generate drafts when ready, and publish only when the structure looks solid.",
                )
            )
            if initial_message:
                s.add(
                    InspirationMessage(
                        space_id=space_id,
                        role="user",
                        kind="message",
                        content=initial_message[:20000],
                    )
                )
                space.message_turn_count = 1
                space.last_activity_at = now
                provider, _err = deps.get_llm_provider_or_error()
                messages = (
                    s.query(InspirationMessage)
                    .filter(InspirationMessage.space_id == space_id)
                    .order_by(InspirationMessage.id.asc())
                    .all()
                )
                resources = deps.inspiration_non_deleted_resources(s, space_id)
                if provider is None:
                    reply = deps.inspiration_fallback_reply(space, initial_message)
                else:
                    try:
                        reply = deps.inspiration_llm_reply(
                            provider,
                            space=space,
                            messages=messages,
                            resources=resources,
                        )
                    except Exception:
                        reply = deps.inspiration_fallback_reply(space, initial_message)
                s.add(
                    InspirationMessage(
                        space_id=space_id,
                        role="assistant",
                        kind="message",
                        content=reply,
                    )
                )
            created_payload = deps.inspiration_space_payload(space, resource_count=1)
        deps.try_audit_memory(
            kind="inspiration.space_created",
            source="web",
            summary=f"Created inspiration space {space_id}.",
            detail=initial_message or title,
            metadata={"space_id": space_id, "title": title, "mode": mode},
        )
        if initial_message:
            deps.inspiration_maybe_emit_phase_summary(space_id)
        return {"ok": True, "item": created_payload}

    @router.get("/api/inspirations/{space_id:int}")
    def inspirations_get_api(
        space_id: int, before_id: int | None = None, page_size: int = 60
    ) -> dict:
        page_size = max(1, min(int(page_size or 60), 200))
        with session_scope() as s:
            space = _space_or_404(s, space_id)
            is_waiting = deps.inspiration_is_waiting(s, int(space_id))
            messages, next_before = deps.inspiration_messages_page(
                s,
                int(space_id),
                before_id=before_id,
                page_size=page_size,
            )
            resources = deps.inspiration_non_deleted_resources(s, int(space_id))
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
            item = deps.inspiration_space_payload(
                space,
                latest_draft=latest_draft,
                resource_count=len(resources),
                draft_count=len(drafts),
                publish_count=len(records),
            )
            return {
                "ok": True,
                "item": item,
                "is_waiting": is_waiting,
                "is_publishing": deps.inspiration_is_publishing(space),
                "messages": [deps.inspiration_message_payload(msg) for msg in messages],
                "next_before_id": next_before,
                "resources": [
                    deps.inspiration_resource_payload(
                        int(space_id), res, include_text=True
                    )
                    for res in resources
                ],
                "drafts": [deps.inspiration_draft_payload(draft) for draft in drafts],
                "publish_records": [
                    deps.inspiration_publish_record_payload(record)
                    for record in records
                ],
            }

    @router.post("/api/inspirations/{space_id:int}/messages")
    async def inspiration_message_create_api(space_id: int, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        content = str(payload.get("content") or "").strip()
        return await _enqueue_turn(int(space_id), content)

    @router.post("/api/inspirations/{space_id:int}/close")
    async def inspiration_close_api(space_id: int) -> dict:
        with session_scope() as s:
            space = _space_or_404(s, space_id)
            if str(space.status or "open") == "published":
                raise HTTPException(
                    status_code=400, detail="Published spaces cannot be closed"
                )
            if str(space.status or "open") != "open":
                raise HTTPException(
                    status_code=400, detail="Only open spaces can be closed"
                )
            now = deps.utcnow()
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
            payload = deps.inspiration_space_payload(space)
        await deps.inspiration_release_terminals(int(space_id))
        deps.try_audit_memory(
            kind="inspiration.closed",
            source="web",
            summary=f"Closed inspiration space {int(space_id)}.",
            detail="User closed the inspiration space.",
            metadata={"space_id": int(space_id)},
        )
        return {"ok": True, "item": payload}

    @router.post("/api/inspirations/{space_id:int}/reopen")
    def inspiration_reopen_api(space_id: int) -> dict:
        with session_scope() as s:
            space = _space_or_404(s, space_id)
            if str(space.status or "open") != "closed":
                raise HTTPException(
                    status_code=400, detail="Only closed spaces can be reopened"
                )
            now = deps.utcnow()
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
            payload = deps.inspiration_space_payload(space)
        deps.try_audit_memory(
            kind="inspiration.reopened",
            source="web",
            summary=f"Reopened inspiration space {int(space_id)}.",
            detail="User reopened the inspiration space.",
            metadata={"space_id": int(space_id)},
        )
        return {"ok": True, "item": payload}

    @router.delete("/api/inspirations/{space_id:int}")
    def inspiration_delete_api(space_id: int) -> dict:
        removed_files_dir = str(deps.inspiration_space_files_dir(int(space_id)))
        with session_scope() as s:
            space = _space_or_404(s, space_id)
            if str(space.status or "open") == "published":
                raise HTTPException(
                    status_code=400, detail="Published spaces cannot be deleted"
                )
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
        try:
            shutil.rmtree(removed_files_dir, ignore_errors=True)
        except Exception:
            pass
        deps.try_audit_memory(
            kind="inspiration.deleted",
            source="web",
            summary=f"Deleted inspiration space {int(space_id)}.",
            detail="User deleted the inspiration space before publication.",
            metadata={"space_id": int(space_id)},
        )
        return {"ok": True, "space_id": int(space_id)}

    @router.post("/api/inspirations/{space_id:int}/resources")
    async def inspiration_resource_create_api(
        space_id: int,
        resource_type: str = Form(..., alias="type"),
        name: str | None = Form(default=None),
        text_content: str | None = Form(default=None),
        url_content: str | None = Form(default=None),
        file: UploadFile | None = File(default=None),
    ) -> dict:
        normalized_type = str(resource_type or "").strip().lower()
        if normalized_type not in {"url", "image", "text", "summary"}:
            raise HTTPException(status_code=400, detail="unsupported resource type")

        with session_scope() as s:
            space = _space_or_404(s, space_id)
            if str(space.status or "open") != "open":
                raise HTTPException(
                    status_code=400, detail="Only open spaces accept new resources"
                )
            seq_id = deps.inspiration_next_resource_seq(s, int(space_id))
            resource_name = str(name or "").strip()
            now = deps.utcnow()
            resource = InspirationResource(
                space_id=int(space_id),
                resource_seq_id=int(seq_id),
                type=normalized_type,
                name=resource_name or f"resource-{int(seq_id)}",
                text_content="",
                url_content="",
                file_path="",
                is_system_generated=False,
            )
            if normalized_type == "url":
                url_text = str(url_content or "").strip()
                if not url_text:
                    raise HTTPException(
                        status_code=400, detail="url_content is required"
                    )
                resource.url_content = url_text[:4000]
                resource.source = "user"
                if not resource_name:
                    resource.name = url_text[:512]
            elif normalized_type in {"text", "summary"}:
                body = str(text_content or "").strip()
                if not body:
                    raise HTTPException(
                        status_code=400, detail="text_content is required"
                    )
                resource.text_content = body[:20000]
                resource.source = "user"
                if normalized_type == "summary":
                    resource.is_system_generated = True
                    resource.source = "built_in_agent"
                if not resource_name:
                    resource.name = f"{normalized_type}-{int(seq_id)}"
            else:
                if file is None:
                    raise HTTPException(
                        status_code=400, detail="file is required for image resources"
                    )
                (
                    target_path,
                    uploaded_name,
                ) = await _store_uploaded_resource_file(
                    space_id=int(space_id),
                    seq_id=int(seq_id),
                    file=file,
                )
                resource.file_path = str(target_path)
                try:
                    resource.external_path = str(
                        target_path.relative_to(
                            deps.inspiration_workspace_path(space, int(space_id))
                        )
                    )
                except Exception:
                    resource.external_path = str(target_path)
                resource.source = "user"
                resource.name = resource_name or uploaded_name
            s.add(resource)
            if normalized_type in {"url", "text", "summary"}:
                deps.inspiration_write_resource_file(resource, space)
            space.last_activity_at = now
            s.flush()
            payload = deps.inspiration_resource_payload(
                int(space_id), resource, include_text=True
            )
        deps.try_audit_memory(
            kind="inspiration.resource_added",
            source="web",
            summary=f"Added a {normalized_type} resource to inspiration space {int(space_id)}.",
            detail=str(payload.get("reference") or ""),
            metadata={
                "space_id": int(space_id),
                "resource_id": int(payload["id"]),
                "resource_type": normalized_type,
            },
        )
        return {"ok": True, "item": payload}

    @router.patch("/api/inspirations/{space_id:int}/resources/{resource_id:int}")
    def inspiration_resource_update_api(
        space_id: int, resource_id: int, payload: dict
    ) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        with session_scope() as s:
            space = _space_or_404(s, space_id)
            if str(space.status or "open") != "open":
                raise HTTPException(
                    status_code=400, detail="Only open spaces can edit resources"
                )
            resource = s.get(InspirationResource, int(resource_id))
            if resource is None or int(resource.space_id) != int(space_id):
                raise HTTPException(status_code=404, detail="Resource not found")
            if resource.deleted_at is not None:
                raise HTTPException(status_code=404, detail="Resource not found")
            if "name" in payload:
                name = str(payload.get("name") or "").strip()
                if name:
                    resource.name = name[:512]
            if str(resource.type or "") == "url" and "url_content" in payload:
                url_text = str(payload.get("url_content") or "").strip()
                if not url_text:
                    raise HTTPException(
                        status_code=400, detail="url_content is required"
                    )
                resource.url_content = url_text[:4000]
            if (
                str(resource.type or "") in {"text", "summary"}
                and "text_content" in payload
            ):
                body = str(payload.get("text_content") or "").strip()
                if not body:
                    raise HTTPException(
                        status_code=400, detail="text_content is required"
                    )
                resource.text_content = body[:20000]
            if str(resource.type or "") in {"url", "text", "summary"}:
                deps.inspiration_write_resource_file(resource, space)
            space.last_activity_at = deps.utcnow()
            payload_out = deps.inspiration_resource_payload(
                int(space_id), resource, include_text=True
            )
        return {"ok": True, "item": payload_out}

    @router.post("/api/inspirations/{space_id:int}/resources/{resource_id:int}/replace")
    async def inspiration_resource_replace_api(
        space_id: int,
        resource_id: int,
        name: str | None = Form(default=None),
        file: UploadFile | None = File(default=None),
    ) -> dict:
        if file is None:
            raise HTTPException(
                status_code=400, detail="file is required for image replacement"
            )
        old_path_raw = ""
        new_path_obj: Path | None = None
        with session_scope() as s:
            space = _space_or_404(s, space_id)
            if str(space.status or "open") != "open":
                raise HTTPException(
                    status_code=400,
                    detail="Only open spaces can replace resource files",
                )
            resource = s.get(InspirationResource, int(resource_id))
            if resource is None or int(resource.space_id) != int(space_id):
                raise HTTPException(status_code=404, detail="Resource not found")
            if resource.deleted_at is not None:
                raise HTTPException(status_code=404, detail="Resource not found")
            if str(resource.type or "") != "image":
                raise HTTPException(
                    status_code=400, detail="Only image resources support replace"
                )
            old_path_raw = str(resource.file_path or "").strip()
            (
                new_path_obj,
                uploaded_name,
            ) = await _store_uploaded_resource_file(
                space_id=int(space_id),
                seq_id=int(resource.resource_seq_id or 0),
                file=file,
            )
            resource.file_path = str(new_path_obj)
            try:
                resource.external_path = str(
                    new_path_obj.relative_to(
                        deps.inspiration_workspace_path(space, int(space_id))
                    )
                )
            except Exception:
                resource.external_path = str(new_path_obj)
            next_name = str(name or "").strip()
            if next_name:
                resource.name = next_name[:512]
            elif not str(resource.name or "").strip():
                resource.name = uploaded_name
            space.last_activity_at = deps.utcnow()
            payload_out = deps.inspiration_resource_payload(
                int(space_id), resource, include_text=True
            )
        if old_path_raw:
            try:
                old_path = Path(old_path_raw).expanduser()
                if (
                    new_path_obj is not None
                    and old_path != new_path_obj
                    and old_path.exists()
                    and old_path.is_file()
                ):
                    old_path.unlink()
            except Exception:
                pass
        return {"ok": True, "item": payload_out}

    @router.delete("/api/inspirations/{space_id:int}/resources/{resource_id:int}")
    def inspiration_resource_delete_api(space_id: int, resource_id: int) -> dict:
        with session_scope() as s:
            space = _space_or_404(s, space_id)
            if str(space.status or "open") != "open":
                raise HTTPException(
                    status_code=400, detail="Only open spaces can delete resources"
                )
            resource = s.get(InspirationResource, int(resource_id))
            if resource is None or int(resource.space_id) != int(space_id):
                raise HTTPException(status_code=404, detail="Resource not found")
            if resource.deleted_at is not None:
                return {"ok": True, "resource_id": int(resource_id)}
            resource.deleted_at = deps.utcnow()
            space.last_activity_at = deps.utcnow()
        return {"ok": True, "resource_id": int(resource_id)}

    @router.get("/api/inspirations/{space_id:int}/resources/{resource_id:int}/raw")
    def inspiration_resource_raw_api(space_id: int, resource_id: int) -> FileResponse:
        with session_scope() as s:
            _space_or_404(s, space_id)
            resource = s.get(InspirationResource, int(resource_id))
            if resource is None or int(resource.space_id) != int(space_id):
                raise HTTPException(status_code=404, detail="Resource not found")
            if (
                resource.deleted_at is not None
                or not str(resource.file_path or "").strip()
            ):
                raise HTTPException(status_code=404, detail="File resource not found")
            file_path = Path(str(resource.file_path or "")).expanduser()
            if not file_path.exists() or not file_path.is_file():
                raise HTTPException(status_code=404, detail="File resource not found")
            return FileResponse(
                path=str(file_path),
                media_type=deps.guess_media_type(file_path),
                filename=str(resource.name or file_path.name),
            )

    @router.post("/api/inspirations/{space_id:int}/resources/sync")
    def inspiration_resources_sync_api(space_id: int) -> dict:
        with session_scope() as s:
            space = _space_or_404(s, int(space_id))
            if str(space.status or "open") == "published":
                raise HTTPException(
                    status_code=400, detail="Published spaces are read-only"
                )
            items = deps.inspiration_sync_resources_dir(s, space)
            payloads = [
                deps.inspiration_resource_payload(
                    int(space_id), item, include_text=True
                )
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
        deps.try_audit_memory(
            kind="inspiration.resources_synced",
            source="web",
            summary=f"Synced resources directory for inspiration space {int(space_id)}.",
            detail=json.dumps(
                [item.get("external_path") for item in payloads], ensure_ascii=False
            )[:4000],
            metadata={"space_id": int(space_id), "resource_count": len(payloads)},
        )
        return {
            "ok": True,
            "synced": bool(payloads),
            "items": payloads,
            "item": draft_item,
        }

    @router.post("/api/inspirations/{space_id:int}/commands/summary_title")
    async def inspiration_summary_title_api(space_id: int) -> dict:
        return await _enqueue_turn(int(space_id), "/summary_title")

    @router.post("/api/inspirations/{space_id:int}/drafts/generate")
    async def inspiration_draft_generate_api(space_id: int) -> dict:
        return await _enqueue_turn(int(space_id), "/plan")

    @router.post("/api/inspirations/{space_id:int}/drafts/generate_from_draft_summary")
    async def inspiration_draft_generate_from_draft_summary_api(space_id: int) -> dict:
        with session_scope() as s:
            space = _space_or_404(s, int(space_id))
            item = _sync_draft_summary_file(s, space)
            if item is None:
                raise HTTPException(status_code=400, detail="Summary is missing")
        return await _enqueue_turn(int(space_id), "/plan")

    @router.post("/api/inspirations/{space_id:int}/drafts/generate_from_resource")
    async def inspiration_draft_generate_from_resource_api(
        space_id: int, payload: dict
    ) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        try:
            resource_id = int(payload.get("resource_id") or 0)
        except Exception:
            resource_id = 0
        if resource_id <= 0:
            raise HTTPException(status_code=400, detail="resource_id is required")
        with session_scope() as s:
            _space_or_404(s, int(space_id))
            resource = (
                s.query(InspirationResource)
                .filter(InspirationResource.space_id == int(space_id))
                .filter(InspirationResource.id == int(resource_id))
                .filter(InspirationResource.deleted_at.is_(None))
                .one_or_none()
            )
            if resource is None:
                raise HTTPException(status_code=404, detail="Resource not found")
            resource_ref = deps.inspiration_resource_reference(resource)
        prompt = (
            "/plan\n"
            "Create a Goal and Tasks using this resource as the primary source. "
            "If it follows the OpenFocus bridge Markdown format, map the level-1 heading to the goal title, "
            "the content under it to the goal content, and each level-2 heading plus its body to one task.\n\n"
            f"{resource_ref}"
        )
        return await _enqueue_turn(int(space_id), prompt)

    @router.get("/api/inspirations/{space_id:int}/drafts")
    def inspiration_drafts_list_api(space_id: int) -> dict:
        with session_scope() as s:
            _space_or_404(s, space_id)
            drafts = (
                s.query(InspirationDraft)
                .filter(InspirationDraft.space_id == int(space_id))
                .order_by(InspirationDraft.version.desc(), InspirationDraft.id.desc())
                .all()
            )
        return {
            "ok": True,
            "items": [deps.inspiration_draft_payload(draft) for draft in drafts],
        }

    @router.post("/api/inspirations/{space_id:int}/publish")
    async def inspiration_publish_api(space_id: int, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        due_date_raw = str(payload.get("due_date") or "").strip()
        if due_date_raw:
            due_date = dt.date.fromisoformat(due_date_raw)
        else:
            due_date = dt.date.today() + dt.timedelta(days=7)
        draft_id = payload.get("draft_id")
        publish_info = _prepare_publish(
            int(space_id),
            int(draft_id) if draft_id is not None else None,
            due_date,
        )
        asyncio.get_running_loop().create_task(
            deps.kickoff_inspiration_publish(
                space_id=int(space_id),
                draft_id=int(publish_info["draft_id"]),
                due_date_iso=str(publish_info["due_date"]),
                previous_status=str(publish_info["previous_status"]),
            )
        )
        return {
            "ok": True,
            "queued": True,
            "space_id": int(space_id),
            "draft_id": int(publish_info["draft_id"]),
            "status": "publishing",
        }

    @router.post("/api/inspirations/{space_id:int}/fork")
    def inspiration_fork_api(space_id: int, payload: dict) -> dict:
        if not isinstance(payload, dict):
            payload = {}
        title = str(payload.get("title") or "").strip()
        include_all_resources = bool(payload.get("include_all_resources"))
        selected_resource_ids_raw = payload.get("resource_ids") or []
        selected_resource_ids: set[int] = set()
        for item in selected_resource_ids_raw:
            try:
                selected_resource_ids.add(int(item))
            except Exception:
                continue

        with session_scope() as s:
            source_space = _space_or_404(s, space_id)
            target_title = (
                title[:512]
                if title
                else deps.inspiration_default_followup_title(source_space.title)
            )
            now = deps.utcnow()
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
            forked.workspace_path = str(
                deps.inspiration_workspace_path(forked, new_space_id)
            )

            resources = deps.inspiration_non_deleted_resources(s, int(space_id))
            seq_id = 1
            for resource in resources:
                if str(resource.type or "") == "summary":
                    deps.inspiration_clone_resource(
                        s=s,
                        source=resource,
                        target_space_id=new_space_id,
                        seq_id=seq_id,
                    )
                    seq_id += 1
                    continue
                if include_all_resources or int(resource.id) in selected_resource_ids:
                    deps.inspiration_clone_resource(
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
            payload_out = deps.inspiration_space_payload(forked)
        deps.try_audit_memory(
            kind="inspiration.forked",
            source="web",
            summary=f"Forked inspiration space {int(space_id)} into {int(payload_out['id'])}.",
            detail=payload_out["title"],
            metadata={
                "space_id": int(space_id),
                "forked_space_id": int(payload_out["id"]),
            },
        )
        return {"ok": True, "item": payload_out}

    def _inspiration_detail_page_context(space_id: int | None) -> dict:
        with session_scope() as s:
            spaces = (
                s.query(InspirationSpace)
                .order_by(
                    InspirationSpace.last_activity_at.desc(), InspirationSpace.id.desc()
                )
                .all()
            )
            space = _space_or_404(s, int(space_id)) if space_id is not None else None
            is_waiting = (
                deps.inspiration_is_waiting(s, int(space_id))
                if space is not None
                else False
            )
            is_publishing = deps.inspiration_is_publishing(space)
            terminals: list[RemoteTerminalSession] = []
            inspiration_terminal: dict | None = None
            messages: list[InspirationMessage] = []
            resources: list[InspirationResource] = []
            published_goal: Goal | None = None
            if space is not None:
                messages = (
                    s.query(InspirationMessage)
                    .filter(InspirationMessage.space_id == int(space_id))
                    .order_by(InspirationMessage.id.asc())
                    .all()
                )
                resources = deps.inspiration_non_deleted_resources(s, int(space_id))
                terminals = terminal_service.list_terminals(
                    s, terminal_service.owner_for_inspiration_space(int(space_id))
                )
                if terminals:
                    inspiration_terminal = deps.inspiration_terminal_payload(
                        int(space_id), terminals[0]
                    )
                published_goal = (
                    s.get(Goal, int(space.published_goal_id))
                    if getattr(space, "published_goal_id", None)
                    else None
                )
        return {
            "spaces": spaces,
            "space": space,
            "is_waiting": is_waiting,
            "is_publishing": is_publishing,
            "messages": messages,
            "resources": resources,
            "inspiration_terminal": inspiration_terminal,
            "inspiration_terminal_count": len(terminals),
            "has_online_companion": deps.has_online_companion(),
            "draft_summary_prompt": deps.build_inspiration_draft_summary_prompt(space)
            if space
            else "",
            "published_goal": published_goal,
            "default_due": (dt.date.today() + dt.timedelta(days=7)).isoformat(),
        }

    @router.get("/inspirations", response_class=HTMLResponse)
    def inspirations_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "inspiration_detail.html",
            _inspiration_detail_page_context(None),
        )

    @router.get("/inspirations/{space_id:int}", response_class=HTMLResponse)
    def inspiration_detail_page(request: Request, space_id: int) -> HTMLResponse:
        try:
            context = _inspiration_detail_page_context(int(space_id))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            context = _inspiration_detail_page_context(None)
            context["missing_space_id"] = int(space_id)
        return templates.TemplateResponse(
            request,
            "inspiration_detail.html",
            context,
        )

    @router.get("/api/inspirations/{space_id:int}/terminals")
    def inspiration_terminals_list(space_id: int) -> dict:
        with session_scope() as s:
            space = _space_or_404(s, int(space_id))
            deps.inspiration_workspace_path(space, int(space_id))
            owner = terminal_service.owner_for_inspiration_space(int(space_id))
            terms = terminal_service.list_terminals(s, owner)
        return {
            "ok": True,
            "companion": {"online": deps.has_online_companion()},
            "terminals": [
                deps.inspiration_terminal_payload(int(space_id), t) for t in terms
            ],
        }

    @router.post("/api/inspirations/{space_id:int}/terminals/new")
    async def inspiration_terminals_new(space_id: int, request: Request) -> dict:
        payload: dict = {}
        try:
            if (
                (request.headers.get("content-type") or "")
                .lower()
                .startswith("application/json")
            ):
                raw_payload = await request.json()
                payload = raw_payload if isinstance(raw_payload, dict) else {}
        except Exception:
            payload = {}
        with session_scope() as s:
            space = _space_or_404(s, int(space_id))
            if str(space.status or "open") != "open":
                raise HTTPException(
                    status_code=400, detail="Only open spaces can start terminals"
                )
            workspace = deps.inspiration_workspace_path(space, int(space_id))
            space.mode = "terminal"
            space.workspace_path = str(workspace)
            s.flush()
            workspace_path = str(workspace)
        companion_id = (
            payload.get("companion_id") if isinstance(payload, dict) else None
        )
        try:
            comp, conn = deps.select_online_companion(
                int(companion_id) if companion_id else None
            )
        except (TypeError, ValueError):
            comp, conn = deps.select_online_companion(None)

        terminal_id = str(uuid.uuid4())
        ttyd_base_path = _inspiration_ttyd_embed_path(int(space_id), terminal_id)
        try:
            res = await conn.request_terminal_start(
                terminal_id=terminal_id,
                root_path=workspace_path,
                base_path=ttyd_base_path,
                timeout_seconds=10.0,
            )
        except CompanionGrpcError as e:
            raise HTTPException(
                status_code=502, detail=f"Companion terminal failed to start: {e}"
            )
        except Exception as e:
            raise HTTPException(
                status_code=502, detail=f"Companion terminal failed to start: {e}"
            )
        real_tid = (res.terminal_id or "").strip() or terminal_id
        backend = str(getattr(res, "backend", "") or "ttyd").strip() or "ttyd"
        connect_url = str(getattr(res, "connect_url", "") or "").strip()
        if backend == "ttyd" and not connect_url:
            raise HTTPException(
                status_code=502,
                detail="Companion terminal failed to start: missing connect_url",
            )
        with session_scope() as s:
            owner = terminal_service.owner_for_inspiration_space(int(space_id))
            t = terminal_service.create_terminal_record(
                s,
                owner=owner,
                task_public_id="",
                companion_id=int(comp.id),
                root_path=workspace_path,
                terminal_id=real_tid,
                backend=backend,
                connect_url=connect_url,
            )
            name = str(t.name or "")
            terminal_payload = deps.inspiration_terminal_payload(int(space_id), t)
        deps.try_audit_memory(
            kind="inspiration.terminal_created",
            source="web",
            summary=f"Created inspiration terminal `{name}`.",
            detail=f"InspirationSpace {int(space_id)} created terminal {real_tid} at {workspace_path}.",
            metadata={"space_id": int(space_id), "terminal_id": real_tid, "name": name},
        )
        return {"ok": True, "terminal": terminal_payload}

    @router.post("/api/inspirations/{space_id:int}/terminals/{terminal_id}/inject")
    async def inspiration_terminals_inject(
        space_id: int, terminal_id: str, payload: dict
    ) -> dict:
        tid = str(terminal_id or "").strip()
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")
        with session_scope() as s:
            _space_or_404(s, int(space_id))
            owner = terminal_service.owner_for_inspiration_space(int(space_id))
            try:
                t = terminal_service.get_terminal_for_owner(
                    s, owner=owner, terminal_id=tid
                )
            except terminal_service.TerminalNotFound:
                raise HTTPException(status_code=404, detail="Terminal not found")
            comp_id = int(t.companion_id or 0)
        conn = _terminal_conn(comp_id)
        raw = b""
        data_b64 = str((payload or {}).get("data_b64") or "")
        if data_b64:
            with contextlib.suppress(Exception):
                raw = base64.b64decode(data_b64)
        if not raw:
            raw = str((payload or {}).get("text") or "").encode("utf-8")
        if not raw:
            raise HTTPException(status_code=400, detail="data is required")
        try:
            await conn.request_terminal_input(
                terminal_id=tid, data=raw, timeout_seconds=10.0
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"terminal inject failed: {e}")
        deps.try_audit_memory(
            kind="inspiration.terminal_input",
            source="web",
            summary=f"Injected input to inspiration terminal `{tid}`.",
            detail=memory_service.decode_terminal_bytes(raw),
            metadata={"space_id": int(space_id), "terminal_id": tid},
        )
        return {"ok": True}

    @router.post("/api/inspirations/{space_id:int}/terminals/{terminal_id}/rename")
    async def inspiration_terminals_rename(
        space_id: int, terminal_id: str, payload: dict
    ) -> dict:
        tid = str(terminal_id or "").strip()
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")
        raw_name = str((payload or {}).get("name") or "").strip()
        if not raw_name:
            raise HTTPException(status_code=400, detail="name is required")
        if len(raw_name) > 128:
            raise HTTPException(status_code=400, detail="name is too long (<=128)")
        with session_scope() as s:
            _space_or_404(s, int(space_id))
            owner = terminal_service.owner_for_inspiration_space(int(space_id))
            try:
                terminal_service.rename_terminal(
                    s, owner=owner, terminal_id=tid, name=raw_name
                )
            except terminal_service.TerminalNotFound:
                raise HTTPException(status_code=404, detail="Terminal not found")
            except terminal_service.TerminalNameConflict:
                raise HTTPException(status_code=400, detail="name already exists")
        return {"ok": True, "terminal": {"terminal_id": tid, "name": raw_name}}

    @router.post("/api/inspirations/{space_id:int}/terminals/{terminal_id}/agent_mode")
    async def inspiration_terminals_agent_mode(
        space_id: int, terminal_id: str, payload: dict
    ) -> dict:
        tid = str(terminal_id or "").strip()
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")
        with session_scope() as s:
            _space_or_404(s, int(space_id))
            owner = terminal_service.owner_for_inspiration_space(int(space_id))
            try:
                terminal_service.get_terminal_for_owner(s, owner=owner, terminal_id=tid)
            except terminal_service.TerminalNotFound:
                raise HTTPException(status_code=404, detail="Terminal not found")
        enabled = bool((payload or {}).get("enabled"))
        prefix = str((payload or {}).get("prefix") or "").strip()
        if enabled and prefix:
            deps.ttyd_agent_mode[tid] = {"enabled": True, "prefix": prefix}
        else:
            deps.ttyd_agent_mode.pop(tid, None)
        return {"ok": True, "enabled": enabled}

    @router.post("/api/inspirations/{space_id:int}/terminals/{terminal_id}/mouse_mode")
    async def inspiration_terminals_mouse_mode(
        space_id: int, terminal_id: str, payload: dict
    ) -> dict:
        tid = str(terminal_id or "").strip()
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")
        with session_scope() as s:
            _space_or_404(s, int(space_id))
            owner = terminal_service.owner_for_inspiration_space(int(space_id))
            try:
                t = terminal_service.get_terminal_for_owner(
                    s, owner=owner, terminal_id=tid
                )
            except terminal_service.TerminalNotFound:
                raise HTTPException(status_code=404, detail="Terminal not found")
            comp_id = int(t.companion_id or 0)
        conn = _terminal_conn(comp_id)
        enabled = bool((payload or {}).get("enabled"))
        try:
            res = await conn.request_terminal_mouse_mode(
                terminal_id=tid, enabled=enabled, timeout_seconds=10.0
            )
        except Exception as e:
            raise HTTPException(
                status_code=502, detail=f"terminal mouse mode failed: {e}"
            )
        return {"ok": True, "enabled": bool(getattr(res, "enabled", enabled))}

    @router.post(
        "/api/inspirations/{space_id:int}/terminals/{terminal_id}/prepare_draft_summary"
    )
    async def inspiration_terminal_prepare_draft_summary(
        space_id: int, terminal_id: str
    ) -> dict:
        with session_scope() as s:
            space = _space_or_404(s, int(space_id))
            prompt = deps.build_inspiration_draft_summary_prompt(space)
        return await inspiration_terminals_inject(
            int(space_id),
            str(terminal_id),
            {"data_b64": base64.b64encode(prompt.encode("utf-8")).decode("ascii")},
        )

    @router.post("/api/inspirations/{space_id:int}/terminals/{terminal_id}/close")
    async def inspiration_terminals_close(space_id: int, terminal_id: str) -> dict:
        tid = str(terminal_id or "").strip()
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")
        comp_id = 0
        with session_scope() as s:
            _space_or_404(s, int(space_id))
            owner = terminal_service.owner_for_inspiration_space(int(space_id))
            try:
                t = terminal_service.get_terminal_for_owner(
                    s, owner=owner, terminal_id=tid
                )
            except terminal_service.TerminalNotFound:
                raise HTTPException(status_code=404, detail="Terminal not found")
            comp_id = int(t.companion_id or 0)
        with contextlib.suppress(Exception):
            conn = _terminal_conn(comp_id)
            await conn.request_terminal_stop(terminal_id=tid, timeout_seconds=10.0)
        with session_scope() as s:
            terminal_service.delete_terminal_record(
                s,
                owner=terminal_service.owner_for_inspiration_space(int(space_id)),
                terminal_id=tid,
            )
        deps.ttyd_agent_mode.pop(tid, None)
        return {"ok": True}

    @router.api_route(
        "/api/inspirations/{space_id:int}/terminals/{terminal_id}/ttyd/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def inspiration_terminals_ttyd_proxy(
        request: Request, space_id: int, terminal_id: str, path: str = ""
    ) -> Response:
        _, connect_url = _load_inspiration_ttyd_terminal(space_id, terminal_id)
        proxy_prefix = _inspiration_ttyd_embed_path(space_id, terminal_id)
        target_tail = proxy_prefix.lstrip("/") + str(path or "")
        target = _ttyd_target_url(connect_url, target_tail, request.url.query)
        body = await request.body()
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower()
            not in {"host", "connection", "content-length", "accept-encoding"}
        }
        try:
            req = urllib.request.Request(
                target,
                data=body if body else None,
                headers=headers,
                method=request.method,
            )
            resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=30)
            data = await asyncio.to_thread(resp.read)
        except urllib.error.HTTPError as e:
            data = await asyncio.to_thread(e.read)
            media_type = e.headers.get("content-type") or "application/octet-stream"
            return Response(
                content=data, status_code=int(e.code), media_type=media_type
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"ttyd proxy failed: {e}")
        excluded = {
            "content-encoding",
            "transfer-encoding",
            "connection",
            "content-length",
        }
        out_headers = {
            k: v for k, v in resp.headers.items() if str(k).lower() not in excluded
        }
        media_type = resp.headers.get("content-type") or "application/octet-stream"
        data = _maybe_inject_ttyd_bridge(data, media_type)
        return Response(
            content=data,
            status_code=int(getattr(resp, "status", 200) or 200),
            headers=out_headers,
            media_type=media_type,
        )

    @router.websocket(
        "/api/inspirations/{space_id:int}/terminals/{terminal_id}/ttyd/{path:path}"
    )
    async def inspiration_terminals_ttyd_ws_proxy(
        websocket: WebSocket, space_id: int, terminal_id: str, path: str = ""
    ) -> None:
        subprotocols = [
            str(x).strip()
            for x in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if str(x).strip()
        ]
        await websocket.accept(subprotocol=subprotocols[0] if subprotocols else None)
        try:
            _, connect_url = _load_inspiration_ttyd_terminal(space_id, terminal_id)
            proxy_prefix = _inspiration_ttyd_embed_path(space_id, terminal_id)
            target_tail = proxy_prefix.lstrip("/") + str(path or "")
            target = _ttyd_target_url(connect_url, target_tail, websocket.url.query)
            if target.startswith("http://"):
                target = "ws://" + target[len("http://") :]
            elif target.startswith("https://"):
                target = "wss://" + target[len("https://") :]
            try:
                import websockets
            except Exception as e:
                await websocket.close(code=1011, reason=f"websockets unavailable: {e}")
                return
            async with websockets.connect(
                target, open_timeout=10, subprotocols=subprotocols or None
            ) as upstream:

                async def _client_to_upstream() -> None:
                    while True:
                        msg = await websocket.receive()
                        typ = msg.get("type")
                        if typ == "websocket.disconnect":
                            await upstream.close()
                            return
                        if msg.get("bytes") is not None:
                            await upstream.send(
                                deps.rewrite_ttyd_input_for_agent_mode(
                                    terminal_id, msg["bytes"]
                                )
                            )
                        elif msg.get("text") is not None:
                            await upstream.send(
                                deps.rewrite_ttyd_input_for_agent_mode(
                                    terminal_id, msg["text"]
                                )
                            )

                async def _upstream_to_client() -> None:
                    async for msg in upstream:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(str(msg))

                a = asyncio.create_task(_client_to_upstream())
                b = asyncio.create_task(_upstream_to_client())
                done, pending = await asyncio.wait(
                    {a, b}, return_when=asyncio.FIRST_COMPLETED
                )
                for tsk in pending:
                    tsk.cancel()
                for tsk in done:
                    with contextlib.suppress(Exception):
                        _ = tsk.exception()
        except WebSocketDisconnect:
            return
        except Exception:
            with contextlib.suppress(Exception):
                await websocket.close(code=1011)

    return router
