# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
from typing import Any

from fastapi import (
    APIRouter,
    Body,
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

from ...db import session_scope
from ...domains.agent_spaces import terminals as terminal_service
from ...domains.companion import service as companion_service
from ...domains.inspirations import service as inspiration_service
from ...domains.inspirations import workspace as inspiration_workspace
from ...domains.memory import service as memory_service
from ...domains.terminals import gateway as terminal_gateway
from ...models import InspirationMessage, InspirationSpace


def create_router(*, templates: Jinja2Templates, deps) -> APIRouter:
    router = APIRouter()
    terminal_ops = terminal_gateway.RemoteTerminalGateway()

    def _companion_http_error(
        exc: companion_service.CompanionUseCaseError,
    ) -> HTTPException:
        if isinstance(exc, companion_service.CompanionNotFoundError):
            return HTTPException(status_code=404, detail=exc.detail)
        if isinstance(
            exc,
            (
                companion_service.CompanionValidationError,
                companion_service.CompanionUnavailableOrUnpairedError,
                companion_service.CompanionOfflineError,
            ),
        ):
            return HTTPException(status_code=400, detail=exc.detail)
        if isinstance(exc, companion_service.CompanionRuntimeError):
            return HTTPException(status_code=502, detail=exc.detail)
        return HTTPException(status_code=500, detail=exc.detail)

    def _workspace_http_error(
        exc: inspiration_workspace.InspirationWorkspaceError,
    ) -> HTTPException:
        if isinstance(
            exc,
            (
                inspiration_workspace.InspirationWorkspaceNotFound,
                inspiration_workspace.InspirationWorkspaceResourceNotFound,
            ),
        ):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, inspiration_workspace.InspirationWorkspaceConflict):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, inspiration_workspace.InspirationWorkspaceValidationError):
            return HTTPException(status_code=400, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    def _audit_workspace_result(result) -> None:
        deps.try_audit_memory(
            kind=result.audit_kind,
            source="web",
            summary=result.audit_summary,
            detail=result.audit_detail,
            metadata=result.audit_metadata,
        )

    async def _enqueue_turn(space_id: int, content: str) -> dict:
        try:
            return await deps.inspiration_enqueue_turn(int(space_id), content)
        except inspiration_service.InspirationNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        except inspiration_service.InspirationConflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        except inspiration_service.InspirationValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

    async def _store_uploaded_resource_file(
        *, space_id: int, seq_id: int, file: UploadFile
    ):
        try:
            return await deps.inspiration_store_uploaded_resource_file(
                space_id=int(space_id), seq_id=int(seq_id), file=file
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    def _terminal_conn(companion_id: int | None):
        try:
            return deps.inspiration_terminal_conn(companion_id)
        except companion_service.CompanionUseCaseError as exc:
            raise _companion_http_error(exc) from exc
        except inspiration_service.InspirationTerminalError as e:
            raise HTTPException(status_code=400, detail=str(e))

    def _load_inspiration_ttyd_terminal(
        space_id: int, terminal_id: str
    ) -> tuple[inspiration_workspace.TerminalOperationContext, str]:
        try:
            terminal_context = inspiration_workspace.prepare_terminal_operation(
                int(space_id)
            )
            connect_url = terminal_ops.load_ttyd_connect_url(
                owner=terminal_context.owner,
                terminal_id=terminal_id,
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        except terminal_gateway.TerminalValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")
        except terminal_gateway.TerminalUnavailable:
            raise HTTPException(status_code=404, detail="ttyd terminal not found")
        return terminal_context, connect_url.rstrip("/")

    @router.get("/api/inspirations")
    def inspirations_list_api(limit: int = 50) -> dict:
        return inspiration_workspace.list_spaces(limit=limit)

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
        try:
            return inspiration_workspace.get_space_detail(
                int(space_id), before_id=before_id, page_size=page_size
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc

    @router.post("/api/inspirations/{space_id:int}/messages")
    async def inspiration_message_create_api(space_id: int, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        content = str(payload.get("content") or "").strip()
        return await _enqueue_turn(int(space_id), content)

    @router.post("/api/inspirations/{space_id:int}/close")
    async def inspiration_close_api(space_id: int) -> dict:
        try:
            result = inspiration_workspace.close_space(int(space_id))
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        if result.release_terminals:
            await deps.inspiration_release_terminals(int(result.space_id))
        _audit_workspace_result(result)
        return {"ok": True, "item": result.item}

    @router.post("/api/inspirations/{space_id:int}/reopen")
    def inspiration_reopen_api(space_id: int) -> dict:
        try:
            result = inspiration_workspace.reopen_space(int(space_id))
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        _audit_workspace_result(result)
        return {"ok": True, "item": result.item}

    @router.delete("/api/inspirations/{space_id:int}")
    def inspiration_delete_api(space_id: int) -> dict:
        try:
            result = inspiration_workspace.delete_space(int(space_id))
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        _audit_workspace_result(result)
        return {"ok": True, "space_id": int(result.space_id)}

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
        stored_file: inspiration_workspace.StoredResourceFile | None = None
        if normalized_type == "image":
            if file is None:
                raise HTTPException(
                    status_code=400, detail="file is required for image resources"
                )
            try:
                slot = inspiration_workspace.prepare_resource_upload(int(space_id))
            except inspiration_workspace.InspirationWorkspaceError as exc:
                raise _workspace_http_error(exc) from exc
            target_path, uploaded_name = await _store_uploaded_resource_file(
                space_id=int(space_id),
                seq_id=int(slot.seq_id),
                file=file,
            )
            stored_file = inspiration_workspace.StoredResourceFile(
                seq_id=int(slot.seq_id), path=target_path, uploaded_name=uploaded_name
            )

        try:
            result = inspiration_workspace.create_resource(
                int(space_id),
                resource_type=normalized_type,
                name=name,
                text_content=text_content,
                url_content=url_content,
                stored_file=stored_file,
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        _audit_workspace_result(result)
        return {"ok": True, "item": result.item}

    @router.patch("/api/inspirations/{space_id:int}/resources/{resource_id:int}")
    def inspiration_resource_update_api(
        space_id: int, resource_id: int, payload: dict
    ) -> dict:
        try:
            result = inspiration_workspace.update_resource(
                int(space_id), int(resource_id), payload
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        return {"ok": True, "item": result.item}

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
        try:
            slot = inspiration_workspace.prepare_resource_file_replacement(
                int(space_id), int(resource_id)
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc

        target_path, uploaded_name = await _store_uploaded_resource_file(
            space_id=int(space_id),
            seq_id=int(slot.seq_id),
            file=file,
        )
        stored_file = inspiration_workspace.StoredResourceFile(
            seq_id=int(slot.seq_id), path=target_path, uploaded_name=uploaded_name
        )
        try:
            result = inspiration_workspace.replace_resource_file(
                int(space_id),
                int(resource_id),
                stored_file=stored_file,
                name=name,
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        return {"ok": True, "item": result.item}

    @router.delete("/api/inspirations/{space_id:int}/resources/{resource_id:int}")
    def inspiration_resource_delete_api(space_id: int, resource_id: int) -> dict:
        try:
            result = inspiration_workspace.delete_resource(
                int(space_id), int(resource_id)
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        return {"ok": True, "resource_id": int(result.resource_id)}

    @router.get("/api/inspirations/{space_id:int}/resources/{resource_id:int}/raw")
    def inspiration_resource_raw_api(space_id: int, resource_id: int) -> FileResponse:
        try:
            result = inspiration_workspace.raw_resource_file(
                int(space_id), int(resource_id)
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        return FileResponse(
            path=str(result.path),
            media_type=result.media_type,
            filename=result.filename,
        )

    @router.post("/api/inspirations/{space_id:int}/resources/sync")
    def inspiration_resources_sync_api(space_id: int) -> dict:
        try:
            result = inspiration_workspace.sync_resources(int(space_id))
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        _audit_workspace_result(result)
        return {
            "ok": True,
            "synced": result.synced,
            "items": result.items,
            "item": result.item,
        }

    @router.post("/api/inspirations/{space_id:int}/commands/summary_title")
    async def inspiration_summary_title_api(space_id: int) -> dict:
        return await _enqueue_turn(int(space_id), "/summary_title")

    @router.post("/api/inspirations/{space_id:int}/drafts/generate")
    async def inspiration_draft_generate_api(space_id: int) -> dict:
        return await _enqueue_turn(int(space_id), "/plan")

    @router.post("/api/inspirations/{space_id:int}/drafts/generate_from_draft_summary")
    async def inspiration_draft_generate_from_draft_summary_api(space_id: int) -> dict:
        try:
            result = inspiration_workspace.prepare_draft_from_draft_summary(
                int(space_id)
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        return await _enqueue_turn(int(result.space_id), result.prompt)

    @router.post("/api/inspirations/{space_id:int}/drafts/generate_from_resource")
    async def inspiration_draft_generate_from_resource_api(
        space_id: int, payload: Any = Body(...)
    ) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        try:
            resource_id = int(payload.get("resource_id") or 0)
        except Exception:
            resource_id = 0
        try:
            result = inspiration_workspace.prepare_draft_from_resource(
                int(space_id), resource_id
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        return await _enqueue_turn(int(result.space_id), result.prompt)

    @router.get("/api/inspirations/{space_id:int}/drafts")
    def inspiration_drafts_list_api(space_id: int) -> dict:
        try:
            return inspiration_workspace.list_drafts(int(space_id))
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc

    @router.post("/api/inspirations/{space_id:int}/publish")
    async def inspiration_publish_api(space_id: int, payload: Any = Body(...)) -> dict:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        due_date_raw = str(payload.get("due_date") or "").strip()
        try:
            due_date = (
                dt.date.fromisoformat(due_date_raw)
                if due_date_raw
                else dt.date.today() + dt.timedelta(days=7)
            )
            draft_id_raw = payload.get("draft_id")
            draft_id = int(draft_id_raw) if draft_id_raw is not None else None
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid payload")

        try:
            publish_info = inspiration_workspace.prepare_publish(
                int(space_id),
                draft_id,
                due_date,
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        asyncio.get_running_loop().create_task(
            deps.kickoff_inspiration_publish(
                space_id=int(space_id),
                draft_id=int(publish_info.draft_id),
                due_date_iso=str(publish_info.due_date),
                previous_status=str(publish_info.previous_status),
            )
        )
        return {
            "ok": True,
            "queued": True,
            "space_id": int(space_id),
            "draft_id": int(publish_info.draft_id),
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

        try:
            result = inspiration_workspace.fork_space(
                int(space_id),
                title=title,
                include_all_resources=include_all_resources,
                resource_ids=selected_resource_ids,
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        _audit_workspace_result(result)
        return {"ok": True, "item": result.item}

    def _inspiration_detail_page_context(space_id: int | None) -> dict:
        return inspiration_workspace.detail_page_context(
            int(space_id) if space_id is not None else None,
            has_online_companion=deps.has_online_companion(),
            default_due=dt.date.today() + dt.timedelta(days=7),
        )

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
        except inspiration_workspace.InspirationWorkspaceNotFound:
            context = _inspiration_detail_page_context(None)
            context["missing_space_id"] = int(space_id)
        return templates.TemplateResponse(
            request,
            "inspiration_detail.html",
            context,
        )

    @router.get("/api/inspirations/{space_id:int}/terminals")
    async def inspiration_terminals_list(space_id: int) -> dict:
        try:
            terminal_context = inspiration_workspace.prepare_terminal_operation(
                int(space_id)
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        terms = await terminal_ops.list_live_terminals(
            owner=terminal_context.owner,
            runtime_resolver=_terminal_conn,
        )
        return {
            "ok": True,
            "companion": {"online": deps.has_online_companion()},
            "terminals": [
                terminal_gateway.terminal_payload(
                    int(space_id), t, route_prefix="/api/inspirations"
                )
                for t in terms
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
        try:
            start_context = inspiration_workspace.prepare_terminal_start(int(space_id))
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        workspace_path = str(start_context.root_path)
        companion_id = (
            payload.get("companion_id") if isinstance(payload, dict) else None
        )
        try:
            try:
                comp, conn = deps.select_online_companion(
                    int(companion_id) if companion_id else None
                )
            except (TypeError, ValueError):
                comp, conn = deps.select_online_companion(None)
        except companion_service.CompanionUseCaseError as exc:
            raise _companion_http_error(exc) from exc

        try:
            result = await terminal_ops.start_terminal(
                owner=start_context.owner,
                runtime=conn,
                companion_id=int(comp.id),
                root_path=workspace_path,
                base_path=start_context.base_path,
                timeout_seconds=10.0,
            )
        except terminal_gateway.TerminalStartError as e:
            raise HTTPException(status_code=502, detail=str(e))
        with session_scope() as s:
            t = terminal_service.get_terminal_for_owner(
                s,
                owner=start_context.owner,
                terminal_id=result.terminal_id,
            )
            terminal_payload = terminal_gateway.terminal_payload(
                int(space_id), t, route_prefix="/api/inspirations"
            )
        audit_metadata = dict(start_context.audit_metadata)
        audit_metadata.update({"terminal_id": result.terminal_id, "name": result.name})
        deps.try_audit_memory(
            kind=start_context.audit_kind,
            source="web",
            summary=start_context.audit_summary.format(name=result.name),
            detail=start_context.audit_detail_template.format(
                space_id=start_context.space_id,
                terminal_id=result.terminal_id,
                root_path=workspace_path,
            ),
            metadata=audit_metadata,
        )
        return {"ok": True, "terminal": terminal_payload}

    @router.post("/api/inspirations/{space_id:int}/terminals/{terminal_id}/inject")
    async def inspiration_terminals_inject(
        space_id: int, terminal_id: str, payload: dict
    ) -> dict:
        try:
            terminal_context = inspiration_workspace.prepare_terminal_operation(
                int(space_id)
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        owner = terminal_context.owner
        tid = str(terminal_id or "").strip()
        try:
            raw = await terminal_ops.inject_input(
                owner=owner,
                terminal_id=tid,
                payload=payload,
                runtime_resolver=_terminal_conn,
                timeout_seconds=10.0,
            )
        except terminal_gateway.TerminalValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")
        except terminal_gateway.TerminalUnavailable as e:
            raise HTTPException(status_code=410, detail=str(e))
        except terminal_gateway.TerminalInputError as e:
            raise HTTPException(status_code=502, detail=str(e))
        except companion_service.CompanionUseCaseError as exc:
            raise _companion_http_error(exc) from exc
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
        try:
            terminal_context = inspiration_workspace.prepare_terminal_operation(
                int(space_id)
            )
            raw_name = terminal_ops.rename_terminal(
                owner=terminal_context.owner,
                terminal_id=terminal_id,
                name=str((payload or {}).get("name") or ""),
            )
            tid = str(terminal_id or "").strip()
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        except terminal_gateway.TerminalValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")
        except terminal_service.TerminalNameConflict:
            raise HTTPException(status_code=400, detail="name already exists")
        return {"ok": True, "terminal": {"terminal_id": tid, "name": raw_name}}

    @router.post("/api/inspirations/{space_id:int}/terminals/{terminal_id}/mouse_mode")
    async def inspiration_terminals_mouse_mode(
        space_id: int, terminal_id: str, payload: dict
    ) -> dict:
        try:
            terminal_context = inspiration_workspace.prepare_terminal_operation(
                int(space_id)
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        owner = terminal_context.owner
        enabled = bool((payload or {}).get("enabled"))
        try:
            actual = await terminal_ops.set_mouse_mode(
                owner=owner,
                terminal_id=terminal_id,
                enabled=enabled,
                runtime_resolver=_terminal_conn,
                timeout_seconds=10.0,
            )
        except terminal_gateway.TerminalValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")
        except terminal_gateway.TerminalUnavailable as e:
            raise HTTPException(status_code=410, detail=str(e))
        except terminal_gateway.TerminalMouseModeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        except companion_service.CompanionUseCaseError as exc:
            raise _companion_http_error(exc) from exc
        return {"ok": True, "enabled": actual}

    @router.post(
        "/api/inspirations/{space_id:int}/terminals/{terminal_id}/prepare_draft_summary"
    )
    async def inspiration_terminal_prepare_draft_summary(
        space_id: int, terminal_id: str
    ) -> dict:
        try:
            prompt_context = inspiration_workspace.prepare_terminal_prompt(
                int(space_id)
            )
            prompt = prompt_context.prompt
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        return await inspiration_terminals_inject(
            int(space_id),
            str(terminal_id),
            {"data_b64": base64.b64encode(prompt.encode("utf-8")).decode("ascii")},
        )

    @router.post("/api/inspirations/{space_id:int}/terminals/{terminal_id}/close")
    async def inspiration_terminals_close(space_id: int, terminal_id: str) -> dict:
        try:
            terminal_context = inspiration_workspace.prepare_terminal_operation(
                int(space_id)
            )
        except inspiration_workspace.InspirationWorkspaceError as exc:
            raise _workspace_http_error(exc) from exc
        owner = terminal_context.owner
        try:
            await terminal_ops.close_terminal(
                owner=owner,
                terminal_id=terminal_id,
                runtime_resolver=_terminal_conn,
                clear_auto_prompt=lambda tid: deps.ttyd_auto_prompts.pop(tid, None),
                timeout_seconds=10.0,
            )
        except terminal_gateway.TerminalValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")
        return {"ok": True}

    @router.api_route(
        "/api/inspirations/{space_id:int}/terminals/{terminal_id}/ttyd/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def inspiration_terminals_ttyd_proxy(
        request: Request, space_id: int, terminal_id: str, path: str = ""
    ) -> Response:
        _, connect_url = _load_inspiration_ttyd_terminal(space_id, terminal_id)
        target = terminal_gateway.ttyd_proxy_target(
            connect_url=connect_url,
            route_prefix="/api/inspirations",
            owner_id=int(space_id),
            terminal_id=terminal_id,
            path=path,
            query=request.url.query,
        )
        try:
            proxied = await terminal_gateway.proxy_ttyd_http_request(
                target_url=target.target_url,
                method=request.method,
                headers=dict(request.headers.items()),
                body=await request.body(),
                timeout_seconds=30.0,
            )
        except terminal_gateway.TtydProxyError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return Response(
            content=proxied.body,
            status_code=proxied.status_code,
            headers=proxied.headers,
            media_type=proxied.media_type,
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
            target_info = terminal_gateway.ttyd_proxy_target(
                connect_url=connect_url,
                route_prefix="/api/inspirations",
                owner_id=int(space_id),
                terminal_id=terminal_id,
                path=path,
                query=websocket.url.query,
            )
            target = terminal_gateway.ttyd_websocket_target_url(target_info.target_url)
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
                            await upstream.send(msg["bytes"])
                        elif msg.get("text") is not None:
                            await upstream.send(msg["text"])

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
