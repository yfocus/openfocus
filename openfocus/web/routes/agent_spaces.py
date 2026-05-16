# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from ...companion.grpc import CompanionGrpcError, CompanionGrpcServer
from ...db import session_scope
from ...domains.agent_spaces import terminals as terminal_service
from ...domains.companion import service as companion_service
from ...domains.memory import service as memory_service
from ...models import (
    AgentMessage,
    AgentSession,
    AgentSpace,
    AgentSpacePrompt,
    Companion,
    Goal,
    RemoteTerminalOutput,
    RemoteTerminalSession,
    Task,
)
from ...schemas import AgentSpaceCreateIn, AgentSpacePromptIn

_TERM_HISTORY_PUBLIC_MAX_BYTES = 4 * 1024 * 1024


def _ttyd_embed_path(space_id: int, terminal_id: str) -> str:
    tid = urllib.parse.quote(str(terminal_id or ""), safe="")
    return f"/api/agent_spaces/{int(space_id)}/terminals/{tid}/ttyd/"


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
        "按 OpenFocus Event Spec 上报：启动立刻报 agent.started；开始当前任务立刻报 task.started；阶段进展/重要中间结果/阻塞/失败用 "
        f'POST {base}/api/agent/events，JSON={{"kind":"task.progress","agent":"<agent>","task_id":taskId,"payload":{{"status":"running","message":"...","progress":0.5}}}}；'
        "合法 kind=task.started|task.progress|task.completed|task.failed|task.blocked|agent.started|agent.completed；"
        "payload 常用字段=status|message|summary|error|reason|progress|step|total_steps|metadata；"
        "status 按 spec 使用：running|succeeded|failed|blocked|waiting|canceled|in_progress|progress|success|ok|done|completed|fail|error|timeout|denied|panic|waiting_on_someone；"
        "完成时先报 agent.completed，再优先用 "
        f'POST {base}/api/skills/focus_report，JSON={{"agent":"<agent>","task_name":"...","status":"succeeded|failed|blocked|running","goal_id":null,"task_public_id":taskId,"user_prompt":"...","assistant_response":"...","metadata":{{}}}}；'
        "若 focus_report 不可用，再用 task.completed/task.failed/task.blocked；不要按 token/日志行/无意义心跳刷屏；completed/succeeded 只表示 Agent 报告完成，不会自动把任务标记为 done。"
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
        "请求体必须是 JSON，对任务相关事件统一使用：\n"
        '{"kind":"task.progress","agent":"<agent>","task_id":taskId,"payload":{"status":"running","message":"...","progress":0.5}}\n'
        "字段规范：kind=事件类型（必填，<=128 chars）；agent=上报方标识（必填）；task_id=任务相关事件必填，且必须等于 taskId；payload=对象（必填）。\n"
        "payload 合法常用字段：status、message、summary、error、reason、progress、step、total_steps、metadata。\n"
        "status 合法值按 spec 使用：running | succeeded | failed | blocked | waiting | canceled | in_progress | progress | success | ok | done | completed | fail | error | timeout | denied | panic | waiting_on_someone。\n"
        "kind 规范：task.started=任务开始；task.progress=阶段进度/重要中间结果；task.completed=Agent 认为已完成、等待用户确认；task.failed=无法完成；task.blocked=需要用户或外部条件；agent.started=一次 agent run 启动；agent.completed=一次 agent run 结束。\n"
        "上报时机必须明确遵守：\n"
        "1. Agent 一启动，立刻先上报 agent.started；开始处理该任务时，立刻上报 task.started。\n"
        "2. 长任务只在有意义的阶段变化、重要进展、阻塞、失败时上报 task.progress / task.blocked / task.failed，不要按 token、日志行或无意义心跳刷屏。\n"
        "不要按 token/日志行/无意义心跳刷屏。\n"
        "3. 完成时必须上报 agent.completed；若本次工作针对 task，随后上报最终任务结果：优先使用 /api/skills/focus_report，若不可用再用 task.completed / task.failed / task.blocked。\n"
        "4. task.completed / skill.focus_report(status=succeeded) 只表示 Agent 报告完成，不会自动把任务标记为 done。\n"
        "接口 2：POST /api/skills/focus_report（完整地址：{openfocusBaseUrl}/api/skills/focus_report，推荐用于最终结果）\n"
        '{"agent":"<agent>","task_name":"...","status":"succeeded|failed|blocked|running","goal_id":null,"task_public_id":taskId,"user_prompt":"...","assistant_response":"...","metadata":{}}\n'
        "focus_report.status 合法值按 spec 归一化：成功=succeeded|success|ok|done|completed；失败=failed|fail|error|timeout|denied|panic；阻塞=blocked|waiting|waiting_on_someone；进行中=running|in_progress|progress。\n"
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

        terms = terminal_service.list_terminals(
            s, terminal_service.owner_for_agent_space(int(space.id))
        )
        term_ids = [t.terminal_id for t in terms]

    cid = int(getattr(comp, "id", 0) or 0) if comp is not None else 0
    conn = grpc_server.registry.get(cid) if cid else None
    if conn is not None and term_ids:

        async def _stop_one(tid: str) -> None:
            try:
                await conn.request_terminal_stop(
                    terminal_id=str(tid), timeout_seconds=5.0
                )
            except Exception:
                pass

        await asyncio.gather(
            *[_stop_one(tid) for tid in term_ids], return_exceptions=True
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

        terminal_service.delete_owner_terminal_records(
            s, owner=terminal_service.owner_for_agent_space(int(space.id))
        )
        s.delete(space)

    return {"ok": True}


def create_router(
    *,
    grpc_server: CompanionGrpcServer,
    templates: Jinja2Templates,
    ttyd_agent_mode: dict[str, dict[str, object]],
    agent_sse_subscribe,
    agent_sse_unsubscribe,
    agent_sse_publish,
    rewrite_ttyd_input_for_agent_mode,
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
    _rewrite_ttyd_input_for_agent_mode = rewrite_ttyd_input_for_agent_mode
    _try_audit_memory = memory_service.try_audit_memory
    _memory_decode_terminal_bytes = memory_service.decode_terminal_bytes

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
        # 释放 AgentSpace 时：尽力清理所有远端资源（Remote Terminal），并删除 OpenFocus 侧记录。
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

            sessions = (
                s.query(AgentSession).filter(AgentSession.space_id == space.id).all()
            )
            sess_ids = [ss.session_id for ss in sessions]

            terms = terminal_service.list_terminals(
                s, terminal_service.owner_for_agent_space(int(space.id))
            )
            term_ids = [t.terminal_id for t in terms]

        # best-effort stop on Companion
        cid = int(getattr(comp, "id", 0) or 0) if comp is not None else 0
        conn = grpc_server.registry.get(cid) if cid else None
        if conn is not None and term_ids:

            async def _stop_one(tid: str) -> None:
                try:
                    await conn.request_terminal_stop(
                        terminal_id=str(tid), timeout_seconds=5.0
                    )
                except Exception:
                    # Companion 离线/失败时允许终端丢失；OpenFocus 侧仍清理记录。
                    pass

            await asyncio.gather(
                *[_stop_one(tid) for tid in term_ids], return_exceptions=True
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
                s.query(AgentMessage).filter(
                    AgentMessage.session_id.in_(sess_ids)
                ).delete(synchronize_session=False)
                s.query(AgentSession).filter(
                    AgentSession.session_id.in_(sess_ids)
                ).delete(synchronize_session=False)

            terminal_service.delete_owner_terminal_records(
                s, owner=terminal_service.owner_for_agent_space(int(space.id))
            )
            s.delete(space)

        return {"ok": True}

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
    def terminals_list(space_id: int) -> dict:
        sp, comp = _load_space_and_optional_companion(space_id)
        with session_scope() as s:
            owner = terminal_service.owner_for_agent_space(int(sp.id))
            terms = terminal_service.list_terminals(s, owner)

        cid = int(getattr(comp, "id", 0) or 0) if comp is not None else 0
        online = bool(cid and (grpc_server.registry.get(cid) is not None))

        def _terminal_payload(t: RemoteTerminalSession) -> dict:
            out = terminal_service.terminal_payload(t)
            backend = str(getattr(t, "backend", "") or "ttyd").strip() or "ttyd"
            connect_url = str(getattr(t, "connect_url", "") or "").strip()
            tid = str(t.terminal_id or "")
            if backend == "ttyd" and connect_url:
                out["embed_url"] = _ttyd_embed_path(int(sp.id), tid)
            return out

        return {
            "ok": True,
            "companion": {
                "id": cid or None,
                "status": _companion_display_status(comp) if comp is not None else None,
                "online": online,
            },
            "terminals": [_terminal_payload(t) for t in terms],
        }

    @router.post("/api/agent_spaces/{space_id}/terminals/new")
    async def terminals_new(space_id: int) -> dict:
        sp, comp = _load_space_and_optional_companion(space_id)
        conn = _require_companion_online(sp=sp, comp=comp)

        terminal_id = str(uuid.uuid4())
        ttyd_base_path = _ttyd_embed_path(int(sp.id), terminal_id)
        try:
            res = await conn.request_terminal_start(
                terminal_id=terminal_id,
                root_path=str(sp.root_path or ""),
                base_path=ttyd_base_path,
                timeout_seconds=10.0,
            )
        except CompanionGrpcError as e:
            raise HTTPException(
                status_code=502, detail=f"Companion terminal failed to start: {e}"
            )

        real_tid = (res.terminal_id or "").strip() or terminal_id
        backend = str(getattr(res, "backend", "") or "ttyd").strip() or "ttyd"
        connect_url = str(getattr(res, "connect_url", "") or "").strip()

        with session_scope() as s:
            owner = terminal_service.owner_for_agent_space(int(sp.id))
            t = terminal_service.create_terminal_record(
                s,
                owner=owner,
                task_public_id=str(sp.task_public_id or ""),
                companion_id=int(getattr(comp, "id", 0) or 0)
                if comp is not None
                else None,
                root_path=str(sp.root_path or ""),
                terminal_id=real_tid,
                backend=backend,
                connect_url=connect_url,
            )
            name = str(t.name or "")

        _try_audit_memory(
            kind="terminal.created",
            source="web",
            summary=f"Created terminal `{name}`.",
            detail=f"AgentSpace {int(sp.id)} created terminal {real_tid} at {str(sp.root_path or '')}.",
            task_public_id=str(sp.task_public_id or "") or None,
            metadata={"space_id": int(sp.id), "terminal_id": real_tid, "name": name},
        )

        terminal_payload = {"terminal_id": real_tid, "name": name, "backend": backend}
        if backend == "ttyd" and connect_url:
            terminal_payload["embed_url"] = _ttyd_embed_path(int(sp.id), real_tid)
        return {"ok": True, "terminal": terminal_payload}

    @router.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/rename")
    async def terminals_rename(space_id: int, terminal_id: str, payload: dict) -> dict:
        sp, _ = _load_space_and_optional_companion(space_id)

        tid = str(terminal_id or "").strip()
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")

        raw_name = str((payload or {}).get("name") or "").strip()
        if not raw_name:
            raise HTTPException(status_code=400, detail="name is required")
        if len(raw_name) > 128:
            raise HTTPException(status_code=400, detail="name is too long (<=128)")

        with session_scope() as s:
            owner = terminal_service.owner_for_agent_space(int(sp.id))
            try:
                terminal_service.rename_terminal(
                    s, owner=owner, terminal_id=tid, name=raw_name
                )
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
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")

        with session_scope() as s:
            owner = terminal_service.owner_for_agent_space(int(sp.id))
            try:
                terminal_service.get_terminal_for_owner(s, owner=owner, terminal_id=tid)
            except terminal_service.TerminalNotFound:
                raise HTTPException(status_code=404, detail="Terminal not found")

        raw = b""
        data_b64 = str((payload or {}).get("data_b64") or "")
        if data_b64:
            try:
                raw = base64.b64decode(data_b64)
            except Exception:
                raw = b""
        if not raw:
            text_value = str((payload or {}).get("text") or "")
            raw = text_value.encode("utf-8")
        if not raw:
            raise HTTPException(status_code=400, detail="data is required")

        _try_audit_memory(
            kind="terminal.input",
            source="web",
            summary=f"Terminal input injected to `{tid}`.",
            detail=_memory_decode_terminal_bytes(raw),
            task_public_id=str(sp.task_public_id or "") or None,
            metadata={"space_id": int(sp.id), "terminal_id": tid, "injected": True},
        )
        try:
            await conn.request_terminal_input(
                terminal_id=tid, data=raw, timeout_seconds=10.0
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"terminal inject failed: {e}")
        return {"ok": True}

    @router.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/agent_mode")
    async def terminals_agent_mode(
        space_id: int, terminal_id: str, payload: dict
    ) -> dict:
        sp, _ = _load_space_and_optional_companion(space_id)
        tid = str(terminal_id or "").strip()
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")
        with session_scope() as s:
            owner = terminal_service.owner_for_agent_space(int(sp.id))
            try:
                terminal_service.get_terminal_for_owner(s, owner=owner, terminal_id=tid)
            except terminal_service.TerminalNotFound:
                raise HTTPException(status_code=404, detail="Terminal not found")
        enabled = bool((payload or {}).get("enabled"))
        prefix = str((payload or {}).get("prefix") or "").strip()
        if enabled and prefix:
            ttyd_agent_mode[tid] = {"enabled": True, "prefix": prefix}
        else:
            ttyd_agent_mode.pop(tid, None)
        return {"ok": True, "enabled": enabled}

    @router.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/mouse_mode")
    async def terminals_mouse_mode(
        space_id: int, terminal_id: str, payload: dict
    ) -> dict:
        sp, comp = _load_space_and_optional_companion(space_id)
        conn = _require_companion_online(sp=sp, comp=comp)
        tid = str(terminal_id or "").strip()
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")
        with session_scope() as s:
            owner = terminal_service.owner_for_agent_space(int(sp.id))
            try:
                terminal_service.get_terminal_for_owner(s, owner=owner, terminal_id=tid)
            except terminal_service.TerminalNotFound:
                raise HTTPException(status_code=404, detail="Terminal not found")
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

    @router.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/close")
    async def terminals_close(space_id: int, terminal_id: str) -> dict:
        sp, comp = _load_space_and_optional_companion(space_id)

        tid = str(terminal_id or "").strip()
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")

        with session_scope() as s:
            owner = terminal_service.owner_for_agent_space(int(sp.id))
            try:
                terminal_service.get_terminal_for_owner(s, owner=owner, terminal_id=tid)
            except terminal_service.TerminalNotFound:
                raise HTTPException(status_code=404, detail="Terminal not found")

        # best-effort stop on Companion (offline 也允许 close：只保证 OpenFocus 侧不再展示)
        cid = int(getattr(comp, "id", 0) or 0) if comp is not None else 0
        conn = grpc_server.registry.get(cid) if cid else None
        if conn is not None:
            try:
                await conn.request_terminal_stop(terminal_id=tid, timeout_seconds=10.0)
            except Exception:
                pass

        with session_scope() as s:
            # 关闭即删除记录（避免刷新后重新出现 tab）
            terminal_service.delete_terminal_record(
                s,
                owner=terminal_service.owner_for_agent_space(int(sp.id)),
                terminal_id=tid,
            )
        ttyd_agent_mode.pop(tid, None)

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
        tid = str(terminal_id or "").strip()
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")
        with session_scope() as s:
            owner = terminal_service.owner_for_agent_space(int(sp.id))
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
        return sp, connect_url.rstrip("/")

    def _ttyd_target_url(base_url: str, tail: str, query: str) -> str:
        base = str(base_url or "").rstrip("/") + "/"
        tail = str(tail or "")
        if tail:
            # ttyd 启动时配置了 --base-path，因此这里必须把同样的 path 透传给 ttyd，
            # 不能剥掉 OpenFocus 的代理前缀，否则 ttyd 前端会用错误的 WebSocket path。
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

    @router.api_route(
        "/api/agent_spaces/{space_id}/terminals/{terminal_id}/ttyd/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def terminals_ttyd_proxy(
        request: Request, space_id: int, terminal_id: str, path: str = ""
    ) -> Response:
        _, connect_url = _load_ttyd_terminal(space_id, terminal_id)
        proxy_prefix = _ttyd_embed_path(space_id, terminal_id)
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
            proxy_prefix = _ttyd_embed_path(space_id, terminal_id)
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
                                _rewrite_ttyd_input_for_agent_mode(
                                    terminal_id, msg["bytes"]
                                )
                            )
                        elif msg.get("text") is not None:
                            await upstream.send(
                                _rewrite_ttyd_input_for_agent_mode(
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
        space_id: int, terminal_id: str, max_bytes: int = _TERM_HISTORY_PUBLIC_MAX_BYTES
    ) -> dict:
        sp, _ = _load_space_and_optional_companion(space_id)

        tid = str(terminal_id or "").strip()
        if not tid:
            raise HTTPException(status_code=400, detail="terminal_id is required")

        # 对外回放要限流：最多允许回放 _TERM_HISTORY_PUBLIC_MAX_BYTES。
        max_bytes = max(1024, min(int(max_bytes or 0), _TERM_HISTORY_PUBLIC_MAX_BYTES))

        with session_scope() as s:
            owner = terminal_service.owner_for_agent_space(int(sp.id))
            try:
                terminal_service.get_terminal_for_owner(s, owner=owner, terminal_id=tid)
            except terminal_service.TerminalNotFound:
                raise HTTPException(status_code=404, detail="Terminal not found")

            rows = (
                s.query(RemoteTerminalOutput)
                .filter(RemoteTerminalOutput.terminal_id == tid)
                .order_by(RemoteTerminalOutput.id.desc())
                .all()
            )

        def _slice_from_last_sync_point(data: bytes) -> tuple[bytes, bool, str]:
            """尽量从“可重建屏幕”的同步点开始回放。

            主要面向 TUI（例如 coco）：如果回放从半截控制序列/半截屏幕状态开始，xterm 很容易出现光标错位/残留字符。
            这里在最后一段历史里，找最后一次进入 alternate screen/清屏/重置的位置，从那里开始截取。
            """

            b = bytes(data or b"")
            if not b:
                return b, False, ""

            alt_enter_markers = [
                b"\x1b[?1049h",
                b"\x1b[?1047h",
                b"\x1b[?47h",
            ]
            alt_exit_markers = [
                b"\x1b[?1049l",
                b"\x1b[?1047l",
                b"\x1b[?47l",
            ]

            # 如果历史末尾仍处于 alternate screen（例如刷新页面时 vim 还开着），
            # 必须从“进入 alternate screen”的位置开始回放。否则若从 vim 内部的清屏
            # 序列开始回放，xterm 会把 vim 内容画到 normal buffer；之后 vim 退出时
            # 发送 ?1049l 就无法恢复/清掉这些内容，表现为“退出后 vim 画面残留”。
            last_alt_enter = max(
                (b.rfind(pat) for pat in alt_enter_markers), default=-1
            )
            last_alt_exit = max((b.rfind(pat) for pat in alt_exit_markers), default=-1)
            if last_alt_enter > max(last_alt_exit, -1):
                return b[last_alt_enter:], True, "alt_screen_active"

            markers: list[tuple[bytes, str]] = [
                (b"\x1b[?1049h", "alt_screen"),
                (b"\x1b[?1047h", "alt_screen"),
                (b"\x1b[?47h", "alt_screen"),
                (b"\x1bc", "reset"),
                (b"\x1b[2J", "clear"),
            ]
            best = -1
            why = ""
            for pat, tag in markers:
                i = b.rfind(pat)
                if i > best:
                    best = i
                    why = tag
            if best <= 0:
                return b, False, ""
            return b[best:], True, why

        buf: list[bytes] = []
        total = 0
        truncated = False
        for r in rows:
            try:
                b = base64.b64decode(str(r.data_b64 or ""))
            except Exception:
                b = b""
            if not b:
                continue
            if total + len(b) > max_bytes:
                truncated = True
                break
            buf.append(b)
            total += len(b)
        buf.reverse()
        raw = b"".join(buf) if buf else b""

        sliced, sliced_ok, sliced_reason = _slice_from_last_sync_point(raw)
        if sliced_ok:
            raw = sliced

        out_b64 = base64.b64encode(raw).decode("ascii") if raw else ""
        return {
            "ok": True,
            "terminal_id": tid,
            "data_b64": out_b64,
            "truncated": truncated,
            "sync_sliced": bool(sliced_ok),
            "sync_reason": str(sliced_reason or ""),
        }

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

            user_msg = AgentMessage(
                session_id=sid, role="user", content=user_text, request_id="", done=True
            )
            s.add(user_msg)

            rid = str(uuid.uuid4())
            asst_msg = AgentMessage(
                session_id=sid, role="assistant", content="", request_id=rid, done=False
            )
            s.add(asst_msg)
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
