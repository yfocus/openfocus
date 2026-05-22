# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from ...companion.grpc import CompanionGrpcError, CompanionGrpcServer
from ...db import session_scope
from ...domains.agent_activity import service as agent_activity_service
from ...domains.agent_spaces import terminals as terminal_service
from ...domains.companion import service as companion_service
from ...domains.memory import service as memory_service
from ...domains.terminals import gateway as terminal_gateway
from ...models import (
    AgentMessage,
    AgentSession,
    AgentSpace,
    AgentSpacePrompt,
    Companion,
    Goal,
    Task,
)
from ...schemas import AgentSpaceCreateIn, AgentSpacePromptIn


def _ttyd_embed_path(space_id: int, terminal_id: str) -> str:
    return terminal_gateway.ttyd_embed_path(
        "/api/agent_spaces", int(space_id), terminal_id
    )


def _openfocus_base_url(request: Request) -> str:
    try:
        return str(request.base_url).rstrip("/")
    except Exception:
        return "http://127.0.0.1:8001"


def _build_openfocus_ttyd_agent_prefix(*, base_url: str, task_public_id: str) -> str:
    base = str(base_url or "").rstrip("/") or "http://127.0.0.1:8001"
    task_id = str(task_public_id or "").strip()
    parts = []
    if task_id:
        parts.append(f"taskId={task_id}")
    parts.append(f"openfocusBaseUrl={base}")
    parts.append(
        "按 OpenFocus Event Spec 同步重要进展：只在有意义进展时调用 "
        f'POST {base}/api/agent/events，JSON={{"kind":"task.progress","agent":"<agent>","task_id":taskId,"payload":{{"status":"running","message":"...","progress":0.5,"step":1,"total_steps":4}}}}；'
        "不要为了 agent 启动、任务启动、任务结束、成功或失败而上报；"
        "适合上报的时机包括多步骤任务中某个步骤启动/完成、有重要中间结果、长期任务每约 5 分钟同步一次进展；"
        "payload 常用字段=status|message|summary|progress|step|total_steps|metadata；"
        "不要按 token/日志行/无意义心跳刷屏。"
    )
    return " · ".join(parts)


def _inject_openfocus_prompt(
    *, base_url: str, task_public_id: str, session_id: str, user_prompt: str
) -> str:
    head = (
        "你在 OpenFocus 的 AgentSpace 中工作。\n"
        f"agentSessionId={session_id}\n"
        f"taskId={task_public_id}\n"
        f"openfocusBaseUrl={base_url}\n"
        "你必须按 OpenFocus 的 Event Spec 上报，不要自定义不兼容格式。\n"
        "接口 1：POST /api/agent/events（完整地址：{openfocusBaseUrl}/api/agent/events）\n"
        "请求体必须是 JSON；只在重要进展时使用 task.progress：\n"
        '{"kind":"task.progress","agent":"<agent>","task_id":taskId,"payload":{"status":"running","message":"...","progress":0.5,"step":1,"total_steps":4}}\n'
        "字段规范：kind=事件类型（必填，<=128 chars）；agent=上报方标识（必填）；task_id=任务相关事件必填，且必须等于 taskId；payload=对象（必填）。\n"
        "payload 合法常用字段：status、message、summary、progress、step、total_steps、metadata。\n"
        "status 合法值按 spec 使用：running | blocked | waiting | canceled | in_progress | progress | waiting_on_someone。\n"
        "上报时机：多步骤任务中某个步骤启动或完成、有重要中间结果、长期任务每约 5 分钟同步一次进展。\n"
        "不要为了 agent 启动、任务启动、任务结束、成功或失败而上报；不要使用启动、结束、成功、失败这类 kind。\n"
        "不要按 token/日志行/无意义心跳刷屏。\n"
        "---\n"
    )
    return head + str(user_prompt or "")


def _load_space_and_optional_companion(space_id: int):
    return companion_service.load_space_and_optional_companion(space_id)


def _agent_space_prompt_payload(prompt: AgentSpacePrompt) -> dict:
    return {
        "id": int(prompt.id),
        "title": str(prompt.title or ""),
        "content": str(prompt.content or ""),
        "enabled": bool(prompt.enabled),
        "auto_enabled": bool(getattr(prompt, "auto_enabled", False)),
    }


def _require_companion_online(*, grpc_server: CompanionGrpcServer, comp):
    return companion_service.require_online(grpc_server, companion=comp)


def _companion_display_status(grpc_server: CompanionGrpcServer, c: Companion | None):
    if c is None:
        return None
    return str(companion_service.display_status(c, grpc_server) or "")


