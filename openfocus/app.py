# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .companion.grpc import CompanionGrpcServer
from .db import get_engine
from .domains.inspirations import drafts as inspiration_drafts
from .domains.inspirations import presenters as inspiration_presenters
from .domains.inspirations import publishing as inspiration_publishing
from .domains.inspirations import resources as inspiration_resources
from .domains.inspirations import service as inspiration_service
from .domains.memory import service as memory_service
from .infrastructure import env as env_config
from .infrastructure import llm_config, streaming
from .infrastructure import migrations as migration_service
from .models import Base
from .web.routes import agent_spaces as agent_spaces_routes
from .web.routes import companions as companion_routes
from .web.routes import events as events_routes
from .web.routes import goals as goals_routes
from .web.routes import inspirations as inspirations_routes
from .web.routes import memory as memory_routes
from .web.routes import recommendations as recommendation_routes

_LOG = logging.getLogger(__name__)

env_config.load_dotenv_once()

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="OpenFocus", version="0.1.0")


# 静态资源：OpenFocus 终端面板前端（ttyd iframe 宿主 / tab 控制层）
_TERMINAL_PANEL_DIR = (APP_DIR / "static" / "terminal-panel").resolve()
if _TERMINAL_PANEL_DIR.exists() and _TERMINAL_PANEL_DIR.is_dir():
    app.mount(
        "/static/terminal-panel",
        StaticFiles(directory=str(_TERMINAL_PANEL_DIR)),
        name="terminal-panel",
    )

# 静态资源：内置资源（resources/，例如 icons）
_RESOURCES_DIR = (APP_DIR.parent / "resources").resolve()
if _RESOURCES_DIR.exists() and _RESOURCES_DIR.is_dir():
    app.mount(
        "/resources", StaticFiles(directory=str(_RESOURCES_DIR)), name="resources"
    )

# 静态资源：Vite 构建产物（openfocus/static/dist/）
_FRONTEND_DIST_DIR = (APP_DIR / "static" / "dist").resolve()
if _FRONTEND_DIST_DIR.exists() and _FRONTEND_DIST_DIR.is_dir():
    app.mount(
        "/static/dist",
        StaticFiles(directory=str(_FRONTEND_DIST_DIR)),
        name="frontend-dist",
    )

# OpenFocus(Control Plane) 内置 gRPC server：Companion(Data Plane) 以客户端方式连接进来。
COMPANION_GRPC = CompanionGrpcServer()


# 在模块加载时即安装监听器：即便测试/部署选择手动启动 gRPC server，AgentChunk 也能被持久化与 SSE 转发。
streaming.install_agent_chunk_listener_once()
streaming.install_terminal_listener_once()
streaming.install_runtime_signal_listener_once()


# App-level dependency wiring. Business rules live in domain services;
# this module only assembles FastAPI, lifecycle hooks, and route dependencies.
_utcnow = memory_service.utcnow
_DOTENV_LOADED = env_config._DOTENV_LOADED
_term_subscribe = streaming.terminal_event_hub.subscribe
_term_unsubscribe = streaming.terminal_event_hub.unsubscribe
_inspiration_fallback_draft = inspiration_drafts.fallback_draft


def _load_dotenv_once() -> None:
    global _DOTENV_LOADED
    if not _DOTENV_LOADED:
        env_config._DOTENV_LOADED = False
    llm_config._DOTENV_LOADED = bool(_DOTENV_LOADED)
    llm_config.load_dotenv_once()
    _DOTENV_LOADED = llm_config._DOTENV_LOADED


def _get_llm_provider_or_error():
    _load_dotenv_once()
    return llm_config.get_llm_provider_or_error()


def _select_online_companion(companion_id: int | None = None):
    return inspiration_service.select_online_companion(COMPANION_GRPC, companion_id)


def _has_online_companion() -> bool:
    return inspiration_service.has_online_companion(COMPANION_GRPC)


async def _kickoff_inspiration_followup(
    *, space_id: int, user_message_id: int, pending_message_id: int
) -> None:
    inspiration_service.drafts.fallback_draft = _inspiration_fallback_draft
    await inspiration_service.kickoff_followup(
        space_id=int(space_id),
        user_message_id=int(user_message_id),
        pending_message_id=int(pending_message_id),
        provider_factory=_get_llm_provider_or_error,
    )


async def _inspiration_enqueue_turn(space_id: int, content: str) -> dict:
    return await inspiration_service.enqueue_turn(
        int(space_id),
        content,
        provider_factory=_get_llm_provider_or_error,
        kickoff_func=_kickoff_inspiration_followup,
    )


async def _inspiration_release_terminals(space_id: int) -> int:
    return await inspiration_service.release_terminals(
        int(space_id),
        select_online=_select_online_companion,
        clear_ttyd_auto_prompts=lambda tid: (
            streaming.terminal_event_hub.ttyd_auto_prompts.pop(str(tid or ""), None)
        ),
    )


def _inspiration_terminal_conn(companion_id: int | None = None):
    return inspiration_service.terminal_conn(
        companion_id, select_online=_select_online_companion
    )


def _inspiration_load_publish_snapshot(space_id: int, draft_id: int) -> dict:
    return inspiration_publishing.load_publish_snapshot(int(space_id), int(draft_id))


def _inspiration_publish_sync(
    *, space_id: int, draft_id: int, due_date_iso: str, previous_status: str
) -> None:
    inspiration_publishing.publish_sync(
        space_id=int(space_id),
        draft_id=int(draft_id),
        due_date_iso=str(due_date_iso),
        previous_status=str(previous_status or "open"),
        load_snapshot=_inspiration_load_publish_snapshot,
        audit=memory_service.try_audit_memory,
    )


async def _kickoff_inspiration_publish(**kwargs) -> None:
    space_id = int(kwargs["space_id"])
    await asyncio.to_thread(
        _inspiration_publish_sync,
        space_id=space_id,
        draft_id=int(kwargs["draft_id"]),
        due_date_iso=str(kwargs["due_date_iso"]),
        previous_status=str(kwargs.get("previous_status") or "open"),
    )
    await inspiration_service.complete_publish_release(
        space_id=space_id, release_terminals=_inspiration_release_terminals
    )


def _human_duration_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {s}s" if s else f"{minutes}m"
    hours, m = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {m}m" if m else f"{hours}h"
    days, h = divmod(hours, 24)
    return f"{days}d {h}h" if h else f"{days}d"


def _human_since(ts: dt.datetime | None, *, now: dt.datetime | None = None) -> str:
    if ts is None:
        return "-"
    now = now or _utcnow()

    # SQLite/SQLAlchemy 在某些配置下会返回 naive datetime；这里统一按 UTC 处理。
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)

    return _human_duration_seconds(int((now - ts).total_seconds()))


templates.env.filters["human_since"] = _human_since


@app.on_event("startup")
def _startup() -> None:
    engine = get_engine()
    migration_service.initialize_database(engine, Base)

    try:
        memory_service.maintenance()
    except Exception as exc:
        _LOG.warning("startup memory maintenance failed: %s", exc)


@app.on_event("startup")
async def _startup_companion_grpc() -> None:
    streaming.install_agent_chunk_listener_once()
    streaming.install_runtime_signal_listener_once()
    # 测试里可能希望手动控制启动/端口
    if os.environ.get("OPENFOCUS_GRPC_AUTOSTART", "1") == "0":
        return
    await COMPANION_GRPC.start()


app.include_router(events_routes.router)
app.include_router(memory_routes.create_router(templates=templates))
app.include_router(
    companion_routes.create_router(grpc_server=COMPANION_GRPC, templates=templates)
)
app.include_router(
    recommendation_routes.create_router(
        provider_factory=lambda: _get_llm_provider_or_error()
    )
)