async def delete_agent_space_for_task(
    grpc_server: CompanionGrpcServer, task_public_id: str
) -> dict:
    terminal_ops = terminal_gateway.RemoteTerminalGateway()
    with session_scope() as s:
        space = (
            s.query(AgentSpace)
            .filter(AgentSpace.task_public_id == task_public_id)
            .one_or_none()
        )
        if space is None:
            return {"ok": True}

        comp = None
        if getattr(space, "companion_id", None):
            comp = s.get(Companion, int(space.companion_id))

        sessions = s.query(AgentSession).filter(AgentSession.space_id == space.id).all()
        sess_ids = [ss.session_id for ss in sessions]
        owner = terminal_service.owner_for_agent_space(int(space.id))

    cid = int(getattr(comp, "id", 0) or 0) if comp is not None else 0
    conn = grpc_server.registry.get(cid) if cid else None
    await terminal_ops.release_owner_terminals(
        owner=owner,
        conn=conn,
        timeout_seconds=5.0,
        delete_local_records=False,
    )

    with session_scope() as s:
        space = (
            s.query(AgentSpace)
            .filter(AgentSpace.task_public_id == task_public_id)
            .one_or_none()
        )
        if space is None:
            return {"ok": True}

        if sess_ids:
            s.query(AgentMessage).filter(AgentMessage.session_id.in_(sess_ids)).delete(
                synchronize_session=False
            )
            s.query(AgentSession).filter(AgentSession.session_id.in_(sess_ids)).delete(
                synchronize_session=False
            )

        terminal_service.delete_owner_terminal_records(s, owner=owner)
        s.delete(space)

    return {"ok": True}


def _ttyd_bridge_script() -> str:
    return terminal_gateway.ttyd_bridge_script()


def _maybe_inject_ttyd_bridge(data: bytes, media_type: str) -> bytes:
    return terminal_gateway.maybe_inject_ttyd_bridge(data, media_type)


def create_router(
    *,
    grpc_server: CompanionGrpcServer,
    templates: Jinja2Templates,
    ttyd_auto_prompts: dict[str, dict[str, object]],
    agent_sse_subscribe,
    agent_sse_unsubscribe,
    agent_sse_publish,
    rewrite_ttyd_input_for_auto_prompts,
) -> APIRouter:
    router = APIRouter()

    @router.get("/agent_space_prompts", response_class=HTMLResponse)
    def agent_space_prompts_view(request: Request) -> HTMLResponse:
        with session_scope() as s:
            prompts = (
                s.query(AgentSpacePrompt)
                .order_by(AgentSpacePrompt.enabled.desc(), AgentSpacePrompt.id.desc())
                .all()
            )
            items = [_agent_space_prompt_payload(p) for p in prompts]
        return templates.TemplateResponse(
            request,
            "agent_space_prompts.html",
            {"prompts": items},
        )

    @router.get("/api/agent_space_prompts")
    def list_agent_space_prompts(enabled_only: bool = True) -> dict:
        with session_scope() as s:
            q = s.query(AgentSpacePrompt)
            if enabled_only:
                q = q.filter(AgentSpacePrompt.enabled == True)  # noqa: E712
            prompts = q.order_by(AgentSpacePrompt.id.desc()).all()
            items = [_agent_space_prompt_payload(p) for p in prompts]
        return {"ok": True, "items": items}

    @router.post("/api/agent_space_prompts")
    def create_agent_space_prompt(payload: AgentSpacePromptIn) -> dict:
        title = str(payload.title or "").strip()
        content = str(payload.content or "").strip()
        if not title or not content:
            raise HTTPException(
                status_code=400, detail="title and content are required"
            )
        with session_scope() as s:
            prompt = AgentSpacePrompt(
                title=title,
                content=content,
                enabled=bool(payload.enabled),
                auto_enabled=bool(payload.auto_enabled),
            )
            s.add(prompt)
            s.flush()
            item = _agent_space_prompt_payload(prompt)
        return {"ok": True, "item": item}

    @router.put("/api/agent_space_prompts/{prompt_id}")
    def update_agent_space_prompt(prompt_id: int, payload: AgentSpacePromptIn) -> dict:
        title = str(payload.title or "").strip()
        content = str(payload.content or "").strip()
        if not title or not content:
            raise HTTPException(
                status_code=400, detail="title and content are required"
            )
        with session_scope() as s:
            prompt = s.get(AgentSpacePrompt, int(prompt_id))
            if prompt is None:
                raise HTTPException(
                    status_code=404, detail="AgentSpace prompt not found"
                )
            prompt.title = title
            prompt.content = content
            prompt.enabled = bool(payload.enabled)
            prompt.auto_enabled = bool(payload.auto_enabled)
            s.add(prompt)
            s.flush()
            item = _agent_space_prompt_payload(prompt)
        return {"ok": True, "item": item}

    @router.patch("/api/agent_space_prompts/{prompt_id}/enabled")
    def update_agent_space_prompt_enabled(prompt_id: int, payload: dict) -> dict:
        enabled = bool(payload.get("enabled")) if isinstance(payload, dict) else False
        with session_scope() as s:
            prompt = s.get(AgentSpacePrompt, int(prompt_id))
            if prompt is None:
                raise HTTPException(
                    status_code=404, detail="AgentSpace prompt not found"
                )
            prompt.enabled = enabled
            s.add(prompt)
            s.flush()
            item = _agent_space_prompt_payload(prompt)
        return {"ok": True, "item": item}

    @router.patch("/api/agent_space_prompts/{prompt_id}/auto_enabled")
    def update_agent_space_prompt_auto_enabled(prompt_id: int, payload: dict) -> dict:
        auto_enabled = (
            bool(payload.get("auto_enabled")) if isinstance(payload, dict) else False
        )
        with session_scope() as s:
            prompt = s.get(AgentSpacePrompt, int(prompt_id))
            if prompt is None:
                raise HTTPException(
                    status_code=404, detail="AgentSpace prompt not found"
                )
            prompt.auto_enabled = auto_enabled
            s.add(prompt)
            s.flush()
            item = _agent_space_prompt_payload(prompt)
        return {"ok": True, "item": item}

    @router.delete("/api/agent_space_prompts/{prompt_id}")
    def delete_agent_space_prompt(prompt_id: int) -> dict:
        with session_scope() as s:
            prompt = s.get(AgentSpacePrompt, int(prompt_id))
            if prompt is None:
                return {"ok": True}
            s.delete(prompt)
        return {"ok": True}

    def _require_companion_online(*, sp: AgentSpace, comp: Companion | None):
        return companion_service.require_online(grpc_server, companion=comp)

    def _companion_display_status(c: Companion | None):
        if c is None:
            return None
        return str(companion_service.display_status(c, grpc_server) or "")

    _agent_sse_subscribe = agent_sse_subscribe
    _agent_sse_unsubscribe = agent_sse_unsubscribe
    _agent_sse_publish = agent_sse_publish
    _rewrite_ttyd_input_for_auto_prompts = rewrite_ttyd_input_for_auto_prompts
    _try_audit_memory = memory_service.try_audit_memory
    _memory_decode_terminal_bytes = memory_service.decode_terminal_bytes
    terminal_ops = terminal_gateway.RemoteTerminalGateway()

    @router.get("/tasks/{task_public_id}/agent_space", response_class=HTMLResponse)
    def agent_space_view(request: Request, task_public_id: str) -> HTMLResponse:
        with session_scope() as s:
            task = s.query(Task).filter(Task.public_id == task_public_id).one_or_none()
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            goal = s.query(Goal).filter(Goal.id == task.goal_id).one_or_none()
            space = (
                s.query(AgentSpace)
                .filter(AgentSpace.task_public_id == task_public_id)
                .one_or_none()
            )
            companion = None
            if space is not None and getattr(space, "companion_id", None):
                companion = s.get(Companion, int(space.companion_id))

        return templates.TemplateResponse(
            request,
            "agent_space.html",
            {
                "task": task,
                "goal": goal,
                "space": space,
                "companion": companion,
                "auto_start_agent_command": str(
                    request.query_params.get("autostart") or ""
                ).strip()
                == "1",
                "agent_prefix": _build_openfocus_ttyd_agent_prefix(
                    base_url=_openfocus_base_url(request),
                    task_public_id=str(task.public_id or ""),
                ),
            },
        )

    @router.get("/api/tasks/{task_public_id}/agent_space")
    def get_agent_space(task_public_id: str) -> dict:
        with session_scope() as s:
            space = (
                s.query(AgentSpace)
                .filter(AgentSpace.task_public_id == task_public_id)
                .one_or_none()
            )
            if space is None:
                return {"ok": True, "space": None}
            return {
                "ok": True,
                "space": {
                    "id": space.id,
                    "task_public_id": space.task_public_id,
                    "companion_id": getattr(space, "companion_id", None),
                    "root_path": space.root_path,
                    "start_agent_command": str(
                        getattr(space, "start_agent_command", "") or ""
                    ),
                },
            }

    @router.post("/api/tasks/{task_public_id}/agent_space")
    def create_agent_space(task_public_id: str, payload: AgentSpaceCreateIn) -> dict:
        root_path = str((payload.root_path or "").strip())
        if not root_path:
            raise HTTPException(status_code=400, detail="root_path is required")
        start_agent_command = str((payload.start_agent_command or "").strip())

        with session_scope() as s:
            task = s.query(Task).filter(Task.public_id == task_public_id).one_or_none()
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")

            comp = s.get(Companion, int(payload.companion_id))
            if comp is None:
                raise HTTPException(status_code=400, detail="Companion not found")
            if comp.status != "active" or not (comp.auth_token or "").strip():
                raise HTTPException(
                    status_code=400, detail="Companion is not paired or unavailable"
                )

            existing = (
                s.query(AgentSpace)
                .filter(AgentSpace.task_public_id == task_public_id)
                .one_or_none()
            )
            if existing is not None:
                # 简化：已存在则更新（方便快速迭代）
                existing.companion_id = int(payload.companion_id)
                existing.root_path = root_path
                existing.agent_type = "trae-cli"  # 统一落库为 trae-cli
                existing.start_agent_command = start_agent_command
                s.add(existing)
                s.flush()
                space = existing
            else:
                space = AgentSpace(
                    task_public_id=task_public_id,
                    companion_id=int(payload.companion_id),
                    root_path=root_path,
                    agent_type="trae-cli",
                    start_agent_command=start_agent_command,
                )
                s.add(space)
                s.flush()

        return {"ok": True, "space_id": space.id}

    @router.get("/api/agent_spaces/{space_id}/start_agent_command")
    def get_start_agent_command(space_id: int) -> dict:
        sp, _ = _load_space_and_optional_companion(space_id)
        return {
            "ok": True,
            "start_agent_command": str(getattr(sp, "start_agent_command", "") or ""),
        }

    @router.put("/api/agent_spaces/{space_id}/start_agent_command")
    def update_start_agent_command(space_id: int, payload: dict) -> dict:
        raw = ""
        if isinstance(payload, dict):
            raw = str(
                payload.get("start_agent_command") or payload.get("command") or ""
            )
        command = raw.strip()
        if len(command) > 2000:
            raise HTTPException(status_code=400, detail="command is too long (<=2000)")
        with session_scope() as s:
            sp = s.get(AgentSpace, int(space_id))
            if sp is None:
                raise HTTPException(status_code=404, detail="AgentSpace not found")
            sp.start_agent_command = command
            s.add(sp)
        return {"ok": True, "start_agent_command": command}

    @router.delete("/api/tasks/{task_public_id}/agent_space")
    async def delete_agent_space(task_public_id: str) -> dict:
        return await delete_agent_space_for_task(grpc_server, task_public_id)

    @router.get("/api/agent_spaces/{space_id}/files/list")
    async def agent_space_files_list(space_id: int, path: str = "") -> dict:
        return await companion_service.list_space_files(
            grpc_server, space_id=space_id, path=path
        )

    @router.get("/api/agent_spaces/{space_id}/files/read")
    async def agent_space_files_read(space_id: int, path: str) -> dict:
        return await companion_service.read_space_file(
            grpc_server, space_id=space_id, path=path
        )

    @router.get("/api/agent_spaces/{space_id}/files/raw")
    async def agent_space_files_raw(space_id: int, path: str) -> Response:
        return await companion_service.raw_space_file(
            grpc_server, space_id=space_id, path=path
        )

    @router.get("/api/agent_spaces/{space_id}/terminals")
    async def terminals_list(space_id: int) -> dict:
        sp, comp = _load_space_and_optional_companion(space_id)
        owner = terminal_service.owner_for_agent_space(int(sp.id))

        cid = int(getattr(comp, "id", 0) or 0) if comp is not None else 0
        conn = grpc_server.registry.get(cid) if cid else None
        online = bool(conn is not None)
        terms = await terminal_ops.list_live_terminals(owner=owner, conn=conn)

        return {
            "ok": True,
            "companion": {
                "id": cid or None,
                "status": _companion_display_status(comp) if comp is not None else None,
                "online": online,
            },
            "terminals": [
                terminal_gateway.terminal_payload(
                    int(sp.id), t, route_prefix="/api/agent_spaces"
                )
                for t in terms
            ],
        }

    @router.post("/api/agent_spaces/{space_id}/terminals/new")
    async def terminals_new(space_id: int) -> dict:
        sp, comp = _load_space_and_optional_companion(space_id)
        conn = _require_companion_online(sp=sp, comp=comp)

        try:
            result = await terminal_ops.start_terminal(
                owner=terminal_service.owner_for_agent_space(int(sp.id)),
                conn=conn,
                companion_id=int(getattr(comp, "id", 0) or 0)
                if comp is not None
                else None,
                root_path=str(sp.root_path or ""),
                base_path=f"/api/agent_spaces/{int(sp.id)}/terminals/{{terminal_id}}/ttyd/",
                task_public_id=str(sp.task_public_id or ""),
                timeout_seconds=10.0,
            )
        except terminal_gateway.TerminalStartError as e:
            raise HTTPException(status_code=502, detail=str(e))

        _try_audit_memory(
            kind="terminal.created",
            source="web",
            summary=f"Created terminal `{result.name}`.",
            detail=f"AgentSpace {int(sp.id)} created terminal {result.terminal_id} at {str(sp.root_path or '')}.",
            task_public_id=str(sp.task_public_id or "") or None,
            metadata={
                "space_id": int(sp.id),
                "terminal_id": result.terminal_id,
                "name": result.name,
            },
        )

        terminal_payload = {
            "terminal_id": result.terminal_id,
            "name": result.name,
            "backend": result.backend,
        }
        if result.backend == "ttyd" and result.connect_url:
            terminal_payload["embed_url"] = _ttyd_embed_path(
                int(sp.id), result.terminal_id
            )
        return {"ok": True, "terminal": terminal_payload}

    @router.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/rename")
    async def terminals_rename(space_id: int, terminal_id: str, payload: dict) -> dict:
        sp, _ = _load_space_and_optional_companion(space_id)

        try:
            raw_name = terminal_ops.rename_terminal(
                owner=terminal_service.owner_for_agent_space(int(sp.id)),
                terminal_id=terminal_id,
                name=str((payload or {}).get("name") or ""),
            )
            tid = str(terminal_id or "").strip()
        except terminal_gateway.TerminalValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")
        except terminal_service.TerminalNameConflict:
            raise HTTPException(status_code=400, detail="name already exists")

        return {"ok": True, "terminal": {"terminal_id": tid, "name": raw_name}}

    @router.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/inject")
    async def terminals_inject(space_id: int, terminal_id: str, payload: dict) -> dict:
        sp, comp = _load_space_and_optional_companion(space_id)
        conn = _require_companion_online(sp=sp, comp=comp)
        tid = str(terminal_id or "").strip()
        try:
            raw = await terminal_ops.inject_input(
                owner=terminal_service.owner_for_agent_space(int(sp.id)),
                terminal_id=terminal_id,
                payload=payload,
                conn=conn,
                timeout_seconds=10.0,
            )
        except terminal_gateway.TerminalValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")
        except terminal_gateway.TerminalInputError as e:
            raise HTTPException(status_code=502, detail=str(e))

        _try_audit_memory(
            kind="terminal.input",
            source="web",
            summary=f"Terminal input injected to `{tid}`.",
            detail=_memory_decode_terminal_bytes(raw),
            task_public_id=str(sp.task_public_id or "") or None,
            metadata={"space_id": int(sp.id), "terminal_id": tid, "injected": True},
        )
        return {"ok": True}

    @router.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/auto_prompts")
    async def terminals_auto_prompts(
        space_id: int, terminal_id: str, payload: dict
    ) -> dict:
        sp, _ = _load_space_and_optional_companion(space_id)
        tid = str(terminal_id or "").strip()
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")
        try:
            terminal_ops.terminal_info(
                owner=terminal_service.owner_for_agent_space(int(sp.id)),
                terminal_id=tid,
            )
        except terminal_gateway.TerminalValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")
        enabled = bool((payload or {}).get("enabled"))
        prompt = str((payload or {}).get("prompt") or "").strip()
        if len(prompt) > 40000:
            raise HTTPException(status_code=400, detail="prompt is too long (<=40000)")
        if enabled and prompt:
            ttyd_auto_prompts[tid] = {"enabled": True, "prompt": prompt}
        else:
            ttyd_auto_prompts.pop(tid, None)
        return {"ok": True, "enabled": enabled}

    @router.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/mouse_mode")
    async def terminals_mouse_mode(
        space_id: int, terminal_id: str, payload: dict
    ) -> dict:
        sp, comp = _load_space_and_optional_companion(space_id)
        conn = _require_companion_online(sp=sp, comp=comp)
        enabled = bool((payload or {}).get("enabled"))
        try:
            actual = await terminal_ops.set_mouse_mode(
                owner=terminal_service.owner_for_agent_space(int(sp.id)),
                terminal_id=terminal_id,
                enabled=enabled,
                conn=conn,
                timeout_seconds=10.0,
            )
        except terminal_gateway.TerminalValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")
        except terminal_gateway.TerminalMouseModeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"ok": True, "enabled": actual}

    @router.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/close")
    async def terminals_close(space_id: int, terminal_id: str) -> dict:
        sp, comp = _load_space_and_optional_companion(space_id)

        tid = str(terminal_id or "").strip()

        # best-effort stop on Companion (offline 也允许 close：只保证 OpenFocus 侧不再展示)
        cid = int(getattr(comp, "id", 0) or 0) if comp is not None else 0
        conn = grpc_server.registry.get(cid) if cid else None
        try:
            await terminal_ops.close_terminal(
                owner=terminal_service.owner_for_agent_space(int(sp.id)),
                terminal_id=terminal_id,
                conn=conn,
                clear_auto_prompt=lambda terminal_id: ttyd_auto_prompts.pop(
                    terminal_id, None
                ),
                timeout_seconds=10.0,
            )
        except terminal_gateway.TerminalValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")

        _try_audit_memory(
            kind="terminal.closed",
            source="web",
            summary=f"Closed terminal `{tid}`.",
            detail=f"AgentSpace {int(sp.id)} removed terminal {tid}.",
            task_public_id=str(sp.task_public_id or "") or None,
            metadata={"space_id": int(sp.id), "terminal_id": tid},
        )

        return {"ok": True}

    def _load_ttyd_terminal(space_id: int, terminal_id: str) -> tuple[object, str]:
        sp, _ = _load_space_and_optional_companion(space_id)
        try:
            connect_url = terminal_ops.load_ttyd_connect_url(
                owner=terminal_service.owner_for_agent_space(int(sp.id)),
                terminal_id=terminal_id,
            )
        except terminal_gateway.TerminalValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")
        except terminal_gateway.TerminalUnavailable:
            raise HTTPException(status_code=404, detail="ttyd terminal not found")
        return sp, connect_url.rstrip("/")

    @router.api_route(
        "/api/agent_spaces/{space_id}/terminals/{terminal_id}/ttyd/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def terminals_ttyd_proxy(
        request: Request, space_id: int, terminal_id: str, path: str = ""
    ) -> Response:
        _, connect_url = _load_ttyd_terminal(space_id, terminal_id)
        target = terminal_gateway.ttyd_proxy_target(
            connect_url=connect_url,
            route_prefix="/api/agent_spaces",
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
        "/api/agent_spaces/{space_id}/terminals/{terminal_id}/ttyd/{path:path}"
    )
    async def terminals_ttyd_ws_proxy(
        websocket: WebSocket, space_id: int, terminal_id: str, path: str = ""
    ) -> None:
        subprotocols = [
            str(x).strip()
            for x in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if str(x).strip()
        ]
        await websocket.accept(subprotocol=subprotocols[0] if subprotocols else None)
        try:
            _, connect_url = _load_ttyd_terminal(space_id, terminal_id)
            target_info = terminal_gateway.ttyd_proxy_target(
                connect_url=connect_url,
                route_prefix="/api/agent_spaces",
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
                            await upstream.send(
                                _rewrite_ttyd_input_for_auto_prompts(
                                    terminal_id, msg["bytes"]
                                )
                            )
                        elif msg.get("text") is not None:
                            await upstream.send(
                                _rewrite_ttyd_input_for_auto_prompts(
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

    @router.get("/api/agent_spaces/{space_id}/terminals/{terminal_id}/history")
    def terminals_history(
        space_id: int,
        terminal_id: str,
        max_bytes: int = terminal_gateway.TERMINAL_HISTORY_PUBLIC_MAX_BYTES,
    ) -> dict:
        sp, _ = _load_space_and_optional_companion(space_id)
        try:
            return terminal_ops.load_history(
                owner=terminal_service.owner_for_agent_space(int(sp.id)),
                terminal_id=terminal_id,
                max_bytes=max_bytes,
            )
        except terminal_gateway.TerminalValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")

    @router.get("/api/agent_spaces/{space_id}/agent/sessions")
    def agent_sessions_list(space_id: int) -> dict:
        sp, comp = _load_space_and_optional_companion(space_id)
        with session_scope() as s:
            sessions = (
                s.query(AgentSession)
                .filter(AgentSession.space_id == int(sp.id))
                .order_by(AgentSession.id.desc())
                .all()
            )
        cid = int(getattr(comp, "id", 0) or 0) if comp is not None else 0
        online = bool(cid and (grpc_server.registry.get(cid) is not None))
        return {
            "ok": True,
            "companion": {
                "id": cid or None,
                "status": _companion_display_status(comp) if comp is not None else None,
                "online": online,
            },
            "sessions": [
                {
                    "session_id": ss.session_id,
                    "status": ss.status,
                    "agent_type": ss.agent_type,
                    "created_at": ss.created_at.isoformat()
                    if hasattr(ss.created_at, "isoformat")
                    else str(ss.created_at),
                    "updated_at": ss.updated_at.isoformat()
                    if hasattr(ss.updated_at, "isoformat")
                    else str(ss.updated_at),
                }
                for ss in sessions
            ],
        }

    @router.post("/api/agent_spaces/{space_id}/agent/sessions/new")
    async def agent_sessions_new(space_id: int) -> dict:
        sp, comp = _load_space_and_optional_companion(space_id)
        conn = _require_companion_online(sp=sp, comp=comp)

        session_id = str(uuid.uuid4())
        try:
            res = await conn.request_agent_start(
                session_id=session_id,
                root_path=str(sp.root_path or ""),
                agent_type=str(sp.agent_type or "trae-cli"),
                task_public_id=str(sp.task_public_id or ""),
                timeout_seconds=10.0,
            )
        except CompanionGrpcError as e:
            raise HTTPException(
                status_code=502, detail=f"Companion agent failed to start: {e}"
            )

        real_sid = (res.session_id or "").strip() or session_id
        with session_scope() as s:
            ss = AgentSession(
                session_id=real_sid,
                space_id=int(sp.id),
                task_public_id=str(sp.task_public_id or ""),
                companion_id=int(getattr(comp, "id", 0) or 0)
                if comp is not None
                else None,
                root_path=str(sp.root_path or ""),
                agent_type=str(sp.agent_type or "trae-cli"),
                status="active",
            )
            s.add(ss)
            s.flush()
        _try_audit_memory(
            kind="agent.session.created",
            source="web",
            summary=f"Created agent session `{real_sid}`.",
            detail=f"Agent type: {str(sp.agent_type or 'trae-cli')}\nRoot path: {str(sp.root_path or '')}",
            task_public_id=str(sp.task_public_id or "") or None,
            metadata={"space_id": int(sp.id), "session_id": real_sid},
        )
        return {"ok": True, "session": {"session_id": real_sid}}

    @router.get("/api/agent_spaces/{space_id}/agent/sessions/{session_id}/messages")
    def agent_session_messages(space_id: int, session_id: str) -> dict:
        sp, _comp = _load_space_and_optional_companion(space_id)
        sid = str(session_id or "").strip()
        if not sid:
            raise HTTPException(status_code=400, detail="session_id is required")

        with session_scope() as s:
            sess = (
                s.query(AgentSession)
                .filter(AgentSession.session_id == sid)
                .one_or_none()
            )
            if sess is None or int(sess.space_id) != int(sp.id):
                raise HTTPException(status_code=404, detail="Agent session not found")
            msgs = (
                s.query(AgentMessage)
                .filter(AgentMessage.session_id == sid)
                .order_by(AgentMessage.id.asc())
                .all()
            )

        return {
            "ok": True,
            "session": {"session_id": sid, "status": sess.status},
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "request_id": m.request_id,
                    "content": m.content,
                    "done": bool(m.done),
                    "error": m.error,
                    "created_at": m.created_at.isoformat()
                    if hasattr(m.created_at, "isoformat")
                    else str(m.created_at),
                }
                for m in msgs
            ],
        }

    @router.post("/api/agent_spaces/{space_id}/agent/sessions/{session_id}/terminate")
    async def agent_session_terminate(space_id: int, session_id: str) -> dict:
        sp, comp = _load_space_and_optional_companion(space_id)
        conn = _require_companion_online(sp=sp, comp=comp)
        sid = str(session_id or "").strip()
        if not sid:
            raise HTTPException(status_code=400, detail="session_id is required")

        with session_scope() as s:
            sess = (
                s.query(AgentSession)
                .filter(AgentSession.session_id == sid)
                .one_or_none()
            )
            if sess is None or int(sess.space_id) != int(sp.id):
                raise HTTPException(status_code=404, detail="Agent session not found")

        try:
            await conn.request_agent_terminate(session_id=sid, timeout_seconds=10.0)
        except CompanionGrpcError as e:
            raise HTTPException(
                status_code=502, detail=f"Companion agent failed to terminate: {e}"
            )

        with session_scope() as s:
            sess = (
                s.query(AgentSession)
                .filter(AgentSession.session_id == sid)
                .one_or_none()
            )
            if sess is not None:
                sess.status = "terminated"
                s.add(sess)
        _try_audit_memory(
            kind="agent.session.terminated",
            source="web",
            summary=f"Terminated agent session `{sid}`.",
            detail="User terminated the managed agent session.",
            task_public_id=str(sp.task_public_id or "") or None,
            metadata={"space_id": int(sp.id), "session_id": sid},
        )
        return {"ok": True}

    @router.post("/api/agent_spaces/{space_id}/agent/sessions/{session_id}/send")
    async def agent_session_send(
        request: Request, space_id: int, session_id: str
    ) -> dict:
        sp, comp = _load_space_and_optional_companion(space_id)
        conn = _require_companion_online(sp=sp, comp=comp)
        sid = str(session_id or "").strip()
        if not sid:
            raise HTTPException(status_code=400, detail="session_id is required")

        payload = await request.json()
        text_in = ""
        if isinstance(payload, dict):
            if isinstance(payload.get("text"), str):
                text_in = payload.get("text")
            elif isinstance(payload.get("prompt"), str):
                text_in = payload.get("prompt")
        user_text = str(text_in or "").strip()
        if not user_text:
            raise HTTPException(status_code=400, detail="text is required")

        # 校验 session 归属
        with session_scope() as s:
            sess = (
                s.query(AgentSession)
                .filter(AgentSession.session_id == sid)
                .one_or_none()
            )
            if sess is None or int(sess.space_id) != int(sp.id):
                raise HTTPException(status_code=404, detail="Agent session not found")
            sess_agent_type = str(sess.agent_type or "agent")
            sess_task_public_id = str(sess.task_public_id or "")
            sess_companion_id = int(sess.companion_id) if sess.companion_id else None

            user_msg = AgentMessage(
                session_id=sid, role="user", content=user_text, request_id="", done=True
            )
            s.add(user_msg)

            rid = str(uuid.uuid4())
            asst_msg = AgentMessage(
                session_id=sid, role="assistant", content="", request_id=rid, done=False
            )
            s.add(asst_msg)
            agent_activity_service.handle_runtime_signal(
                s,
                kind="runtime.turn.started",
                agent_runtime=sess_agent_type,
                session_id=sid,
                turn_id=rid,
                task_public_id=sess_task_public_id,
                companion_id=sess_companion_id,
                source="openfocus.agent_session.send",
                payload={"message": "Prompt submitted from OpenFocus AgentSpace."},
            )
            s.flush()

        injected = _inject_openfocus_prompt(
            base_url=_openfocus_base_url(request),
            task_public_id=str(sp.task_public_id or ""),
            session_id=sid,
            user_prompt=user_text,
        )

        _try_audit_memory(
            kind="agent.session.user_message",
            source="web",
            summary=f"Sent message to agent session `{sid}`.",
            detail=user_text,
            task_public_id=str(sp.task_public_id or "") or None,
            metadata={"space_id": int(sp.id), "session_id": sid},
        )

        try:
            await conn.request_agent_send(
                request_id=rid, session_id=sid, prompt=injected, timeout_seconds=10.0
            )
        except CompanionGrpcError as e:
            # 标记 assistant 消息失败并通过 SSE 通知
            with session_scope() as s:
                m = (
                    s.query(AgentMessage)
                    .filter(AgentMessage.session_id == sid)
                    .filter(AgentMessage.request_id == rid)
                    .filter(AgentMessage.role == "assistant")
                    .order_by(AgentMessage.id.desc())
                    .first()
                )
                if m is not None:
                    m.done = True
                    m.error = str(e)
                    s.add(m)
                agent_activity_service.handle_runtime_signal(
                    s,
                    kind="runtime.turn.failed",
                    agent_runtime=sess_agent_type,
                    session_id=sid,
                    turn_id=rid,
                    task_public_id=sess_task_public_id,
                    companion_id=sess_companion_id,
                    source="openfocus.agent_session.send",
                    payload={"error": str(e)},
                )
            _agent_sse_publish(
                sid,
                {
                    "type": "chunk",
                    "request_id": rid,
                    "session_id": sid,
                    "ok": False,
                    "text": "",
                    "done": True,
                    "error": str(e),
                },
            )
            raise HTTPException(
                status_code=502, detail=f"Companion agent send failed: {e}"
            )

        return {"ok": True, "request_id": rid}

    @router.get("/api/agent_spaces/{space_id}/agent/sessions/{session_id}/sse")
    async def agent_session_sse(space_id: int, session_id: str) -> StreamingResponse:
        sp, _comp = _load_space_and_optional_companion(space_id)
        sid = str(session_id or "").strip()
        if not sid:
            raise HTTPException(status_code=400, detail="session_id is required")

        with session_scope() as s:
            sess = (
                s.query(AgentSession)
                .filter(AgentSession.session_id == sid)
                .one_or_none()
            )
            if sess is None or int(sess.space_id) != int(sp.id):
                raise HTTPException(status_code=404, detail="Agent session not found")

        async def _gen():
            q = await _agent_sse_subscribe(sid)
            try:
                yield (
                    "event: hello\n"
                    + "data: "
                    + json.dumps({"session_id": sid}, ensure_ascii=False)
                    + "\n\n"
                )
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    et = str(ev.get("type") or "message")
                    yield (
                        "event: "
                        + et
                        + "\n"
                        + "data: "
                        + json.dumps(ev, ensure_ascii=False)
                        + "\n\n"
                    )
            finally:
                await _agent_sse_unsubscribe(sid, q)

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