app.include_router(
    goals_routes.create_router(
        templates=templates,
        release_agent_space=lambda task_public_id: (
            agent_spaces_routes.delete_agent_space_for_task(
                COMPANION_GRPC, task_public_id
            )
        ),
    )
)
app.include_router(
    agent_spaces_routes.create_router(
        grpc_server=COMPANION_GRPC,
        templates=templates,
        ttyd_auto_prompts=streaming.terminal_event_hub.ttyd_auto_prompts,
        agent_sse_subscribe=streaming.agent_sse_hub.subscribe,
        agent_sse_unsubscribe=streaming.agent_sse_hub.unsubscribe,
        agent_sse_publish=streaming.agent_sse_hub.publish,
        rewrite_ttyd_input_for_auto_prompts=streaming.terminal_event_hub.rewrite_ttyd_input_for_auto_prompts,
    )
)
app.include_router(
    inspirations_routes.create_router(
        templates=templates,
        deps=SimpleNamespace(
            truncate_zh=inspiration_service.truncate_zh,
            utcnow=memory_service.utcnow,
            get_llm_provider_or_error=lambda: _get_llm_provider_or_error(),
            inspiration_space_payload=inspiration_presenters.space_payload,
            inspiration_workspace_path=inspiration_resources.workspace_path,
            inspiration_create_initial_note_resource=inspiration_resources.create_initial_note_resource,
            inspiration_non_deleted_resources=inspiration_resources.non_deleted_resources,
            inspiration_fallback_reply=inspiration_drafts.fallback_reply,
            inspiration_llm_reply=inspiration_drafts.llm_reply,
            try_audit_memory=memory_service.try_audit_memory,
            inspiration_maybe_emit_phase_summary=inspiration_service.maybe_emit_phase_summary,
            inspiration_space_or_404=inspiration_service.space_or_error,
            inspiration_is_waiting=inspiration_service.is_waiting,
            inspiration_messages_page=inspiration_service.messages_page,
            inspiration_is_publishing=inspiration_service.is_publishing,
            inspiration_message_payload=inspiration_presenters.message_payload,
            inspiration_resource_payload=inspiration_presenters.resource_payload,
            inspiration_draft_payload=inspiration_presenters.draft_payload,
            inspiration_publish_record_payload=inspiration_presenters.publish_record_payload,
            inspiration_enqueue_turn=_inspiration_enqueue_turn,
            inspiration_release_terminals=_inspiration_release_terminals,
            inspiration_space_files_dir=inspiration_resources.space_files_dir,
            inspiration_next_resource_seq=inspiration_resources.next_resource_seq,
            inspiration_store_uploaded_resource_file=inspiration_service.store_uploaded_resource_file,
            inspiration_write_resource_file=inspiration_resources.write_resource_file,
            guess_media_type=inspiration_resources.guess_media_type,
            inspiration_sync_resources_dir=inspiration_resources.sync_resources_dir,
            inspiration_sync_draft_summary_file=inspiration_resources.sync_draft_summary_file,
            inspiration_resource_reference=inspiration_resources.resource_reference,
            inspiration_prepare_publish=inspiration_service.prepare_publish,
            kickoff_inspiration_publish=lambda **kwargs: _kickoff_inspiration_publish(
                **kwargs
            ),
            inspiration_default_followup_title=inspiration_service.default_followup_title,
            inspiration_clone_resource=inspiration_resources.clone_resource,
            has_online_companion=lambda: _has_online_companion(),
            inspiration_terminal_payload=inspiration_service.terminal_payload,
            build_inspiration_draft_summary_prompt=inspiration_service.build_draft_summary_prompt,
            select_online_companion=lambda companion_id=None: _select_online_companion(
                companion_id
            ),
            inspiration_terminal_conn=_inspiration_terminal_conn,
            ttyd_auto_prompts=streaming.terminal_event_hub.ttyd_auto_prompts,
        ),
    )
)
