# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from .agent.llm.openai_compat import OpenAICompatibleProvider
from .companion.grpc import (
    CompanionGrpcError,
    CompanionGrpcServer,
    add_agent_chunk_listener,
    add_terminal_output_listener,
)
from .db import get_engine, session_scope
from .domains.agent_spaces import terminals as terminal_service
from .domains.companion import service as companion_service
from .domains.goals import service as goal_service
from .domains.inspirations import drafts as inspiration_drafts
from .domains.inspirations import presenters as inspiration_presenters
from .domains.inspirations import publishing as inspiration_publishing
from .domains.inspirations import resources as inspiration_resources
from .domains.inspirations import terminal_bridge as inspiration_terminal_bridge
from .domains.memory import service as memory_service
from .infrastructure import migrations as migration_service
from .models import (
    AgentMessage,
    AgentSession,
    AgentSpace,
    Base,
    Companion,
    Event,
    Goal,
    InspirationDraft,
    InspirationMessage,
    InspirationPublishRecord,
    InspirationResource,
    InspirationSpace,
    NextMoveFeedback,
    NextMoveRun,
    RemoteTerminalOutput,
    RemoteTerminalSession,
    Task,
)
from .schemas import (
    AgentEventIn,
    AgentSpaceCreateIn,
    FocusReportIn,
)

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


# Agent SSE hub（按 session_id 组织）。
_AGENT_SSE_LOCK = asyncio.Lock()
_AGENT_SSE_SUBS: dict[str, set[asyncio.Queue[dict]]]
_AGENT_SSE_SUBS = {}
_AGENT_LISTENER_INSTALLED = False


async def _agent_sse_subscribe(session_id: str) -> asyncio.Queue[dict]:
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
    sid = str(session_id or "").strip()
    async with _AGENT_SSE_LOCK:
        _AGENT_SSE_SUBS.setdefault(sid, set()).add(q)
    return q


async def _agent_sse_unsubscribe(session_id: str, q: asyncio.Queue[dict]) -> None:
    sid = str(session_id or "").strip()
    async with _AGENT_SSE_LOCK:
        subs = _AGENT_SSE_SUBS.get(sid)
        if not subs:
            return
        subs.discard(q)
        if not subs:
            _AGENT_SSE_SUBS.pop(sid, None)


def _agent_sse_publish(session_id: str, ev: dict) -> None:
    sid = str(session_id or "").strip()
    subs = _AGENT_SSE_SUBS.get(sid)
    if not subs:
        return
    for q in list(subs):
        try:
            q.put_nowait(ev)
        except Exception:
            # 队列满/关闭：丢弃即可（前端可用 history 兜底）。
            pass


async def _persist_and_publish_agent_chunk(ch) -> None:
    # 1) SSE
    _agent_sse_publish(
        ch.session_id,
        {
            "type": "chunk",
            "request_id": ch.request_id,
            "session_id": ch.session_id,
            "ok": bool(ch.ok),
            "text": ch.text,
            "done": bool(ch.done),
            "error": ch.error,
        },
    )

    # 2) DB 持久化（assistant 消息按 request_id 增量追加）
    with session_scope() as s:
        msg = (
            s.query(AgentMessage)
            .filter(AgentMessage.session_id == ch.session_id)
            .filter(AgentMessage.request_id == ch.request_id)
            .filter(AgentMessage.role == "assistant")
            .order_by(AgentMessage.id.desc())
            .first()
        )
        if msg is None:
            msg = AgentMessage(
                session_id=ch.session_id,
                request_id=ch.request_id,
                role="assistant",
                content="",
                done=False,
                error="",
            )
            s.add(msg)
            s.flush()

        if ch.text:
            msg.content = (msg.content or "") + str(ch.text)
        if ch.error:
            msg.error = str(ch.error)
        if bool(ch.done) or (not bool(ch.ok)):
            msg.done = True
        s.add(msg)

        sess = (
            s.query(AgentSession)
            .filter(AgentSession.session_id == ch.session_id)
            .one_or_none()
        )
        if sess is not None:
            sess.updated_at = _utcnow()
            s.add(sess)

    if ch.text or ch.error or bool(ch.done):
        _try_audit_memory(
            kind="agent.session.chunk",
            source=f"agent:{str(getattr(ch, 'session_id', '') or '').strip() or 'session'}",
            summary="Agent session produced output.",
            detail=(
                str(ch.text or "") + (f"\n\nError: {ch.error}" if ch.error else "")
            ).strip(),
            metadata={
                "session_id": str(getattr(ch, "session_id", "") or ""),
                "request_id": str(getattr(ch, "request_id", "") or ""),
                "done": bool(getattr(ch, "done", False)),
                "ok": bool(getattr(ch, "ok", False)),
            },
        )


def _install_agent_chunk_listener_once() -> None:
    global _AGENT_LISTENER_INSTALLED
    if _AGENT_LISTENER_INSTALLED:
        return

    def _on_chunk(ch) -> None:
        try:
            asyncio.get_running_loop().create_task(_persist_and_publish_agent_chunk(ch))
        except RuntimeError:
            # 没有 event loop 时直接忽略（正常情况下不会发生）
            pass

    add_agent_chunk_listener(_on_chunk)
    _AGENT_LISTENER_INSTALLED = True


# 在模块加载时即安装监听器：即便测试/部署选择手动启动 gRPC server，AgentChunk 也能被持久化与 SSE 转发。
_install_agent_chunk_listener_once()


# Remote Terminal event hub/state.
_TERM_LOCK = asyncio.Lock()
_TERM_SUBS: dict[str, set[asyncio.Queue[dict]]]
_TERM_SUBS = {}
_TERM_LISTENER_INSTALLED = False
_TTYD_AGENT_MODE: dict[str, dict[str, object]] = {}


async def _term_subscribe(terminal_id: str) -> asyncio.Queue[dict]:
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=500)
    tid = str(terminal_id or "").strip()
    async with _TERM_LOCK:
        _TERM_SUBS.setdefault(tid, set()).add(q)
    return q


async def _term_unsubscribe(terminal_id: str, q: asyncio.Queue[dict]) -> None:
    tid = str(terminal_id or "").strip()
    async with _TERM_LOCK:
        subs = _TERM_SUBS.get(tid)
        if not subs:
            return
        subs.discard(q)
        if not subs:
            _TERM_SUBS.pop(tid, None)


def _term_publish(terminal_id: str, ev: dict) -> None:
    tid = str(terminal_id or "").strip()
    subs = _TERM_SUBS.get(tid)
    if not subs:
        return
    for q in list(subs):
        try:
            q.put_nowait(ev)
        except Exception:
            pass


def _rewrite_ttyd_input_for_agent_mode(terminal_id: str, msg):
    st = _TTYD_AGENT_MODE.get(str(terminal_id or "")) or {}
    if not bool(st.get("enabled")):
        return msg
    prefix = str(st.get("prefix") or "").strip()
    if not prefix:
        return msg
    paste_s = f"\x1b[200~ {prefix}\x1b[201~"
    if isinstance(msg, bytes):
        paste_b = paste_s.encode("utf-8")
        if msg.startswith(b"0"):
            return b"0" + msg[1:].replace(b"\r", paste_b + b"\r")
        return msg.replace(b"\r", paste_b + b"\r")
    if isinstance(msg, str):
        if msg.startswith("0"):
            return "0" + msg[1:].replace("\r", paste_s + "\r")
        return msg.replace("\r", paste_s + "\r")
    return msg


async def _handle_terminal_output(out) -> None:
    raw = bytes(out.data or b"")
    data_b64 = base64.b64encode(raw).decode("ascii") if raw else ""

    _term_publish(
        out.terminal_id,
        {
            "type": "output",
            "terminal_id": out.terminal_id,
            "data_b64": data_b64,
            "closed": bool(out.closed),
            "error": out.error,
        },
    )

    # 持久化输出（用于刷新/重进页面回放）。
    if raw:
        decoded = _memory_decode_terminal_bytes(raw)
        try:
            with session_scope() as s:
                ts = (
                    s.query(RemoteTerminalSession)
                    .filter(RemoteTerminalSession.terminal_id == out.terminal_id)
                    .one_or_none()
                )
                if ts is not None:
                    s.add(
                        RemoteTerminalOutput(
                            space_id=int(ts.space_id),
                            terminal_id=str(out.terminal_id or ""),
                            data_b64=data_b64,
                            nbytes=int(len(raw)),
                        )
                    )
                    s.flush()

                    # 控制体积：每个 terminal 最多保留最近 1MB。
                    total = (
                        s.query(text("COALESCE(SUM(nbytes), 0)"))
                        .select_from(RemoteTerminalOutput)
                        .filter(RemoteTerminalOutput.terminal_id == out.terminal_id)
                        .scalar()
                    )
                    try:
                        total = int(total or 0)
                    except Exception:
                        total = 0

                    if total > _TERM_HISTORY_MAX_BYTES:
                        need = int(total - _TERM_HISTORY_MAX_BYTES)
                        rows = (
                            s.query(
                                RemoteTerminalOutput.id, RemoteTerminalOutput.nbytes
                            )
                            .filter(RemoteTerminalOutput.terminal_id == out.terminal_id)
                            .order_by(RemoteTerminalOutput.id.asc())
                            .all()
                        )
                        del_ids: list[int] = []
                        freed = 0
                        for rid, nb in rows:
                            del_ids.append(int(rid))
                            freed += int(nb or 0)
                            if freed >= need:
                                break
                        if del_ids:
                            s.query(RemoteTerminalOutput).filter(
                                RemoteTerminalOutput.id.in_(del_ids)
                            ).delete(synchronize_session=False)
        except Exception:
            pass
        _try_audit_memory(
            kind="terminal.output",
            source="agentspace.shell",
            summary=f"Terminal output from {str(out.terminal_id or '').strip()}",
            detail=decoded,
            metadata={
                "terminal_id": str(out.terminal_id or ""),
                "closed": bool(out.closed),
                "error": str(out.error or ""),
            },
        )

    if bool(out.closed) or (out.error or ""):
        with session_scope() as s:
            ts = (
                s.query(RemoteTerminalSession)
                .filter(RemoteTerminalSession.terminal_id == out.terminal_id)
                .one_or_none()
            )
            if ts is not None:
                ts.status = "closed"
                s.add(ts)


def _install_terminal_listener_once() -> None:
    global _TERM_LISTENER_INSTALLED
    if _TERM_LISTENER_INSTALLED:
        return

    def _on_out(out) -> None:
        try:
            asyncio.get_running_loop().create_task(_handle_terminal_output(out))
        except RuntimeError:
            pass

    add_terminal_output_listener(_on_out)
    _TERM_LISTENER_INSTALLED = True


_install_terminal_listener_once()


# Remote Terminal：每个 terminal 最多保留最近 1GB 历史（用于刷新/重进页面回放）。
# 注意：该值会影响 SQLite 持久化体积与清理频率。
_TERM_HISTORY_MAX_BYTES = 1024 * 1024 * 1024

# 回放接口单次返回的最大体积（避免把 1GB 直接塞给浏览器/WS）。
_TERM_HISTORY_PUBLIC_MAX_BYTES = 4 * 1024 * 1024


def _ttyd_embed_path(space_id: int, terminal_id: str) -> str:
    tid = urllib.parse.quote(str(terminal_id or ""), safe="")
    return f"/api/agent_spaces/{int(space_id)}/terminals/{tid}/ttyd/"


def _inspiration_ttyd_embed_path(space_id: int, terminal_id: str) -> str:
    tid = urllib.parse.quote(str(terminal_id or ""), safe="")
    return f"/api/inspirations/{int(space_id)}/terminals/{tid}/ttyd/"


# Memory domain facade. Business rules and filesystem persistence live in
# openfocus.domains.memory.service; main.py keeps only routing/glue aliases while
# the rest of the god module is incrementally split by domain.
_utcnow = memory_service.utcnow
_memory_dir = memory_service.memory_dir
_memory_audit_root = memory_service.audit_root
_memory_daily_root = memory_service.daily_root
_memory_long_term_path = memory_service.long_term_path
_memory_load_state_unlocked = memory_service.load_state_unlocked
_memory_read_text = memory_service.read_text
_memory_iso = memory_service.iso
_memory_decode_terminal_bytes = memory_service.decode_terminal_bytes
_memory_collect_file_items = memory_service.collect_file_items
_memory_read_selected_file = memory_service.read_selected_file
_memory_maintenance = memory_service.maintenance
_try_audit_memory = memory_service.try_audit_memory


def _memory_force_audit_summary(now: dt.datetime) -> None:
    cfg = memory_service.config_from_env()
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    with memory_service._LOCK:
        state = memory_service.load_state_unlocked()
        memory_service.rotate_current_audit_unlocked(
            state, now, cfg=cfg, force=True, create_next=True
        )
        memory_service.finalize_due_days_unlocked(state, now)
        memory_service.cleanup_audit_files_unlocked(now, cfg)
        state["last_maintenance_at"] = memory_service.iso(now)
        memory_service.save_state_unlocked(state)


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
        _memory_maintenance()
    except Exception:
        pass


@app.on_event("startup")
async def _startup_companion_grpc() -> None:
    _install_agent_chunk_listener_once()
    # 测试里可能希望手动控制启动/端口
    if os.environ.get("OPENFOCUS_GRPC_AUTOSTART", "1") == "0":
        return
    await COMPANION_GRPC.start()


_DOTENV_LOADED = False


def _load_dotenv_once() -> None:
    """Best-effort load `.env` into process env (only if variables are missing).

    约定：
    - 默认读取启动目录（cwd）下的 `.env`
    - 若 cwd 下不存在，则再尝试读取“仓库根目录”（`openfocus/..`）下的 `.env`
      （解决从子目录启动服务时读不到 `.env` 的问题）
    - 可通过 `OPENFOCUS_ENV_FILE` 指定自定义路径
    - 永不覆盖已存在的环境变量
    """

    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True

    candidates: list[Path] = []
    env_file = str(os.environ.get("OPENFOCUS_ENV_FILE") or "").strip()

    # 默认行为：
    # - 运行应用时（非测试）：自动尝试加载 cwd/.env
    # - 运行测试时：默认不加载 cwd/.env（避免本地密钥污染测试）；只有显式指定 OPENFOCUS_ENV_FILE 才加载。
    mode = str(os.environ.get("OPENFOCUS_DOTENV") or "auto").strip().lower()
    if mode in {"0", "false", "off", "no"}:
        return
    if mode == "auto" and os.environ.get("PYTEST_CURRENT_TEST") and not env_file:
        return

    if env_file:
        try:
            candidates.append(Path(env_file).expanduser())
        except Exception:
            pass
    candidates.append(Path.cwd() / ".env")
    # 兼容：从源码目录推断仓库根目录（openfocus/..），避免 cwd 不在仓库根目录时读不到 .env。
    try:
        repo_env = Path(__file__).resolve().parent.parent / ".env"
        if repo_env not in candidates:
            candidates.append(repo_env)
    except Exception:
        pass

    def _strip_quotes(v: str) -> str:
        s = (v or "").strip()
        if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
            return s[1:-1]
        return s

    def _parse_line(line: str) -> tuple[str, str] | None:
        raw = (line or "").strip()
        if not raw or raw.startswith("#"):
            return None
        if raw.startswith("export "):
            raw = raw[len("export ") :].lstrip()
        if "=" not in raw:
            return None
        k, v = raw.split("=", 1)
        key = (k or "").strip()
        if not key:
            return None
        val = (v or "").strip()
        # 支持：KEY=VALUE # comment（仅对未加引号的值做截断）
        if val and not (val.startswith('"') or val.startswith("'")):
            hash_idx = val.find("#")
            if hash_idx >= 0:
                before = val[:hash_idx]
                # Treat `#` as a comment only when preceded by whitespace.
                if before.rstrip() != before:
                    val = before.strip()
        return key, _strip_quotes(val)

    for p in candidates:
        try:
            if not p.exists() or not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                kv = _parse_line(line)
                if not kv:
                    continue
                key, val = kv
                if key not in os.environ and val != "":
                    os.environ[key] = val
            break
        except Exception:
            continue


def _get_llm_provider_or_error() -> tuple[OpenAICompatibleProvider | None, str | None]:
    _load_dotenv_once()
    try:
        return OpenAICompatibleProvider.from_env(), None
    except Exception as e:
        return None, (
            "Missing LLM configuration. LLM-powered features are unavailable.\n"
            "Set one of the following environment variable groups:\n"
            "- OpenAI-compatible: OPENFOCUS_OPENAI_API_KEY (optionally OPENFOCUS_OPENAI_BASE_URL / OPENFOCUS_OPENAI_MODEL)\n"
            "- Ark: OPENFOCUS_ARK_API_KEY (or ARK_API_KEY), plus OPENFOCUS_ARK_BASE_URL / OPENFOCUS_ARK_MODEL (or ARK_BASE_URL / ARK_MODEL)\n"
            "You can also place a `.env` file in the startup directory (see `.env-default` at the repo root), or point to one with OPENFOCUS_ENV_FILE.\n"
            f"Error: {e}"
        )


def _truncate_zh(text: str, n: int = 20) -> str:
    s = (text or "").strip()
    if len(s) <= n:
        return s
    return s[:n].rstrip() + "…"


# Active Inspiration resource/workspace implementation lives in the domain module.
# These bindings keep route call sites stable while preventing new business logic
# from accumulating in main.py.
_openfocus_data_root = inspiration_resources.openfocus_data_root
_inspiration_files_root = inspiration_resources.files_root
_inspiration_space_files_dir = inspiration_resources.space_files_dir
_inspiration_workspace_path = inspiration_resources.workspace_path
_inspiration_resources_dir = inspiration_resources.resources_dir
_safe_resource_filename = inspiration_resources.safe_resource_filename
_inspiration_resource_file_path = inspiration_resources.resource_file_path
_inspiration_write_resource_file = inspiration_resources.write_resource_file
_inspiration_create_initial_note_resource = (
    inspiration_resources.create_initial_note_resource
)
_guess_media_type = inspiration_resources.guess_media_type
_inspiration_resource_reference = inspiration_resources.resource_reference
_inspiration_resource_preview = inspiration_resources.resource_preview
_inspiration_resource_name_from_path = inspiration_resources.resource_name_from_path
_inspiration_parse_url_resource_file = inspiration_resources.parse_url_resource_file
_inspiration_sync_resources_dir = inspiration_resources.sync_resources_dir
_inspiration_non_deleted_resources = inspiration_resources.non_deleted_resources


async def _inspiration_store_uploaded_resource_file(
    *, space_id: int, seq_id: int, file: UploadFile
) -> tuple[Path, str]:
    content = await file.read()
    try:
        return inspiration_resources.store_uploaded_resource_bytes(
            space_id=int(space_id),
            seq_id=int(seq_id),
            original_name=str(file.filename or "image"),
            content=content,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _inspiration_sync_draft_summary_file(
    s, space: InspirationSpace
) -> InspirationResource | None:
    try:
        return inspiration_resources.sync_draft_summary_file(s, space)
    except inspiration_resources.EmptyDraftSummary as e:
        raise HTTPException(status_code=400, detail=str(e))
    except inspiration_resources.DraftSummaryReadError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _inspiration_messages_page(
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


_inspiration_context_lines = inspiration_drafts.context_lines
_inspiration_fallback_reply = inspiration_drafts.fallback_reply
_inspiration_fallback_title_suggestions = inspiration_drafts.fallback_title_suggestions
_inspiration_fallback_draft = inspiration_drafts.fallback_draft
_inspiration_llm_reply = inspiration_drafts.llm_reply
_inspiration_llm_title_suggestions = inspiration_drafts.llm_title_suggestions
_inspiration_llm_draft = inspiration_drafts.llm_draft
_inspiration_make_phase_summary = inspiration_drafts.make_phase_summary


def _inspiration_maybe_emit_phase_summary(space_id: int) -> None:
    with session_scope() as s:
        space = s.get(InspirationSpace, int(space_id))
        if space is None:
            return
        now = _utcnow()
        due_turns = (
            int(space.message_turn_count or 0) - int(space.last_phase_summary_turn or 0)
            >= 10
        )
        due_time = False
        if getattr(space, "last_phase_summary_at", None) is None:
            due_time = False
        else:
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
        resources = _inspiration_non_deleted_resources(s, int(space_id))
        detail = _inspiration_make_phase_summary(space, messages, resources)
        space.last_phase_summary_turn = int(space.message_turn_count or 0)
        space.last_phase_summary_at = now
        _try_audit_memory(
            kind="inspiration.phase_summary",
            source="inspiration",
            summary=f"Inspiration space {int(space_id)} reached a phase-summary checkpoint.",
            detail=detail,
            metadata={
                "space_id": int(space_id),
                "message_turn_count": int(space.message_turn_count or 0),
            },
        )


_inspiration_next_resource_seq = inspiration_resources.next_resource_seq


def _inspiration_latest_draft(s, space_id: int) -> InspirationDraft | None:
    return (
        s.query(InspirationDraft)
        .filter(InspirationDraft.space_id == int(space_id))
        .order_by(InspirationDraft.version.desc(), InspirationDraft.id.desc())
        .first()
    )


def _inspiration_default_followup_title(title: str) -> str:
    base = str(title or "Inspiration").strip() or "Inspiration"
    return (base + " / Follow-up")[:120]


_inspiration_build_published_summary = inspiration_publishing.build_published_summary
_inspiration_clone_resource = inspiration_resources.clone_resource
_inspiration_space_payload = inspiration_presenters.space_payload
_inspiration_message_payload = inspiration_presenters.message_payload
_inspiration_resource_payload = inspiration_presenters.resource_payload
_inspiration_draft_payload = inspiration_presenters.draft_payload
_inspiration_publish_record_payload = inspiration_presenters.publish_record_payload


def _inspiration_space_or_404(s, space_id: int) -> InspirationSpace:
    space = s.get(InspirationSpace, int(space_id))
    if space is None:
        raise HTTPException(status_code=404, detail="Inspiration space not found")
    # Backfill workspace lazily for spaces created before workspace support.
    workspace = _inspiration_workspace_path(space, int(space_id))
    if not str(getattr(space, "workspace_path", "") or "").strip():
        space.workspace_path = str(workspace)
    if not str(getattr(space, "mode", "") or "").strip():
        space.mode = "built_in"
    return space


def _inspiration_latest_pending_message(s, space_id: int) -> InspirationMessage | None:
    return (
        s.query(InspirationMessage)
        .filter(InspirationMessage.space_id == int(space_id))
        .filter(InspirationMessage.kind == "pending")
        .order_by(InspirationMessage.id.desc())
        .first()
    )


def _inspiration_is_waiting(s, space_id: int) -> bool:
    return _inspiration_latest_pending_message(s, int(space_id)) is not None


def _inspiration_command_kind(content: str) -> str:
    text = str(content or "").strip()
    if text in {"/summary_title", "/summary-title"}:
        return "summary_title"
    if text in {"/plan", "/draft_goal_tasks"} or text.startswith(
        ("/plan\n", "/draft_goal_tasks\n")
    ):
        return "plan"
    return "message"


def _inspiration_user_message_kind(content: str) -> str:
    return "command" if _inspiration_command_kind(content) != "message" else "message"


def _inspiration_pending_text(command_kind: str) -> str:
    if command_kind == "summary_title":
        return "Generating title suggestions…"
    if command_kind == "plan":
        return "Generating a publish-ready draft…"
    return "Thinking…"


def _inspiration_is_publishing(space: InspirationSpace | None) -> bool:
    return str(getattr(space, "status", "") or "") == "publishing"


def _inspiration_generate_followup_result(
    *,
    command_kind: str,
    provider: OpenAICompatibleProvider | None,
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
    user_text: str,
) -> dict:
    if command_kind == "summary_title":
        if provider is None:
            titles = _inspiration_fallback_title_suggestions(space, messages)
        else:
            try:
                titles = _inspiration_llm_title_suggestions(
                    provider,
                    space=space,
                    messages=messages,
                    resources=resources,
                )
            except Exception:
                titles = _inspiration_fallback_title_suggestions(space, messages)
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
            data = _inspiration_fallback_draft(space, messages, resources)
        else:
            try:
                data = _inspiration_llm_draft(
                    provider,
                    space=space,
                    messages=messages,
                    resources=resources,
                )
            except Exception:
                data = _inspiration_fallback_draft(space, messages, resources)
        return {
            "message_kind": "draft_generated",
            "draft_data": data,
            "audit_kind": "inspiration.draft_generated",
            "audit_detail": "",
            "audit_metadata": {},
        }

    if provider is None:
        reply = _inspiration_fallback_reply(space, user_text)
    else:
        try:
            reply = _inspiration_llm_reply(
                provider,
                space=space,
                messages=messages,
                resources=resources,
            )
        except Exception:
            reply = _inspiration_fallback_reply(space, user_text)
    return {
        "message_kind": "message",
        "content": reply,
        "payload": {},
        "audit_kind": "inspiration.message",
        "audit_detail": user_text,
        "audit_metadata": {},
    }


async def _kickoff_inspiration_followup(
    *, space_id: int, user_message_id: int, pending_message_id: int
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
            resources = _inspiration_non_deleted_resources(s, int(space_id))
            command_kind = _inspiration_command_kind(str(user_message.content or ""))

        provider, _err = _get_llm_provider_or_error()
        user_text = str(user_message.content or "")
        try:
            timeout_seconds = float(
                os.environ.get("OPENFOCUS_INSPIRATION_AGENT_TIMEOUT_SECONDS") or 120
            )
        except Exception:
            timeout_seconds = 120.0
        result = await asyncio.wait_for(
            asyncio.to_thread(
                _inspiration_generate_followup_result,
                command_kind=command_kind,
                provider=provider,
                space=space,
                messages=messages,
                resources=resources,
                user_text=user_text,
            ),
            timeout=max(10.0, timeout_seconds),
        )
        audit_kind = str(result.get("audit_kind") or audit_kind)
        audit_detail = str(result.get("audit_detail") or audit_detail)
        audit_metadata.update(result.get("audit_metadata") or {})

        if command_kind == "summary_title":
            with session_scope() as s:
                pending = s.get(InspirationMessage, int(pending_message_id))
                current_space = s.get(InspirationSpace, int(space_id))
                if pending is None or current_space is None:
                    return
                pending.kind = str(result.get("message_kind") or "title_suggestions")
                pending.content = str(result.get("content") or "")
                pending.payload = result.get("payload") or {}
                current_space.last_activity_at = _utcnow()
        elif command_kind == "plan":
            data = result.get("draft_data") or {}
            with session_scope() as s:
                pending = s.get(InspirationMessage, int(pending_message_id))
                current_space = s.get(InspirationSpace, int(space_id))
                if pending is None or current_space is None:
                    return
                latest = _inspiration_latest_draft(s, int(space_id))
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
                draft_payload = _inspiration_draft_payload(draft)
                pending.kind = "draft_generated"
                pending.content = f"Draft v{int(draft.version)} is ready. Review it in the chat stream and decide whether to publish."
                pending.payload = {"draft": draft_payload}
                pending.draft_version = int(draft.version)
                current_space.last_activity_at = _utcnow()
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
                current_space.last_activity_at = _utcnow()

        await asyncio.to_thread(_inspiration_maybe_emit_phase_summary, int(space_id))
        await asyncio.to_thread(
            _try_audit_memory,
            kind=audit_kind,
            source="inspiration",
            summary=f"Inspiration space {int(space_id)} completed a {command_kind} turn.",
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
                current_space.last_activity_at = _utcnow()
        await asyncio.to_thread(
            _try_audit_memory,
            kind="inspiration.error",
            source="inspiration",
            summary=f"Inspiration space {int(space_id)} failed to generate a response.",
            detail=str(e),
            metadata={
                "space_id": int(space_id),
                "user_message_id": int(user_message_id),
            },
        )


def _inspiration_prepare_publish(
    space_id: int, draft_id: int | None, due_date: dt.date
) -> dict:
    try:
        return inspiration_publishing.prepare_publish(int(space_id), draft_id, due_date)
    except inspiration_publishing.PublishConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except inspiration_publishing.PublishUnavailable as e:
        detail = str(e)
        status_code = 404 if detail == "Inspiration space not found" else 400
        raise HTTPException(status_code=status_code, detail=detail)


def _inspiration_load_publish_snapshot(space_id: int, draft_id: int) -> dict:
    return inspiration_publishing.load_publish_snapshot(int(space_id), int(draft_id))


def _inspiration_publish_sync(
    *,
    space_id: int,
    draft_id: int,
    due_date_iso: str,
    previous_status: str,
) -> None:
    inspiration_publishing.publish_sync(
        space_id=int(space_id),
        draft_id=int(draft_id),
        due_date_iso=str(due_date_iso),
        previous_status=str(previous_status or "open"),
        load_snapshot=_inspiration_load_publish_snapshot,
        audit=_try_audit_memory,
    )


async def _kickoff_inspiration_publish(
    *,
    space_id: int,
    draft_id: int,
    due_date_iso: str,
    previous_status: str,
) -> None:
    await asyncio.to_thread(
        _inspiration_publish_sync,
        space_id=int(space_id),
        draft_id=int(draft_id),
        due_date_iso=str(due_date_iso),
        previous_status=str(previous_status or "open"),
    )
    with session_scope() as s:
        space = s.get(InspirationSpace, int(space_id))
        should_release = space is not None and str(space.status or "") in {
            "published",
            "publishing_releasing",
        }
    if should_release:
        await _inspiration_release_terminals(int(space_id))
        with session_scope() as s:
            space = s.get(InspirationSpace, int(space_id))
            if space is not None and str(space.status or "") == "publishing_releasing":
                space.status = "published"
                space.last_activity_at = _utcnow()


async def _inspiration_enqueue_turn(space_id: int, content: str) -> dict:
    text = str(content or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="content is required")
    if len(text) > 20000:
        text = text[:20000]
    with session_scope() as s:
        space = _inspiration_space_or_404(s, int(space_id))
        if str(space.status or "open") != "open":
            raise HTTPException(
                status_code=400, detail="Only open spaces accept new messages"
            )
        if _inspiration_is_waiting(s, int(space_id)):
            raise HTTPException(status_code=409, detail="Agent is still responding")
        now = _utcnow()
        command_kind = _inspiration_command_kind(text)
        user_message = InspirationMessage(
            space_id=int(space_id),
            role="user",
            kind=_inspiration_user_message_kind(text),
            content=text,
        )
        pending_message = InspirationMessage(
            space_id=int(space_id),
            role="assistant",
            kind="pending",
            content=_inspiration_pending_text(command_kind),
            payload={"command": command_kind},
        )
        s.add(user_message)
        s.add(pending_message)
        space.message_turn_count = int(space.message_turn_count or 0) + 1
        space.last_activity_at = now
        s.flush()
        user_payload = _inspiration_message_payload(user_message)
        pending_payload = _inspiration_message_payload(pending_message)
        queued_command = command_kind
        user_message_id = int(user_message.id)
        pending_message_id = int(pending_message.id)

    try:
        asyncio.get_running_loop().create_task(
            _kickoff_inspiration_followup(
                space_id=int(space_id),
                user_message_id=int(user_message_id),
                pending_message_id=int(pending_message_id),
            )
        )
    except RuntimeError:
        pass

    return {
        "ok": True,
        "queued": True,
        "command_kind": queued_command,
        "user_message": user_payload,
        "assistant_message": pending_payload,
        "is_waiting": True,
    }


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/goals", status_code=302)


@app.get("/goals", response_class=HTMLResponse)
def goals_list(request: Request) -> HTMLResponse:
    with session_scope() as s:
        # Dashboard 左侧目标列表：支持筛选/排序
        goal_filter = (request.query_params.get("gfilter") or "ALL").strip().upper()
        goal_sort = (request.query_params.get("gsort") or "DDL").strip().upper()

        goals_all = s.query(Goal).order_by(Goal.id.desc()).all()
        today = dt.date.today()

        # 仅对当前页面所需的 goals 做聚合
        goal_ids = [g.id for g in goals_all]
        tasks = []
        if goal_ids:
            tasks = (
                s.query(Task)
                .filter(Task.goal_id.in_(goal_ids))
                .order_by(Task.id.asc())
                .all()
            )

        tasks_by_goal: dict[int, list[Task]] = {}
        for t in tasks:
            tasks_by_goal.setdefault(t.goal_id, []).append(t)

        # AgentSpace：用于 Task 详情页展示“创建/进入工作区”
        public_ids = [t.public_id for t in tasks]
        agent_spaces_by_task: dict[str, AgentSpace] = {}
        if public_ids:
            spaces = (
                s.query(AgentSpace)
                .filter(AgentSpace.task_public_id.in_(public_ids))
                .all()
            )
            for sp in spaces:
                agent_spaces_by_task[sp.task_public_id] = sp

        # 尽量用已有 events 推断“进行中/进度百分比/最近更新时间”
        public_ids = [t.public_id for t in tasks]
        latest_event_by_task: dict[str, Event] = {}
        if public_ids:
            evs = (
                s.query(Event)
                .filter(Event.task_id.in_(public_ids))
                .order_by(Event.id.desc())
                .all()
            )
            for ev in evs:
                if ev.task_id and ev.task_id not in latest_event_by_task:
                    latest_event_by_task[ev.task_id] = ev

        # 任务详情栏需要展示“与该任务相关的事件”（只展示最近 N 条，避免页面过重）。
        # 注意：事件展示面向人，不直接暴露内部 kind/status 码。
        task_events: dict[str, list[dict]] = {pid: [] for pid in public_ids}

        # Goal 的事件：聚合该 Goal 下各 Task 的事件（用于 Dashboard 中间栏 Goal->Event）。
        # 先初始化，保证即使没有 task 也不会出现未定义。
        task_goal_by_pid: dict[str, int] = {t.public_id: t.goal_id for t in tasks}
        goal_events: dict[int, list[dict]] = {g.id: [] for g in goals_all}
        if public_ids:
            per_task_limit = 12
            evs = (
                s.query(Event)
                .filter(Event.task_id.in_(public_ids))
                .order_by(Event.id.desc())
                .all()
            )
            for ev in evs:
                pid = ev.task_id
                if not pid or pid not in task_events:
                    continue
                if len(task_events[pid]) >= per_task_limit:
                    continue
                task_events[pid].append(
                    {
                        "id": ev.id,
                        "kind": ev.kind,
                        "kind_label": _event_kind_label(ev.kind, ev.payload or {}),
                        "source_label": _event_source_label(ev.agent),
                        "created_at": ev.created_at,
                        "summary": _event_summary(ev.kind, ev.payload or {}),
                    }
                )

        for pid, evs in task_events.items():
            gid = task_goal_by_pid.get(pid)
            if gid is None or gid not in goal_events:
                continue
            for it in evs:
                # 额外带上 task_public_id，未来可在 UI 里做“打开该任务”。
                goal_events[gid].append({**it, "task_public_id": pid})

        # Goal 级事件：用于 Goal 详情页的 Event 区块（例如“confirm done by user”）。
        # 同时记录“完成时间”，用于 Dashboard 左侧排序。
        goal_done_at: dict[int, dt.datetime] = {}
        goal_level_evs = (
            s.query(Event)
            .filter(Event.kind.like("goal.%"))
            .order_by(Event.id.desc())
            .limit(200)
            .all()
        )
        for ev in goal_level_evs:
            payload = ev.payload or {}
            try:
                gid = int((payload or {}).get("goal_id") or 0)
            except Exception:
                gid = 0
            if not gid or gid not in goal_events:
                continue

            if ev.kind == "goal.confirmed_done_by_user":
                prev = goal_done_at.get(gid)
                if prev is None or (
                    hasattr(ev.created_at, "timestamp")
                    and hasattr(prev, "timestamp")
                    and ev.created_at > prev
                ):
                    goal_done_at[gid] = ev.created_at

            goal_events[gid].append(
                {
                    "id": ev.id,
                    "kind": ev.kind,
                    "kind_label": _event_kind_label(ev.kind, payload),
                    "source_label": _event_source_label(ev.agent),
                    "created_at": ev.created_at,
                    "summary": _event_summary(ev.kind, payload),
                    "task_public_id": None,
                }
            )
        for gid, evs in goal_events.items():
            evs.sort(key=lambda x: x.get("created_at") or _utcnow(), reverse=True)
            goal_events[gid] = evs[:30]

        task_meta: dict[str, dict] = {}
        now = _utcnow()
        for t in tasks:
            ev = latest_event_by_task.get(t.public_id)
            last_at = None
            kind = None
            if ev is not None:
                kind = ev.kind
                last_at = ev.created_at

            ui_status = "todo"
            if t.status == "done":
                ui_status = "done"
            else:
                if kind in {"task.started", "task.progress"}:
                    ui_status = "in_progress"

            task_meta[t.public_id] = {
                "ui_status": ui_status,
                # 产品约束：进度仅二元（完成/未完成），不展示 80% 等百分比。
                "percent": (100 if t.status == "done" else None),
                "last_event_at": last_at,
                "elapsed": _human_since(last_at or t.created_at, now=now),
            }

        def _task_sort_key(t: Task):
            meta = task_meta.get(t.public_id, {}) or {}
            ui_status = (
                str(meta.get("ui_status") or getattr(t, "status", "") or "todo")
                .strip()
                .lower()
            )
            status_rank = {
                "in_progress": 0,
                "todo": 1,
                "blocked": 2,
                "done": 9,
            }.get(ui_status, 3)
            created_at = getattr(t, "created_at", None) or _utcnow()
            created_ts = (
                created_at.timestamp() if hasattr(created_at, "timestamp") else 0
            )
            return (status_rank, -created_ts, -int(getattr(t, "id", 0) or 0))

        for gid, grouped_tasks in tasks_by_goal.items():
            grouped_tasks.sort(key=_task_sort_key)

        def _goal_group(g: Goal) -> int:
            # 0: in_progress, 1: expired, 2: completed
            if (g.status or "").strip() == "done":
                return 2
            if getattr(g, "due_date", None) and g.due_date < today:
                return 1
            return 0

        def _accept_goal(g: Goal) -> bool:
            x = goal_filter
            if x == "ALL":
                return True
            grp = _goal_group(g)
            if x in {"IN_PROGRESS", "INPROGRESS", "IN-PROGRESS"}:
                return grp == 0
            if x == "EXPIRED":
                return grp == 1
            if x == "COMPLETED":
                return grp == 2
            return True

        def _sort_key(g: Goal):
            grp = _goal_group(g)
            created_at = getattr(g, "created_at", None) or _utcnow()
            # 只对已完成的 goal 使用 done_at；否则为空
            done_at = goal_done_at.get(int(g.id)) if grp == 2 else None

            # 统一把“已完成”放到下面（grp 参与排序），满足默认要求
            if goal_sort in {"CREATED", "CREATED_AT", "CREATED_EVENT"}:
                # 新建优先（倒序）
                return (
                    grp,
                    -(
                        created_at.timestamp()
                        if hasattr(created_at, "timestamp")
                        else 0
                    ),
                    -int(g.id),
                )
            if goal_sort in {"COMPLETED", "COMPLETED_AT", "DONE", "DONE_AT"}:
                # 完成时间优先（倒序）；未完成放在各自组里按创建时间兜底
                ts_done = (
                    done_at.timestamp()
                    if (done_at and hasattr(done_at, "timestamp"))
                    else -1
                )
                ts_created = (
                    created_at.timestamp() if hasattr(created_at, "timestamp") else 0
                )
                return (grp, -ts_done if grp == 2 else -ts_created, -int(g.id))
            # 默认 DDL（due_date 升序；同 DDL 以创建时间倒序）
            due = getattr(g, "due_date", None) or today
            ts_created = (
                created_at.timestamp() if hasattr(created_at, "timestamp") else 0
            )
            return (
                grp,
                int(due.toordinal()) if hasattr(due, "toordinal") else 0,
                -ts_created,
                -int(g.id),
            )

        goals = [g for g in goals_all if _accept_goal(g)]
        goals.sort(key=_sort_key)

        # Dashboard 左栏显示用标题截断（不再维护独立 summary 字段）。
        goal_display: dict[int, str] = {}
        for g in goals:
            goal_display[g.id] = _truncate_zh(str(g.title or "").strip(), 20)

        task_display: dict[str, str] = {}
        for t in tasks:
            task_display[t.public_id] = _truncate_zh(str(t.title or "").strip(), 20)

        # 选中态（用于右侧详情栏默认展示）
        sel_goal_id = request.query_params.get("goal")
        sel_task_pid = request.query_params.get("task")
        selected_goal = None
        selected_task = None
        if sel_goal_id:
            try:
                selected_goal = s.get(Goal, int(sel_goal_id))
            except Exception:
                selected_goal = None
        if sel_task_pid:
            selected_task = (
                s.query(Task).filter(Task.public_id == sel_task_pid).one_or_none()
            )

    default_due = dt.date.today() + dt.timedelta(days=1)
    return templates.TemplateResponse(
        request,
        "goals.html",
        {
            "goals": goals,
            "tasks_by_goal": tasks_by_goal,
            "agent_spaces_by_task": agent_spaces_by_task,
            "task_meta": task_meta,
            "goal_display": goal_display,
            "task_display": task_display,
            "task_events": task_events,
            "goal_events": goal_events,
            "now": _utcnow(),
            "today": today,
            "selected_goal": selected_goal,
            "selected_task": selected_task,
            "default_due": default_due.isoformat(),
            "goal_filter": goal_filter,
            "goal_sort": goal_sort,
        },
    )


def _score_text_to_weight(v: str | None) -> int:
    x = (v or "").strip().lower()
    if x in {"p0", "urgent", "highest", "high"}:
        return 3
    if x in {"p1", "medium", "normal"}:
        return 2
    if x in {"p2", "low"}:
        return 1
    return 2


_NEXT_MOVE_TASK_TYPE_LABELS = {
    "deep_work": "Deep Work",
    "communication": "Communication",
    "review": "Review",
    "execution": "Execution",
    "admin": "Admin",
}


def _next_move_goal_label(goal: Goal) -> str:
    return _truncate_zh(str(goal.title or "").strip(), 20)


def _next_move_task_type_label(task_type: str | None) -> str:
    return _NEXT_MOVE_TASK_TYPE_LABELS.get(
        str(task_type or "").strip().lower(), "Execution"
    )


_infer_task_type = goal_service.infer_task_type
_infer_estimated_minutes = goal_service.infer_estimated_minutes
_infer_context_key = goal_service.infer_context_key


def _next_move_memory_context() -> dict:
    daily_text = ""
    try:
        daily_files = sorted(_memory_daily_root().glob("*.md"), reverse=True)
        if daily_files:
            daily_text = _memory_read_text(daily_files[0])
    except Exception:
        daily_text = ""
    long_term_text = _memory_read_text(_memory_long_term_path())
    merged = f"{daily_text}\n{long_term_text}".lower()
    signals: set[str] = set()
    if any(
        k in merged
        for k in [
            "fast feedback",
            "quick feedback",
            "快速反馈",
            "短反馈",
            "short task",
            "short tasks",
        ]
    ):
        signals.add("fast_feedback")
    if any(
        k in merged
        for k in [
            "avoid context switch",
            "reduce context switch",
            "减少切换",
            "连续推进",
            "保持上下文",
            "stay in the same context",
        ]
    ):
        signals.add("avoid_context_switch")
    if any(
        k in merged for k in ["deep work", "深度工作", "long focus block", "大块时间"]
    ):
        signals.add("deep_work")
    if any(
        k in merged
        for k in ["review first", "先 review", "prefer review", "喜欢 review"]
    ):
        signals.add("review")
    return {
        "daily": daily_text[:4000],
        "long_term": long_term_text[:4000],
        "signals": sorted(signals),
    }


def _next_move_feedback_meta(raw: str | None) -> dict:
    text_value = str(raw or "").strip()
    if not text_value:
        return {}
    try:
        data = json.loads(text_value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _next_move_feedback_penalty(
    feedback_rows: list[NextMoveFeedback],
    *,
    task_public_id: str,
    task_type: str,
    estimated_minutes: int,
    goal_id: int,
    continuity_score: float,
    now: dt.datetime,
) -> tuple[float, list[str]]:
    penalty = 0.0
    reasons: list[str] = []
    for fb in feedback_rows:
        created_at = getattr(fb, "created_at", None) or now
        if getattr(created_at, "tzinfo", None) is None:
            created_at = created_at.replace(tzinfo=dt.timezone.utc)
        if getattr(now, "tzinfo", None) is None:
            now = now.replace(tzinfo=dt.timezone.utc)
        age_hours = max(0.0, (now - created_at).total_seconds() / 3600.0)
        if age_hours > 24 * 14:
            continue
        freshness = (
            1.0
            if age_hours <= 24
            else (0.7 if age_hours <= 72 else (0.4 if age_hours <= 24 * 7 else 0.2))
        )
        meta = _next_move_feedback_meta(getattr(fb, "learned_summary", ""))
        reason_code = (
            str(getattr(fb, "reason_code", "") or meta.get("reason_code") or "")
            .strip()
            .lower()
        )
        meta_task_type = str(meta.get("task_type") or "").strip().lower()
        try:
            meta_goal_id = int(meta.get("goal_id") or 0)
        except Exception:
            meta_goal_id = 0

        if str(getattr(fb, "task_public_id", "") or "") == task_public_id:
            penalty += 8.0 * freshness
            reasons.append("you recently said not now for this task")
        if reason_code == "too_long" and estimated_minutes >= max(
            45, int(meta.get("estimated_minutes") or 45)
        ):
            penalty += 2.5 * freshness
            reasons.append("recent feedback prefers a shorter block")
        if (
            reason_code == "wrong_type"
            and meta_task_type
            and meta_task_type == task_type
        ):
            penalty += 2.2 * freshness
            reasons.append("recent feedback deprioritized this task type")
        if (
            reason_code in {"too_much_context_switch", "lacking_context"}
            and continuity_score < 1.0
        ):
            penalty += 2.0 * freshness
            reasons.append("recent feedback asked for less context switching")
        if (
            reason_code == "not_important_now"
            and meta_goal_id
            and meta_goal_id == goal_id
        ):
            penalty += 1.6 * freshness
            reasons.append("this goal was recently deprioritized")
    deduped: list[str] = []
    for item in reasons:
        if item not in deduped:
            deduped.append(item)
    return penalty, deduped[:2]


def _next_move_confidence(score: float) -> str:
    if score >= 19:
        return "high"
    if score >= 13:
        return "medium"
    return "low"


def _next_move_sentence(items: list[dict]) -> str | None:
    if not items:
        return None
    titles = [
        str((it.get("title") or "")).strip()
        for it in items[:3]
        if str((it.get("title") or "")).strip()
    ]
    if not titles:
        return None
    return "Top picks now: " + ", ".join(titles) + "."


def _next_move_learning_note(
    *,
    task_title: str,
    task_type: str,
    reason_code: str,
    reason_text: str,
    estimated_minutes: int,
) -> str:
    type_label = _next_move_task_type_label(task_type)
    reason_map = {
        "too_much_context_switch": "user wants less context switching",
        "too_long": "user wants a shorter task block right now",
        "wrong_type": "this work type does not fit the current mode",
        "not_important_now": "this task is not important right now",
        "lacking_context": "user needs more context first",
        "waiting_on_someone": "the task is blocked on someone else",
    }
    reason_label = reason_map.get(reason_code, "the recommendation was dismissed")
    note = f"- Next Move feedback: `{task_title}` ({type_label}, ~{estimated_minutes}m) was dismissed because {reason_label}."
    if reason_text:
        note += f" Note: {reason_text.strip()}"
    return note


def _next_move_persist_feedback_learning(
    *, note: str, memory_note: str | None = None
) -> None:
    memory_service.persist_feedback_learning(note=note, memory_note=memory_note)


@app.get("/api/recommendations/next")
def recommendations_next(
    limit: int = 3, trigger: str = "manual_refresh"
) -> JSONResponse:
    now = _utcnow()
    today = now.date()
    limit = 3

    with session_scope() as s:
        memory_context = _next_move_memory_context()
        goal_rows = (
            s.query(Goal)
            .filter(Goal.status.notin_(["done", "archived", "paused"]))
            .order_by(Goal.due_date.asc(), Goal.id.desc())
            .all()
        )
        goal_by_id = {g.id: g for g in goal_rows}
        tasks = (
            s.query(Task)
            .filter(
                Task.goal_id.in_(list(goal_by_id.keys())) if goal_by_id else text("1=0")
            )
            .filter(Task.status.in_(["todo", "in_progress", "blocked"]))
            .order_by(Task.id.asc())
            .all()
        )
        public_ids = [t.public_id for t in tasks]
        spaces_by_task: dict[str, AgentSpace] = {}
        if public_ids:
            for space in (
                s.query(AgentSpace)
                .filter(AgentSpace.task_public_id.in_(public_ids))
                .all()
            ):
                spaces_by_task[space.task_public_id] = space

        latest_event_by_task: dict[str, Event] = {}
        recent_focus_task_id = ""
        if public_ids:
            evs = (
                s.query(Event)
                .filter(Event.task_id.in_(public_ids))
                .order_by(Event.id.desc())
                .all()
            )
            for ev in evs:
                if ev.task_id and ev.task_id not in latest_event_by_task:
                    latest_event_by_task[ev.task_id] = ev
                if (
                    not recent_focus_task_id
                    and ev.task_id
                    and ev.kind in {"task.started", "task.progress"}
                ):
                    recent_focus_task_id = str(ev.task_id)

        feedback_rows = (
            s.query(NextMoveFeedback)
            .order_by(NextMoveFeedback.id.desc())
            .limit(120)
            .all()
        )

        memory_signals = set(memory_context.get("signals") or [])
        recent_focus_task = next(
            (t for t in tasks if t.public_id == recent_focus_task_id), None
        )
        recent_focus_goal_id = (
            int(recent_focus_task.goal_id) if recent_focus_task is not None else 0
        )
        recent_focus_type = (
            str(getattr(recent_focus_task, "task_type", "") or "").strip().lower()
            if recent_focus_task is not None
            else ""
        )
        recent_focus_context = ""
        if recent_focus_task is not None:
            recent_focus_context = str(
                getattr(recent_focus_task, "context_key", "") or ""
            ).strip()
            if not recent_focus_context:
                recent_focus_context = _infer_context_key(
                    str(recent_focus_task.title or ""),
                    str(recent_focus_task.content or ""),
                    goal_id=int(recent_focus_task.goal_id),
                    root_path=getattr(
                        spaces_by_task.get(recent_focus_task.public_id),
                        "root_path",
                        None,
                    ),
                )

        scored: list[tuple[float, dict]] = []
        for t in tasks:
            g = goal_by_id.get(t.goal_id)
            if g is None:
                continue

            space = spaces_by_task.get(t.public_id)
            task_type = str(
                getattr(t, "task_type", "") or ""
            ).strip().lower() or _infer_task_type(t.title, t.content)
            estimated_minutes = int(
                getattr(t, "estimated_minutes", 0) or 0
            ) or _infer_estimated_minutes(task_type, t.title, t.content)
            context_key = str(
                getattr(t, "context_key", "") or ""
            ).strip() or _infer_context_key(
                t.title,
                t.content,
                goal_id=int(t.goal_id),
                root_path=getattr(space, "root_path", None),
            )

            days_left = (g.due_date - today).days
            urgency = (
                6.5
                if days_left <= 0
                else (
                    5.2
                    if days_left <= 1
                    else (4.1 if days_left <= 3 else (2.8 if days_left <= 7 else 1.0))
                )
            )

            pri = _score_text_to_weight(g.priority)
            imp = _score_text_to_weight(g.importance)

            ev = latest_event_by_task.get(t.public_id)
            in_progress = (t.status == "in_progress") or (
                ev is not None and ev.kind in {"task.started", "task.progress"}
            )
            continuity_score = 0.0
            if recent_focus_task_id and recent_focus_task_id == t.public_id:
                continuity_score = 3.0
            elif recent_focus_context and recent_focus_context == context_key:
                continuity_score = 2.4
            elif recent_focus_goal_id and recent_focus_goal_id == int(g.id):
                continuity_score = 1.6
            elif recent_focus_type and recent_focus_type == task_type:
                continuity_score = 0.8
            if in_progress:
                continuity_score += 1.2

            hour = now.astimezone().hour
            if estimated_minutes <= 30:
                time_fit = 2.0 if hour < 10 or hour >= 18 else 1.0
            elif estimated_minutes >= 90:
                time_fit = 1.2 if 10 <= hour <= 16 else -1.0
            else:
                time_fit = 0.8

            memory_bonus = 0.0
            memory_notes: list[str] = []
            if "fast_feedback" in memory_signals:
                if estimated_minutes <= 30:
                    memory_bonus += 2.0
                    memory_notes.append("matches your fast-feedback preference")
                elif estimated_minutes >= 90:
                    memory_bonus -= 1.0
            if "avoid_context_switch" in memory_signals:
                if continuity_score >= 1.6:
                    memory_bonus += 2.0
                    memory_notes.append("keeps the current context warm")
                else:
                    memory_bonus -= 1.0
            if "deep_work" in memory_signals and task_type == "deep_work":
                memory_bonus += 1.4
                memory_notes.append("matches your deep-work preference")
            if "review" in memory_signals and task_type == "review":
                memory_bonus += 1.2
                memory_notes.append("fits your review-first preference")

            feedback_penalty, feedback_notes = _next_move_feedback_penalty(
                feedback_rows,
                task_public_id=t.public_id,
                task_type=task_type,
                estimated_minutes=estimated_minutes,
                goal_id=int(g.id),
                continuity_score=continuity_score,
                now=now,
            )

            score = (
                urgency * 2.7
                + pri * 2.0
                + imp * 2.2
                + continuity_score
                + time_fit
                + memory_bonus
                - feedback_penalty
            )

            context_switch_cost = (
                "low"
                if continuity_score >= 2.0
                else ("medium" if continuity_score >= 0.8 else "high")
            )
            why: list[str] = []
            if days_left <= 0:
                why.append("Deadline pressure is high.")
            elif days_left <= 3:
                why.append(f"Deadline is close ({days_left}d).")
            else:
                why.append(f"Goal DDL: {g.due_date.isoformat()}.")
            if continuity_score >= 2.0:
                why.append("Keeps your current context active.")
            elif estimated_minutes <= 30:
                why.append(f"Short block: about {estimated_minutes}m.")
            else:
                why.append(f"Estimated effort: about {estimated_minutes}m.")
            if memory_notes:
                why.append(memory_notes[0].capitalize() + ".")
            elif imp >= 3 or pri >= 3:
                why.append(f"High goal weight: {g.importance}/{g.priority}.")
            elif feedback_notes:
                why.append(
                    "Kept lower because of recent feedback, but still relevant now."
                )

            scored.append(
                (
                    score,
                    {
                        "type": "do_task",
                        "target": {"goal_id": g.id, "task_public_id": t.public_id},
                        "goal_title": _next_move_goal_label(g),
                        "title": t.title,
                        "task_type": task_type,
                        "task_type_label": _next_move_task_type_label(task_type),
                        "why": why[:3],
                        "expected_time_minutes": estimated_minutes,
                        "context_switch_cost": context_switch_cost,
                        "confidence": _next_move_confidence(score),
                        "debug": {"score": round(score, 2)},
                    },
                )
            )

        scored.sort(key=lambda x: x[0], reverse=True)
        items = [it for _s, it in scored[:limit]]
        run = NextMoveRun(
            trigger_kind=str(trigger or "manual_refresh")[:64],
            context_summary={
                "candidate_count": len(tasks),
                "recent_focus_task_public_id": recent_focus_task_id or None,
                "memory_signals": sorted(memory_signals),
                "feedback_count": len(feedback_rows),
            },
            recommendations={"items": items},
        )
        s.add(run)
        s.flush()
        run_id = int(run.id or 0)

    sentence = _next_move_sentence(items)
    item = items[0] if items else None

    return JSONResponse(
        {
            "generated_at": now.isoformat(),
            "run_id": run_id,
            "item": item,
            "items": items,
            "sentence": sentence,
        },
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@app.post("/api/recommendations/feedback")
def recommendations_feedback(payload: dict) -> JSONResponse:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")

    task_public_id = str(payload.get("task_public_id") or "").strip()
    if not task_public_id:
        raise HTTPException(status_code=400, detail="task_public_id is required")

    feedback_type = (
        str(payload.get("feedback_type") or "dismiss").strip().lower() or "dismiss"
    )
    reason_code = str(payload.get("reason_code") or "").strip().lower()
    reason_text = str(payload.get("reason_text") or "").strip()
    try:
        run_id = int(payload.get("run_id") or 0) or None
    except Exception:
        run_id = None

    with session_scope() as s:
        task = s.query(Task).filter(Task.public_id == task_public_id).one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        space = (
            s.query(AgentSpace)
            .filter(AgentSpace.task_public_id == task_public_id)
            .one_or_none()
        )

        task_type = str(
            getattr(task, "task_type", "") or ""
        ).strip().lower() or _infer_task_type(task.title, task.content)
        estimated_minutes = int(
            getattr(task, "estimated_minutes", 0) or 0
        ) or _infer_estimated_minutes(task_type, task.title, task.content)
        context_key = str(
            getattr(task, "context_key", "") or ""
        ).strip() or _infer_context_key(
            task.title,
            task.content,
            goal_id=int(task.goal_id),
            root_path=getattr(space, "root_path", None),
        )
        learned_summary = json.dumps(
            {
                "feedback_type": feedback_type,
                "reason_code": reason_code,
                "task_type": task_type,
                "estimated_minutes": estimated_minutes,
                "context_key": context_key,
                "goal_id": int(task.goal_id),
            },
            ensure_ascii=False,
        )
        row = NextMoveFeedback(
            run_id=run_id,
            task_public_id=task_public_id,
            feedback_type=feedback_type,
            reason_code=reason_code,
            reason_text=reason_text[:2000],
            learned_summary=learned_summary,
        )
        s.add(row)
        s.flush()
        feedback_id = int(row.id or 0)
        similar_rows = (
            s.query(NextMoveFeedback)
            .filter(NextMoveFeedback.feedback_type == feedback_type)
            .filter(NextMoveFeedback.reason_code == reason_code)
            .order_by(NextMoveFeedback.id.desc())
            .limit(50)
            .all()
        )

    daily_note = _next_move_learning_note(
        task_title=str(task.title or task_public_id),
        task_type=task_type,
        reason_code=reason_code,
        reason_text=reason_text,
        estimated_minutes=estimated_minutes,
    )
    memory_note = None
    if (
        reason_code
        and sum(
            1
            for row in similar_rows
            if _next_move_feedback_meta(getattr(row, "learned_summary", "")).get(
                "task_type"
            )
            == task_type
        )
        >= 2
    ):
        if reason_code == "too_long":
            memory_note = f"- Prefer shorter tasks over ~{estimated_minutes}m when dismissing {_next_move_task_type_label(task_type)} work."
        elif reason_code == "too_much_context_switch":
            memory_note = f"- Prefer recommendations that continue the current context before suggesting new {_next_move_task_type_label(task_type)} work."
        elif reason_code == "wrong_type":
            memory_note = f"- Avoid prioritizing {_next_move_task_type_label(task_type)} tasks when the user says the work type is wrong for now."
        elif reason_code == "not_important_now":
            memory_note = "- When the user dismisses a recommendation as not important now, reduce near-term priority for similar work."
    _next_move_persist_feedback_learning(note=daily_note, memory_note=memory_note)

    _try_audit_memory(
        kind="next_move.feedback",
        source="web",
        summary=f"Next Move feedback for task: {task_public_id}",
        detail=f"Feedback type: {feedback_type}\nReason code: {reason_code or '-'}\nReason text:\n\n{reason_text or '-'}",
        goal_id=int(task.goal_id),
        task_public_id=task_public_id,
        metadata={
            "run_id": run_id,
            "reason_code": reason_code,
            "learned_summary": learned_summary,
        },
    )
    return JSONResponse(
        {"ok": True, "feedback_id": feedback_id, "task_public_id": task_public_id}
    )


@app.get("/goals/new", response_class=HTMLResponse)
def goals_new(request: Request) -> HTMLResponse:
    # 兼容旧入口：直接跳到目标页
    return RedirectResponse(url="/goals", status_code=302)


@app.post("/goals", include_in_schema=False)
async def goals_create(
    title: str = Form(..., min_length=1, max_length=2000),
    content: str = Form(..., min_length=1, max_length=4000),
    due_date: str = Form(...),
) -> RedirectResponse:
    parsed_due = dt.date.fromisoformat(due_date)
    with session_scope() as s:
        goal = goal_service.create_goal(
            s,
            title=title,
            content=content,
            due_date=parsed_due,
            agent="ui",
            source="web",
        )
        created_goal_id = int(goal.id or 0)
    return RedirectResponse(
        url=f"/goals?goal={created_goal_id}&tab=tasks", status_code=303
    )


@app.post("/goals/{goal_id:int}/tasks", include_in_schema=False)
def tasks_create(
    goal_id: int,
    title: str = Form(..., min_length=1, max_length=512),
    content: str = Form(..., min_length=1, max_length=4000),
) -> RedirectResponse:
    with session_scope() as s:
        try:
            goal_service.create_task(
                s,
                goal_id=int(goal_id),
                title=title,
                content=content,
                agent="ui",
                source="web",
            )
        except goal_service.GoalTaskNotFound:
            raise HTTPException(status_code=404, detail="Goal not found")
    return RedirectResponse(url=f"/goals?goal={goal_id}&tab=tasks", status_code=303)


@app.post("/goals/{goal_id:int}/done", include_in_schema=False)
def goals_mark_done(goal_id: int) -> RedirectResponse:
    """将 Goal 标记为已完成（人工行为）。"""

    with session_scope() as s:
        try:
            goal_service.mark_goal_done(s, goal_id=int(goal_id))
        except goal_service.GoalTaskNotFound:
            raise HTTPException(status_code=404, detail="Goal not found")
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/goals/{goal_id:int}/reopen", include_in_schema=False)
def goals_reopen(goal_id: int) -> RedirectResponse:
    """将已完成的 Goal 重新打开（人工行为）。"""

    with session_scope() as s:
        try:
            goal_service.reopen_goal(s, goal_id=int(goal_id))
        except goal_service.GoalTaskNotFound:
            raise HTTPException(status_code=404, detail="Goal not found")
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/tasks/{task_id:int}/done", include_in_schema=False)
def tasks_mark_done(task_id: int) -> RedirectResponse:
    with session_scope() as s:
        try:
            result = goal_service.mark_task_done(s, task_id=int(task_id))
        except goal_service.GoalTaskNotFound:
            raise HTTPException(status_code=404, detail="Task not found")
        goal_id = result.goal_id
        task_public_id = result.task_public_id

    # 完成任务时自动释放 AgentSpace（若存在）。
    # 注意：这里是 best-effort；释放失败不应阻断“完成”本身。
    try:
        asyncio.run(delete_agent_space(task_public_id))
    except RuntimeError:
        # 兼容：极少数情况下当前线程已有 event loop。
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(delete_agent_space(task_public_id))
        finally:
            try:
                loop.close()
            except Exception:
                pass
    except Exception:
        pass
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/tasks/{task_id:int}/reopen", include_in_schema=False)
def tasks_reopen(task_id: int) -> RedirectResponse:
    """将已完成任务重新打开（人工行为）。"""

    with session_scope() as s:
        try:
            result = goal_service.reopen_task(s, task_id=int(task_id))
        except goal_service.GoalTaskNotFound:
            raise HTTPException(status_code=404, detail="Task not found")
        goal_id = result.goal_id
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/tasks/{task_id:int}/edit", include_in_schema=False)
def tasks_update(
    task_id: int,
    title: str = Form(..., min_length=1, max_length=512),
    content: str = Form(..., min_length=1, max_length=4000),
) -> RedirectResponse:
    with session_scope() as s:
        try:
            result = goal_service.update_task(
                s, task_id=int(task_id), title=title, content=content
            )
        except goal_service.GoalTaskNotFound:
            raise HTTPException(status_code=404, detail="Task not found")
    # 保持 Dashboard 选中态
    return RedirectResponse(
        url=f"/goals?task={result.task_public_id}&goal={result.goal_id}",
        status_code=303,
    )


@app.post("/tasks/{task_id:int}/delete", include_in_schema=False)
def tasks_delete(task_id: int) -> RedirectResponse:
    with session_scope() as s:
        try:
            result = goal_service.delete_task(s, task_id=int(task_id))
        except goal_service.GoalTaskNotFound:
            raise HTTPException(status_code=404, detail="Task not found")
    return RedirectResponse(url=f"/goals?goal={result.goal_id}", status_code=303)


@app.post("/goals/{goal_id:int}/edit", include_in_schema=False)
def goals_update(
    goal_id: int,
    title: str = Form(..., min_length=1, max_length=2000),
    content: str = Form(..., min_length=1, max_length=4000),
    due_date: str = Form(...),
    status: str = Form("active", max_length=32),
    priority: str = Form("normal", max_length=32),
    importance: str = Form("normal", max_length=32),
) -> RedirectResponse:
    parsed_due = dt.date.fromisoformat(due_date)
    with session_scope() as s:
        try:
            goal_service.update_goal(
                s,
                goal_id=int(goal_id),
                title=title,
                content=content,
                due_date=parsed_due,
                status=status,
                priority=priority,
                importance=importance,
            )
        except goal_service.GoalTaskNotFound:
            raise HTTPException(status_code=404, detail="Goal not found")
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/goals/{goal_id:int}/delete", include_in_schema=False)
def goals_delete(goal_id: int) -> RedirectResponse:
    with session_scope() as s:
        try:
            goal_service.delete_goal(s, goal_id=int(goal_id))
        except goal_service.GoalTaskNotFound:
            raise HTTPException(status_code=404, detail="Goal not found")
    return RedirectResponse(url="/goals", status_code=303)


@app.get("/api/inspirations")
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
                s.query(InspirationPublishRecord.space_id, InspirationPublishRecord.id)
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
            _inspiration_space_payload(
                space,
                latest_draft=latest_drafts.get(int(space.id)),
                resource_count=resource_counts.get(int(space.id), 0),
                draft_count=draft_counts.get(int(space.id), 0),
                publish_count=publish_counts.get(int(space.id), 0),
            )
            for space in spaces
        ],
    }


@app.post("/api/inspirations")
def inspirations_create_api(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    title = str(payload.get("title") or "").strip()
    mode = (
        str(payload.get("mode") or payload.get("surface") or "built_in").strip().lower()
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
        title = _truncate_zh(initial_message.replace("\n", " "), 40) or "Inspiration"
    title = title[:512]

    space_id = 0
    created_payload: dict | None = None
    with session_scope() as s:
        now = _utcnow()
        space = InspirationSpace(
            title=title,
            status="open",
            mode=mode,
            last_activity_at=now,
        )
        s.add(space)
        s.flush()
        space_id = int(space.id)
        workspace = _inspiration_workspace_path(space, space_id)
        space.workspace_path = str(workspace)
        _inspiration_create_initial_note_resource(
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
            provider, _err = _get_llm_provider_or_error()
            messages = (
                s.query(InspirationMessage)
                .filter(InspirationMessage.space_id == space_id)
                .order_by(InspirationMessage.id.asc())
                .all()
            )
            resources = _inspiration_non_deleted_resources(s, space_id)
            if provider is None:
                reply = _inspiration_fallback_reply(space, initial_message)
            else:
                try:
                    reply = _inspiration_llm_reply(
                        provider,
                        space=space,
                        messages=messages,
                        resources=resources,
                    )
                except Exception:
                    reply = _inspiration_fallback_reply(space, initial_message)
            s.add(
                InspirationMessage(
                    space_id=space_id,
                    role="assistant",
                    kind="message",
                    content=reply,
                )
            )
        created_payload = _inspiration_space_payload(space, resource_count=1)
    _try_audit_memory(
        kind="inspiration.space_created",
        source="web",
        summary=f"Created inspiration space {space_id}.",
        detail=initial_message or title,
        metadata={"space_id": space_id, "title": title, "mode": mode},
    )
    if initial_message:
        _inspiration_maybe_emit_phase_summary(space_id)
    return {"ok": True, "item": created_payload}


@app.get("/api/inspirations/{space_id:int}")
def inspirations_get_api(
    space_id: int, before_id: int | None = None, page_size: int = 60
) -> dict:
    page_size = max(1, min(int(page_size or 60), 200))
    with session_scope() as s:
        space = _inspiration_space_or_404(s, space_id)
        is_waiting = _inspiration_is_waiting(s, int(space_id))
        messages, next_before = _inspiration_messages_page(
            s,
            int(space_id),
            before_id=before_id,
            page_size=page_size,
        )
        resources = _inspiration_non_deleted_resources(s, int(space_id))
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
        item = _inspiration_space_payload(
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
            "is_publishing": _inspiration_is_publishing(space),
            "messages": [_inspiration_message_payload(msg) for msg in messages],
            "next_before_id": next_before,
            "resources": [
                _inspiration_resource_payload(int(space_id), res, include_text=True)
                for res in resources
            ],
            "drafts": [_inspiration_draft_payload(draft) for draft in drafts],
            "publish_records": [
                _inspiration_publish_record_payload(record) for record in records
            ],
        }


@app.post("/api/inspirations/{space_id:int}/messages")
async def inspiration_message_create_api(space_id: int, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    content = str(payload.get("content") or "").strip()
    return await _inspiration_enqueue_turn(int(space_id), content)


@app.post("/api/inspirations/{space_id:int}/close")
async def inspiration_close_api(space_id: int) -> dict:
    with session_scope() as s:
        space = _inspiration_space_or_404(s, space_id)
        if str(space.status or "open") == "published":
            raise HTTPException(
                status_code=400, detail="Published spaces cannot be closed"
            )
        if str(space.status or "open") != "open":
            raise HTTPException(
                status_code=400, detail="Only open spaces can be closed"
            )
        now = _utcnow()
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
        payload = _inspiration_space_payload(space)
    await _inspiration_release_terminals(int(space_id))
    _try_audit_memory(
        kind="inspiration.closed",
        source="web",
        summary=f"Closed inspiration space {int(space_id)}.",
        detail="User closed the inspiration space.",
        metadata={"space_id": int(space_id)},
    )
    return {"ok": True, "item": payload}


@app.post("/api/inspirations/{space_id:int}/reopen")
def inspiration_reopen_api(space_id: int) -> dict:
    with session_scope() as s:
        space = _inspiration_space_or_404(s, space_id)
        if str(space.status or "open") != "closed":
            raise HTTPException(
                status_code=400, detail="Only closed spaces can be reopened"
            )
        now = _utcnow()
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
        payload = _inspiration_space_payload(space)
    _try_audit_memory(
        kind="inspiration.reopened",
        source="web",
        summary=f"Reopened inspiration space {int(space_id)}.",
        detail="User reopened the inspiration space.",
        metadata={"space_id": int(space_id)},
    )
    return {"ok": True, "item": payload}


@app.delete("/api/inspirations/{space_id:int}")
def inspiration_delete_api(space_id: int) -> dict:
    removed_files_dir = str(_inspiration_space_files_dir(int(space_id)))
    with session_scope() as s:
        space = _inspiration_space_or_404(s, space_id)
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
    _try_audit_memory(
        kind="inspiration.deleted",
        source="web",
        summary=f"Deleted inspiration space {int(space_id)}.",
        detail="User deleted the inspiration space before publication.",
        metadata={"space_id": int(space_id)},
    )
    return {"ok": True, "space_id": int(space_id)}


@app.post("/api/inspirations/{space_id:int}/resources")
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
        space = _inspiration_space_or_404(s, space_id)
        if str(space.status or "open") != "open":
            raise HTTPException(
                status_code=400, detail="Only open spaces accept new resources"
            )
        seq_id = _inspiration_next_resource_seq(s, int(space_id))
        resource_name = str(name or "").strip()
        now = _utcnow()
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
                raise HTTPException(status_code=400, detail="url_content is required")
            resource.url_content = url_text[:4000]
            resource.source = "user"
            if not resource_name:
                resource.name = url_text[:512]
        elif normalized_type in {"text", "summary"}:
            body = str(text_content or "").strip()
            if not body:
                raise HTTPException(status_code=400, detail="text_content is required")
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
            ) = await _inspiration_store_uploaded_resource_file(
                space_id=int(space_id),
                seq_id=int(seq_id),
                file=file,
            )
            resource.file_path = str(target_path)
            try:
                resource.external_path = str(
                    target_path.relative_to(
                        _inspiration_workspace_path(space, int(space_id))
                    )
                )
            except Exception:
                resource.external_path = str(target_path)
            resource.source = "user"
            resource.name = resource_name or uploaded_name
        s.add(resource)
        if normalized_type in {"url", "text", "summary"}:
            _inspiration_write_resource_file(resource, space)
        space.last_activity_at = now
        s.flush()
        payload = _inspiration_resource_payload(
            int(space_id), resource, include_text=True
        )
    _try_audit_memory(
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


@app.patch("/api/inspirations/{space_id:int}/resources/{resource_id:int}")
def inspiration_resource_update_api(
    space_id: int, resource_id: int, payload: dict
) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    with session_scope() as s:
        space = _inspiration_space_or_404(s, space_id)
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
                raise HTTPException(status_code=400, detail="url_content is required")
            resource.url_content = url_text[:4000]
        if (
            str(resource.type or "") in {"text", "summary"}
            and "text_content" in payload
        ):
            body = str(payload.get("text_content") or "").strip()
            if not body:
                raise HTTPException(status_code=400, detail="text_content is required")
            resource.text_content = body[:20000]
        if str(resource.type or "") in {"url", "text", "summary"}:
            _inspiration_write_resource_file(resource, space)
        space.last_activity_at = _utcnow()
        payload_out = _inspiration_resource_payload(
            int(space_id), resource, include_text=True
        )
    return {"ok": True, "item": payload_out}


@app.post("/api/inspirations/{space_id:int}/resources/{resource_id:int}/replace")
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
        space = _inspiration_space_or_404(s, space_id)
        if str(space.status or "open") != "open":
            raise HTTPException(
                status_code=400, detail="Only open spaces can replace resource files"
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
        new_path_obj, uploaded_name = await _inspiration_store_uploaded_resource_file(
            space_id=int(space_id),
            seq_id=int(resource.resource_seq_id or 0),
            file=file,
        )
        resource.file_path = str(new_path_obj)
        try:
            resource.external_path = str(
                new_path_obj.relative_to(
                    _inspiration_workspace_path(space, int(space_id))
                )
            )
        except Exception:
            resource.external_path = str(new_path_obj)
        next_name = str(name or "").strip()
        if next_name:
            resource.name = next_name[:512]
        elif not str(resource.name or "").strip():
            resource.name = uploaded_name
        space.last_activity_at = _utcnow()
        payload_out = _inspiration_resource_payload(
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


@app.delete("/api/inspirations/{space_id:int}/resources/{resource_id:int}")
def inspiration_resource_delete_api(space_id: int, resource_id: int) -> dict:
    with session_scope() as s:
        space = _inspiration_space_or_404(s, space_id)
        if str(space.status or "open") != "open":
            raise HTTPException(
                status_code=400, detail="Only open spaces can delete resources"
            )
        resource = s.get(InspirationResource, int(resource_id))
        if resource is None or int(resource.space_id) != int(space_id):
            raise HTTPException(status_code=404, detail="Resource not found")
        if resource.deleted_at is not None:
            return {"ok": True, "resource_id": int(resource_id)}
        resource.deleted_at = _utcnow()
        space.last_activity_at = _utcnow()
    return {"ok": True, "resource_id": int(resource_id)}


@app.get("/api/inspirations/{space_id:int}/resources/{resource_id:int}/raw")
def inspiration_resource_raw_api(space_id: int, resource_id: int) -> FileResponse:
    with session_scope() as s:
        _inspiration_space_or_404(s, space_id)
        resource = s.get(InspirationResource, int(resource_id))
        if resource is None or int(resource.space_id) != int(space_id):
            raise HTTPException(status_code=404, detail="Resource not found")
        if resource.deleted_at is not None or not str(resource.file_path or "").strip():
            raise HTTPException(status_code=404, detail="File resource not found")
        file_path = Path(str(resource.file_path or "")).expanduser()
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="File resource not found")
        return FileResponse(
            path=str(file_path),
            media_type=_guess_media_type(file_path),
            filename=str(resource.name or file_path.name),
        )


@app.post("/api/inspirations/{space_id:int}/resources/sync")
def inspiration_resources_sync_api(space_id: int) -> dict:
    with session_scope() as s:
        space = _inspiration_space_or_404(s, int(space_id))
        if str(space.status or "open") == "published":
            raise HTTPException(
                status_code=400, detail="Published spaces are read-only"
            )
        items = _inspiration_sync_resources_dir(s, space)
        payloads = [
            _inspiration_resource_payload(int(space_id), item, include_text=True)
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
    _try_audit_memory(
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


@app.post("/api/inspirations/{space_id:int}/commands/summary_title")
async def inspiration_summary_title_api(space_id: int) -> dict:
    return await _inspiration_enqueue_turn(int(space_id), "/summary_title")


@app.post("/api/inspirations/{space_id:int}/drafts/generate")
async def inspiration_draft_generate_api(space_id: int) -> dict:
    return await _inspiration_enqueue_turn(int(space_id), "/plan")


@app.post("/api/inspirations/{space_id:int}/drafts/generate_from_draft_summary")
async def inspiration_draft_generate_from_draft_summary_api(space_id: int) -> dict:
    with session_scope() as s:
        space = _inspiration_space_or_404(s, int(space_id))
        item = _inspiration_sync_draft_summary_file(s, space)
        if item is None:
            raise HTTPException(status_code=400, detail="Summary is missing")
    return await _inspiration_enqueue_turn(int(space_id), "/plan")


@app.post("/api/inspirations/{space_id:int}/drafts/generate_from_resource")
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
        _inspiration_space_or_404(s, int(space_id))
        resource = (
            s.query(InspirationResource)
            .filter(InspirationResource.space_id == int(space_id))
            .filter(InspirationResource.id == int(resource_id))
            .filter(InspirationResource.deleted_at.is_(None))
            .one_or_none()
        )
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        resource_ref = _inspiration_resource_reference(resource)
    prompt = (
        "/plan\n"
        "Create a Goal and Tasks using this resource as the primary source. "
        "If it follows the OpenFocus bridge Markdown format, map the level-1 heading to the goal title, "
        "the content under it to the goal content, and each level-2 heading plus its body to one task.\n\n"
        f"{resource_ref}"
    )
    return await _inspiration_enqueue_turn(int(space_id), prompt)


@app.get("/api/inspirations/{space_id:int}/drafts")
def inspiration_drafts_list_api(space_id: int) -> dict:
    with session_scope() as s:
        _inspiration_space_or_404(s, space_id)
        drafts = (
            s.query(InspirationDraft)
            .filter(InspirationDraft.space_id == int(space_id))
            .order_by(InspirationDraft.version.desc(), InspirationDraft.id.desc())
            .all()
        )
    return {
        "ok": True,
        "items": [_inspiration_draft_payload(draft) for draft in drafts],
    }


@app.post("/api/inspirations/{space_id:int}/publish")
async def inspiration_publish_api(space_id: int, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    due_date_raw = str(payload.get("due_date") or "").strip()
    if due_date_raw:
        due_date = dt.date.fromisoformat(due_date_raw)
    else:
        due_date = dt.date.today() + dt.timedelta(days=7)
    draft_id = payload.get("draft_id")
    publish_info = _inspiration_prepare_publish(
        int(space_id),
        int(draft_id) if draft_id is not None else None,
        due_date,
    )
    asyncio.get_running_loop().create_task(
        _kickoff_inspiration_publish(
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


@app.post("/api/inspirations/{space_id:int}/fork")
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
        source_space = _inspiration_space_or_404(s, space_id)
        target_title = (
            title[:512]
            if title
            else _inspiration_default_followup_title(source_space.title)
        )
        now = _utcnow()
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
        forked.workspace_path = str(_inspiration_workspace_path(forked, new_space_id))

        resources = _inspiration_non_deleted_resources(s, int(space_id))
        seq_id = 1
        for resource in resources:
            if str(resource.type or "") == "summary":
                _inspiration_clone_resource(
                    s=s,
                    source=resource,
                    target_space_id=new_space_id,
                    seq_id=seq_id,
                )
                seq_id += 1
                continue
            if include_all_resources or int(resource.id) in selected_resource_ids:
                _inspiration_clone_resource(
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
        payload_out = _inspiration_space_payload(forked)
    _try_audit_memory(
        kind="inspiration.forked",
        source="web",
        summary=f"Forked inspiration space {int(space_id)} into {int(payload_out['id'])}.",
        detail=payload_out["title"],
        metadata={"space_id": int(space_id), "forked_space_id": int(payload_out["id"])},
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
        space = (
            _inspiration_space_or_404(s, int(space_id))
            if space_id is not None
            else None
        )
        is_waiting = (
            _inspiration_is_waiting(s, int(space_id)) if space is not None else False
        )
        is_publishing = _inspiration_is_publishing(space)
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
            resources = _inspiration_non_deleted_resources(s, int(space_id))
            owner_sid = _inspiration_terminal_space_id(int(space_id))
            terminals = (
                s.query(RemoteTerminalSession)
                .filter(RemoteTerminalSession.space_id == owner_sid)
                .filter(RemoteTerminalSession.status != "closed")
                .order_by(RemoteTerminalSession.id.asc())
                .all()
            )
            if terminals:
                inspiration_terminal = _inspiration_terminal_payload(
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
        "has_online_companion": _has_online_companion(),
        "draft_summary_prompt": _build_inspiration_draft_summary_prompt(space)
        if space
        else "",
        "published_goal": published_goal,
        "default_due": (dt.date.today() + dt.timedelta(days=7)).isoformat(),
    }


@app.get("/inspirations", response_class=HTMLResponse)
def inspirations_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "inspiration_detail.html",
        _inspiration_detail_page_context(None),
    )


@app.get("/inspirations/{space_id:int}", response_class=HTMLResponse)
def inspiration_detail_page(request: Request, space_id: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "inspiration_detail.html",
        _inspiration_detail_page_context(int(space_id)),
    )


def _memory_dir() -> Path:
    env = os.environ.get("OPENFOCUS_MEMORY_DIR")
    if env:
        p = Path(env).expanduser().resolve()
    else:
        p = (Path(__file__).resolve().parent.parent / ".data" / "memory").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


@app.get("/memory", response_class=HTMLResponse)
def memory_view(request: Request) -> HTMLResponse:
    _memory_maintenance()
    mem_dir = _memory_dir()
    state = _memory_load_state_unlocked()
    audit_files = _memory_collect_file_items(_memory_audit_root(), "**/*.md")
    daily_files = _memory_collect_file_items(_memory_daily_root(), "*.md")
    selected_tab = str(request.query_params.get("tab") or "audit").strip().lower()
    if selected_tab not in {"audit", "daily", "long_term"}:
        selected_tab = "audit"
    selected_audit = str(request.query_params.get("audit_file") or "").strip()
    selected_daily = str(request.query_params.get("daily_file") or "").strip()
    if not selected_audit and audit_files:
        selected_audit = str(audit_files[0].get("rel_path") or "")
    if not selected_daily and daily_files:
        selected_daily = str(daily_files[0].get("rel_path") or "")
    audit_content = _memory_read_selected_file(selected_audit)
    daily_content = _memory_read_selected_file(selected_daily)
    long_term_path = _memory_long_term_path()
    long_term_memory = _memory_read_text(long_term_path)
    if not long_term_memory:
        long_term_memory = _read_text(mem_dir / "user_memory.md")
    return templates.TemplateResponse(
        request,
        "memory.html",
        {
            "selected_tab": selected_tab,
            "audit_files": audit_files,
            "daily_files": daily_files,
            "selected_audit": selected_audit,
            "selected_daily": selected_daily,
            "audit_content": audit_content,
            "daily_content": daily_content,
            "long_term_memory": long_term_memory,
            "state": state,
        },
    )


@app.post("/memory/audit/summary", include_in_schema=False)
def memory_audit_summary() -> RedirectResponse:
    now = _utcnow()
    _memory_force_audit_summary(now)
    return RedirectResponse(url="/memory?tab=audit", status_code=303)


@app.get("/companions", response_class=HTMLResponse)
def companions_view(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "companions.html", {})


def _companion_display_status(c: Companion, *, now: dt.datetime | None = None) -> str:
    return str(companion_service.display_status(c, COMPANION_GRPC, now=now) or "")


@app.post("/api/companions/register")
def companion_register(payload: dict) -> dict:
    return companion_service.register_companion(payload)


@app.get("/api/companions")
def companions_list(limit: int = 50) -> dict:
    return companion_service.list_companions(COMPANION_GRPC, limit=limit)


@app.delete("/api/companions/{companion_id:int}")
def companion_delete(companion_id: int) -> dict:
    return companion_service.delete_companion(COMPANION_GRPC, companion_id)


@app.post("/api/companions/{companion_id:int}/pair")
async def companion_pair(companion_id: int, payload: dict) -> dict:
    return await companion_service.pair_companion(COMPANION_GRPC, companion_id, payload)


@app.post("/api/companions/{companion_id:int}/pairing_code")
async def companion_pairing_code(companion_id: int) -> dict:
    return await companion_service.request_pairing_code(COMPANION_GRPC, companion_id)


@app.post("/api/companions/{companion_id:int}/choose_directory")
async def companion_choose_directory_proxy(companion_id: int) -> dict:
    return await companion_service.choose_directory(COMPANION_GRPC, companion_id)


@app.post("/memory/save", include_in_schema=False)
def memory_save(
    long_term_memory: str = Form(""),
    user_memory: str = Form(""),
    user_card: str = Form(""),
) -> RedirectResponse:
    mem_dir = _memory_dir()
    if user_card:
        (mem_dir / "user_card.md").write_text(user_card or "", encoding="utf-8")
    content = long_term_memory if str(long_term_memory or "").strip() else user_memory
    _memory_long_term_path().write_text(content or "", encoding="utf-8")
    if user_memory:
        (mem_dir / "user_memory.md").write_text(user_memory or "", encoding="utf-8")
    return RedirectResponse(url="/memory?tab=long_term", status_code=303)


@app.post("/api/agent/events")
def agent_report_event(payload: AgentEventIn) -> dict:
    """Agent 上报任务进度/状态。

    每次调用都会落一条 event 到数据库，便于后续做历史、指标与推荐。
    """
    with session_scope() as s:
        ev = Event(
            kind=payload.kind,
            agent=payload.agent,
            task_id=payload.task_id,
            payload=payload.payload,
        )
        s.add(ev)

        s.flush()  # 获取自增 id
        event_id = ev.id
        created_at = ev.created_at
    _try_audit_memory(
        kind=f"event.{payload.kind}",
        source=f"agent:{payload.agent}",
        summary=f"Agent reported event `{payload.kind}`.",
        detail=json.dumps(payload.payload or {}, ensure_ascii=False, indent=2),
        task_public_id=payload.task_id,
        metadata={"event_id": event_id, "created_at": _memory_iso(created_at)},
        occurred_at=created_at,
    )
    return {"id": event_id, "created_at": created_at}


@app.get("/api/events/recent")
def recent_events(limit: int = 30) -> dict:
    """近期事件（用于 Dashboard 事件流）。"""

    limit = max(1, min(int(limit or 30), 200))
    with session_scope() as s:
        # 过滤噪声事件：Companion 的连接/断连不展示
        exclude = {"companion.connected", "companion.disconnected"}
        evs_raw = s.query(Event).order_by(Event.id.desc()).limit(limit * 3).all()
        evs = [ev for ev in evs_raw if (ev.kind or "") not in exclude][:limit]

        # 只对真实存在的任务提供“打开”能力，避免 UI 出现能点但打不开的事件。
        cand_task_ids = [ev.task_id for ev in evs if ev.task_id]
        existing_task_ids: set[str] = set()
        if cand_task_ids:
            existing_task_ids = {
                r[0]
                for r in s.query(Task.public_id)
                .filter(Task.public_id.in_(cand_task_ids))
                .all()
            }

    items: list[dict] = []
    for ev in evs:
        payload = ev.payload or {}
        task_public_id = (
            ev.task_id if (ev.task_id and ev.task_id in existing_task_ids) else None
        )
        items.append(
            {
                "id": ev.id,
                "kind": ev.kind,
                "kind_label": _event_kind_label(ev.kind, payload),
                "source_label": _event_source_label(ev.agent),
                "task_id": ev.task_id,
                "task_public_id": task_public_id,
                "created_at": ev.created_at.isoformat()
                if hasattr(ev.created_at, "isoformat")
                else str(ev.created_at),
                "summary": _event_summary(ev.kind, payload),
            }
        )
    return {"items": items}


@app.get("/api/calendar/month")
def calendar_month(ym: str | None = None) -> dict:
    """Calendar by month.

    - Grid view: show tasks completed per day (based on Task.completed_at)
    - Swimlane view: show goals timeline within the month (created_at -> due_date)
    """

    today = dt.date.today()
    raw = str(ym or "").strip()
    try:
        if raw:
            parts = raw.split("-")
            if len(parts) != 2:
                raise ValueError("ym must be YYYY-MM")
            y = int(parts[0])
            m = int(parts[1])
        else:
            y, m = int(today.year), int(today.month)
        if not (1 <= m <= 12):
            raise ValueError("month out of range")
        # Keep a reasonable bound to avoid accidental giant queries.
        if y < 1970 or y > 2100:
            raise ValueError("year out of range")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    month_start = dt.date(y, m, 1)
    if m == 12:
        month_end = dt.date(y + 1, 1, 1)
    else:
        month_end = dt.date(y, m + 1, 1)

    start_dt = dt.datetime(
        month_start.year, month_start.month, month_start.day, tzinfo=dt.timezone.utc
    )
    end_dt = dt.datetime(
        month_end.year, month_end.month, month_end.day, tzinfo=dt.timezone.utc
    )

    with session_scope() as s:
        done_tasks = (
            s.query(Task)
            .filter(Task.completed_at.isnot(None))
            .filter(Task.completed_at >= start_dt)
            .filter(Task.completed_at < end_dt)
            .all()
        )

        # Goals for swimlane + goal tasks view.
        goals = s.query(Goal).order_by(Goal.id.asc()).all()
        all_tasks = s.query(Task).order_by(Task.id.asc()).all()

    goal_by_id: dict[int, Goal] = {int(g.id): g for g in goals}
    tasks_by_goal: dict[int, list[Task]] = {}
    for t in all_tasks:
        tasks_by_goal.setdefault(int(t.goal_id), []).append(t)

    days: dict[str, list[dict]] = {}
    for t in done_tasks:
        if not t.completed_at:
            continue
        d = t.completed_at.astimezone(dt.timezone.utc).date().isoformat()
        g = goal_by_id.get(int(t.goal_id))
        days.setdefault(d, []).append(
            {
                "task_public_id": t.public_id,
                "task_title": t.title,
                "goal_id": int(t.goal_id),
                "goal_title": (g.title if g is not None else ""),
                "completed_at": t.completed_at.isoformat()
                if hasattr(t.completed_at, "isoformat")
                else str(t.completed_at),
            }
        )

    goals_out: list[dict] = []
    for g in goals:
        gid = int(g.id)
        ts = tasks_by_goal.get(gid, [])
        done_n = sum(1 for t in ts if (t.status or "").strip() == "done")
        goals_out.append(
            {
                "id": gid,
                "title": g.title,
                "status": g.status,
                "created_at": g.created_at.isoformat()
                if hasattr(g.created_at, "isoformat")
                else str(g.created_at),
                "due_date": g.due_date.isoformat()
                if hasattr(g.due_date, "isoformat")
                else str(g.due_date),
                "total_tasks": len(ts),
                "done_tasks": done_n,
                "tasks": [
                    {
                        "id": int(t.id),
                        "public_id": t.public_id,
                        "title": t.title,
                        "status": t.status,
                        "completed_at": t.completed_at.isoformat()
                        if (t.completed_at and hasattr(t.completed_at, "isoformat"))
                        else (str(t.completed_at) if t.completed_at else None),
                    }
                    for t in ts
                ],
            }
        )

    return {
        "ok": True,
        "ym": f"{y:04d}-{m:02d}",
        "month_start": month_start.isoformat(),
        "month_end": month_end.isoformat(),
        "days": days,
        "goals": goals_out,
    }


@app.post("/api/skills/focus_report")
def focus_report(report: FocusReportIn) -> dict:
    """Skill: focus_report

    用于外部 agent 上报任务执行情况。
    - 每次上报都会作为 Event 持久化（kind=skill.focus_report）
    - 注意：上报“完成”不等于真实完成，是否完成必须由人确认（详情页按钮）。
    """
    payload = {
        "task_name": report.task_name,
        "status": report.status,
        "goal_id": report.goal_id,
        "task_public_id": report.task_public_id,
        "user_prompt": report.user_prompt,
        "assistant_response": report.assistant_response,
        "metadata": report.metadata,
    }

    with session_scope() as s:
        s.add(
            Event(
                kind="skill.focus_report",
                agent=report.agent,
                task_id=report.task_public_id,
                payload=payload,
            )
        )
        s.flush()
    _try_audit_memory(
        kind="skill.focus_report",
        source=f"agent:{report.agent}",
        summary=f"Focus report for task `{report.task_name}` with status `{report.status}`.",
        detail=json.dumps(payload, ensure_ascii=False, indent=2),
        goal_id=report.goal_id,
        task_public_id=report.task_public_id,
        metadata={"status": report.status},
    )
    return {"ok": True, "task_updated": None}


def _event_summary(kind: str, payload: object) -> str:
    """将 Event 转成用于 UI 展示的短摘要。"""

    if kind == "task.confirmed_done":
        return "已人工确认完成"
    if kind == "goal.confirmed_done_by_user":
        return "confirm done by user"
    if kind == "goal.reopened_by_user":
        return "reopen by user"
    if kind == "task.reopened":
        return "已重新打开（从完成状态恢复）"

    if kind == "companion.pairing_code.requested":
        return "申请配对码"
    if kind == "companion.pair.attempted":
        return "提交认证码"
    if kind == "companion.paired":
        return "配对成功"
    if kind == "companion.disconnected":
        return "失去连接"
    if kind == "companion.deleted":
        return "已删除 Companion"

    if isinstance(payload, dict):
        msg = payload.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()

        # focus_report 的 status 需要做可读化
        if kind == "skill.focus_report":
            tn = payload.get("task_name")
            st = payload.get("status")
            st_label = _status_label(st)
            if tn and st_label:
                return f"{tn} · {st_label}（待确认）"
            if st_label:
                return f"{st_label}（待确认）"

        # 常见上报：percent 进度
        # 产品约束：进度仅二元（完成/未完成），不要展示 80% 等百分比。
        if kind in {"task.progress", "task.started", "task.completed"}:
            if kind == "task.started":
                return "开始执行"
            if kind == "task.completed":
                return "上报完成（待确认）"
            return "有新进展（待确认）"

        # 避免直接暴露 status=... 这种调试风格
        st2 = payload.get("status")
        if isinstance(st2, str) and st2.strip():
            st_label2 = _status_label(st2)
            if st_label2:
                return st_label2

    # 兜底：返回可读 kind_label
    return _event_kind_label(kind, payload)


def _status_label(status: object) -> str:
    x = str(status or "").strip().lower()
    if not x:
        return ""
    if x in {"succeeded", "success", "ok", "done", "completed"}:
        return "已完成"
    if x in {"failed", "fail", "error"}:
        return "失败"
    if x in {"running", "in_progress", "progress"}:
        return "进行中"
    return str(status).strip()


def _event_source_label(agent: str | None) -> str:
    a = (agent or "").strip()
    if not a:
        return "来源：未知"
    if a.lower() in {"ui", "web", "webui"} or a.lower().endswith("/ui"):
        return "来源：Web 操作"
    return f"来源：Agent（{a}）"


def _event_kind_label(kind: str, payload: object) -> str:
    # 面向人：把内部事件类型翻译成更容易理解的短标题
    if kind == "skill.focus_report":
        return "执行结果上报"
    if kind == "task.completed":
        return "上报完成"
    if kind == "task.progress":
        return "进度上报"
    if kind == "task.started":
        return "开始执行"
    if kind == "task.reopened":
        return "重新打开"
    if kind == "task.confirmed_done":
        return "人工确认完成"
    if kind == "goal.confirmed_done_by_user":
        return "confirm done by user"
    if kind == "goal.reopened_by_user":
        return "reopen by user"
    if kind == "companion.pairing_code.requested":
        return "Companion 配对"
    if kind == "companion.pair.attempted":
        return "Companion 配对"
    if kind == "companion.paired":
        return "Companion 配对"
    if kind == "companion.disconnected":
        return "Companion 连接"
    if kind == "companion.deleted":
        return "Companion 管理"
    return kind


@app.get("/tasks/{task_public_id}/agent_space", response_class=HTMLResponse)
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
        },
    )


@app.get("/api/tasks/{task_public_id}/agent_space")
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
            },
        }


@app.post("/api/tasks/{task_public_id}/agent_space")
def create_agent_space(task_public_id: str, payload: AgentSpaceCreateIn) -> dict:
    root_path = str((payload.root_path or "").strip())
    if not root_path:
        raise HTTPException(status_code=400, detail="root_path 不能为空")

    with session_scope() as s:
        task = s.query(Task).filter(Task.public_id == task_public_id).one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")

        comp = s.get(Companion, int(payload.companion_id))
        if comp is None:
            raise HTTPException(status_code=400, detail="Companion 不存在")
        if comp.status != "active" or not (comp.auth_token or "").strip():
            raise HTTPException(status_code=400, detail="Companion 未认证/不可用")

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
            s.add(existing)
            s.flush()
            space = existing
        else:
            space = AgentSpace(
                task_public_id=task_public_id,
                companion_id=int(payload.companion_id),
                root_path=root_path,
                agent_type="trae-cli",
            )
            s.add(space)
            s.flush()

    return {"ok": True, "space_id": space.id}


@app.delete("/api/tasks/{task_public_id}/agent_space")
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

        sessions = s.query(AgentSession).filter(AgentSession.space_id == space.id).all()
        sess_ids = [ss.session_id for ss in sessions]

        terms = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.space_id == space.id)
            .all()
        )
        term_ids = [t.terminal_id for t in terms]

    # best-effort stop on Companion
    cid = int(getattr(comp, "id", 0) or 0) if comp is not None else 0
    conn = COMPANION_GRPC.registry.get(cid) if cid else None
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
            s.query(AgentMessage).filter(AgentMessage.session_id.in_(sess_ids)).delete(
                synchronize_session=False
            )
            s.query(AgentSession).filter(AgentSession.session_id.in_(sess_ids)).delete(
                synchronize_session=False
            )

        # 先清理终端输出日志，再清理 session 元信息
        s.query(RemoteTerminalOutput).filter(
            RemoteTerminalOutput.space_id == space.id
        ).delete(synchronize_session=False)
        s.query(RemoteTerminalSession).filter(
            RemoteTerminalSession.space_id == space.id
        ).delete(synchronize_session=False)
        s.delete(space)

    return {"ok": True}


@app.get("/api/agent_spaces/{space_id}/files/list")
async def agent_space_files_list(space_id: int, path: str = "") -> dict:
    return await companion_service.list_space_files(
        COMPANION_GRPC, space_id=space_id, path=path
    )


@app.get("/api/agent_spaces/{space_id}/files/read")
async def agent_space_files_read(space_id: int, path: str) -> dict:
    return await companion_service.read_space_file(
        COMPANION_GRPC, space_id=space_id, path=path
    )


@app.get("/api/agent_spaces/{space_id}/files/raw")
async def agent_space_files_raw(space_id: int, path: str) -> Response:
    return await companion_service.raw_space_file(
        COMPANION_GRPC, space_id=space_id, path=path
    )


def _openfocus_base_url(request: Request) -> str:
    try:
        return str(request.base_url).rstrip("/")
    except Exception:
        return "http://127.0.0.1:8001"


def _inject_openfocus_prompt(
    *, base_url: str, task_public_id: str, session_id: str, user_prompt: str
) -> str:
    # 轻量注入：每次 send 时拼接，避免依赖 agent 侧的“系统 prompt”能力。
    head = (
        "你在 OpenFocus 的 AgentSpace 中工作。\n"
        f"taskId={task_public_id}\n"
        f"agentSessionId={session_id}\n"
        f"openfocusBaseUrl={base_url}\n"
        "执行过程中请持续上报进度：POST /api/agent/events；最终结果可用 POST /api/skills/focus_report。\n"
        "若你支持 OpenFocus 的 focus_report skill，请优先使用 skill 上报。\n"
        "---\n"
    )
    return head + str(user_prompt or "")


def _load_space_and_optional_companion(
    space_id: int,
) -> tuple[AgentSpace, Companion | None]:
    return companion_service.load_space_and_optional_companion(space_id)


def _require_companion_online(*, sp: AgentSpace, comp: Companion | None):
    return companion_service.require_online(COMPANION_GRPC, companion=comp)


def _inspiration_terminal_space_id(space_id: int) -> int:
    return inspiration_terminal_bridge.terminal_space_id(int(space_id))


def _select_online_companion(
    companion_id: int | None = None,
) -> tuple[Companion, object]:
    return companion_service.select_online(COMPANION_GRPC, companion_id)


def _has_online_companion() -> bool:
    return companion_service.has_online(COMPANION_GRPC)


def _inspiration_terminal_payload(space_id: int, t: RemoteTerminalSession) -> dict:
    return inspiration_terminal_bridge.terminal_payload(
        int(space_id), t, embed_path=_inspiration_ttyd_embed_path
    )


def _build_inspiration_draft_summary_prompt(space: InspirationSpace) -> str:
    return inspiration_terminal_bridge.draft_summary_prompt(
        space, base_url=str(os.environ.get("OPENFOCUS_BASE_URL") or "").strip()
    )


def _inspiration_terminal_conn(companion_id: int | None):
    comp_id = int(companion_id or 0)
    if not comp_id:
        raise HTTPException(status_code=400, detail="Terminal has no Companion")
    _comp, conn = _select_online_companion(comp_id)
    return conn


async def _inspiration_release_terminals(space_id: int) -> int:
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
        with contextlib.suppress(Exception):
            conn = _inspiration_terminal_conn(comp_id)
            await conn.request_terminal_stop(terminal_id=tid, timeout_seconds=5.0)
    with session_scope() as s:
        terminal_service.delete_owner_terminal_records(s, owner=owner)
    for info in term_infos:
        _TTYD_AGENT_MODE.pop(str(info.get("terminal_id") or ""), None)
    return len(term_infos)


@app.get("/api/inspirations/{space_id:int}/terminals")
def inspiration_terminals_list(space_id: int) -> dict:
    with session_scope() as s:
        space = _inspiration_space_or_404(s, int(space_id))
        _inspiration_workspace_path(space, int(space_id))
        owner = terminal_service.owner_for_inspiration_space(int(space_id))
        terms = terminal_service.list_terminals(s, owner)
    return {
        "ok": True,
        "companion": {"online": _has_online_companion()},
        "terminals": [_inspiration_terminal_payload(int(space_id), t) for t in terms],
    }


@app.post("/api/inspirations/{space_id:int}/terminals/new")
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
        space = _inspiration_space_or_404(s, int(space_id))
        if str(space.status or "open") != "open":
            raise HTTPException(
                status_code=400, detail="Only open spaces can start terminals"
            )
        workspace = _inspiration_workspace_path(space, int(space_id))
        space.mode = "terminal"
        space.workspace_path = str(workspace)
        s.flush()
        workspace_path = str(workspace)
    companion_id = payload.get("companion_id") if isinstance(payload, dict) else None
    try:
        comp, conn = _select_online_companion(
            int(companion_id) if companion_id else None
        )
    except (TypeError, ValueError):
        comp, conn = _select_online_companion(None)

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
        raise HTTPException(status_code=502, detail=f"Companion Terminal 启动失败：{e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Companion Terminal 启动失败：{e}")
    real_tid = (res.terminal_id or "").strip() or terminal_id
    backend = str(getattr(res, "backend", "") or "ttyd").strip() or "ttyd"
    connect_url = str(getattr(res, "connect_url", "") or "").strip()
    if backend == "ttyd" and not connect_url:
        raise HTTPException(
            status_code=502, detail="Companion Terminal 启动失败：missing connect_url"
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
        terminal_payload = _inspiration_terminal_payload(int(space_id), t)
    _try_audit_memory(
        kind="inspiration.terminal_created",
        source="web",
        summary=f"Created inspiration terminal `{name}`.",
        detail=f"InspirationSpace {int(space_id)} created terminal {real_tid} at {workspace_path}.",
        metadata={"space_id": int(space_id), "terminal_id": real_tid, "name": name},
    )
    return {"ok": True, "terminal": terminal_payload}


@app.post("/api/inspirations/{space_id:int}/terminals/{terminal_id}/inject")
async def inspiration_terminals_inject(
    space_id: int, terminal_id: str, payload: dict
) -> dict:
    owner_sid = _inspiration_terminal_space_id(int(space_id))
    tid = str(terminal_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="terminal_id is required")
    with session_scope() as s:
        _inspiration_space_or_404(s, int(space_id))
        t = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == tid)
            .one_or_none()
        )
        if t is None or int(t.space_id) != owner_sid:
            raise HTTPException(status_code=404, detail="Terminal not found")
        comp_id = int(t.companion_id or 0)
    conn = _inspiration_terminal_conn(comp_id)
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
    _try_audit_memory(
        kind="inspiration.terminal_input",
        source="web",
        summary=f"Injected input to inspiration terminal `{tid}`.",
        detail=_memory_decode_terminal_bytes(raw),
        metadata={"space_id": int(space_id), "terminal_id": tid},
    )
    return {"ok": True}


@app.post("/api/inspirations/{space_id:int}/terminals/{terminal_id}/rename")
async def inspiration_terminals_rename(
    space_id: int, terminal_id: str, payload: dict
) -> dict:
    tid = str(terminal_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="terminal_id is required")
    raw_name = str((payload or {}).get("name") or "").strip()
    if not raw_name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    if len(raw_name) > 128:
        raise HTTPException(status_code=400, detail="name 过长（<=128）")
    with session_scope() as s:
        _inspiration_space_or_404(s, int(space_id))
        owner = terminal_service.owner_for_inspiration_space(int(space_id))
        try:
            terminal_service.rename_terminal(
                s, owner=owner, terminal_id=tid, name=raw_name
            )
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")
        except terminal_service.TerminalNameConflict:
            raise HTTPException(status_code=400, detail="name 已存在")
    return {"ok": True, "terminal": {"terminal_id": tid, "name": raw_name}}


@app.post("/api/inspirations/{space_id:int}/terminals/{terminal_id}/agent_mode")
async def inspiration_terminals_agent_mode(
    space_id: int, terminal_id: str, payload: dict
) -> dict:
    owner_sid = _inspiration_terminal_space_id(int(space_id))
    tid = str(terminal_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="terminal_id is required")
    with session_scope() as s:
        _inspiration_space_or_404(s, int(space_id))
        t = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == tid)
            .one_or_none()
        )
        if t is None or int(t.space_id) != owner_sid:
            raise HTTPException(status_code=404, detail="Terminal not found")
    enabled = bool((payload or {}).get("enabled"))
    prefix = str((payload or {}).get("prefix") or "").strip()
    if enabled and prefix:
        _TTYD_AGENT_MODE[tid] = {"enabled": True, "prefix": prefix}
    else:
        _TTYD_AGENT_MODE.pop(tid, None)
    return {"ok": True, "enabled": enabled}


@app.post("/api/inspirations/{space_id:int}/terminals/{terminal_id}/mouse_mode")
async def inspiration_terminals_mouse_mode(
    space_id: int, terminal_id: str, payload: dict
) -> dict:
    owner_sid = _inspiration_terminal_space_id(int(space_id))
    tid = str(terminal_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="terminal_id is required")
    with session_scope() as s:
        _inspiration_space_or_404(s, int(space_id))
        t = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == tid)
            .one_or_none()
        )
        if t is None or int(t.space_id) != owner_sid:
            raise HTTPException(status_code=404, detail="Terminal not found")
        comp_id = int(t.companion_id or 0)
    conn = _inspiration_terminal_conn(comp_id)
    enabled = bool((payload or {}).get("enabled"))
    try:
        res = await conn.request_terminal_mouse_mode(
            terminal_id=tid, enabled=enabled, timeout_seconds=10.0
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"terminal mouse mode failed: {e}")
    return {"ok": True, "enabled": bool(getattr(res, "enabled", enabled))}


@app.post(
    "/api/inspirations/{space_id:int}/terminals/{terminal_id}/prepare_draft_summary"
)
async def inspiration_terminal_prepare_draft_summary(
    space_id: int, terminal_id: str
) -> dict:
    with session_scope() as s:
        space = _inspiration_space_or_404(s, int(space_id))
        prompt = _build_inspiration_draft_summary_prompt(space)
    return await inspiration_terminals_inject(
        int(space_id),
        str(terminal_id),
        {"data_b64": base64.b64encode(prompt.encode("utf-8")).decode("ascii")},
    )


@app.post("/api/inspirations/{space_id:int}/terminals/{terminal_id}/close")
async def inspiration_terminals_close(space_id: int, terminal_id: str) -> dict:
    tid = str(terminal_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="terminal_id is required")
    comp_id = 0
    with session_scope() as s:
        _inspiration_space_or_404(s, int(space_id))
        owner = terminal_service.owner_for_inspiration_space(int(space_id))
        try:
            t = terminal_service.get_terminal_for_owner(s, owner=owner, terminal_id=tid)
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")
        comp_id = int(t.companion_id or 0)
    with contextlib.suppress(Exception):
        conn = _inspiration_terminal_conn(comp_id)
        await conn.request_terminal_stop(terminal_id=tid, timeout_seconds=10.0)
    with session_scope() as s:
        terminal_service.delete_terminal_record(s, terminal_id=tid)
    _TTYD_AGENT_MODE.pop(tid, None)
    return {"ok": True}


def _load_inspiration_ttyd_terminal(
    space_id: int, terminal_id: str
) -> tuple[InspirationSpace, str]:
    owner_sid = _inspiration_terminal_space_id(int(space_id))
    tid = str(terminal_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="terminal_id is required")
    with session_scope() as s:
        space = _inspiration_space_or_404(s, int(space_id))
        t = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == tid)
            .one_or_none()
        )
        if t is None or int(t.space_id) != owner_sid:
            raise HTTPException(status_code=404, detail="Terminal not found")
        backend = str(getattr(t, "backend", "") or "ttyd").strip()
        connect_url = str(getattr(t, "connect_url", "") or "").strip()
    if backend != "ttyd" or not connect_url:
        raise HTTPException(status_code=404, detail="ttyd terminal not found")
    return space, connect_url.rstrip("/")


@app.get("/api/agent_spaces/{space_id}/terminals")
def terminals_list(space_id: int) -> dict:
    sp, comp = _load_space_and_optional_companion(space_id)
    with session_scope() as s:
        owner = terminal_service.owner_for_agent_space(int(sp.id))
        terms = terminal_service.list_terminals(s, owner)

    cid = int(getattr(comp, "id", 0) or 0) if comp is not None else 0
    online = bool(cid and (COMPANION_GRPC.registry.get(cid) is not None))

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


@app.post("/api/agent_spaces/{space_id}/terminals/new")
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
        raise HTTPException(status_code=502, detail=f"Companion Terminal 启动失败：{e}")

    real_tid = (res.terminal_id or "").strip() or terminal_id
    backend = str(getattr(res, "backend", "") or "ttyd").strip() or "ttyd"
    connect_url = str(getattr(res, "connect_url", "") or "").strip()

    with session_scope() as s:
        owner = terminal_service.owner_for_agent_space(int(sp.id))
        t = terminal_service.create_terminal_record(
            s,
            owner=owner,
            task_public_id=str(sp.task_public_id or ""),
            companion_id=int(getattr(comp, "id", 0) or 0) if comp is not None else None,
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


@app.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/rename")
async def terminals_rename(space_id: int, terminal_id: str, payload: dict) -> dict:
    sp, _ = _load_space_and_optional_companion(space_id)

    tid = str(terminal_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="terminal_id is required")

    raw_name = str((payload or {}).get("name") or "").strip()
    if not raw_name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    if len(raw_name) > 128:
        raise HTTPException(status_code=400, detail="name 过长（<=128）")

    with session_scope() as s:
        owner = terminal_service.owner_for_agent_space(int(sp.id))
        try:
            terminal_service.rename_terminal(
                s, owner=owner, terminal_id=tid, name=raw_name
            )
        except terminal_service.TerminalNotFound:
            raise HTTPException(status_code=404, detail="Terminal not found")
        except terminal_service.TerminalNameConflict:
            raise HTTPException(status_code=400, detail="name 已存在")

    return {"ok": True, "terminal": {"terminal_id": tid, "name": raw_name}}


@app.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/inject")
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


@app.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/agent_mode")
async def terminals_agent_mode(space_id: int, terminal_id: str, payload: dict) -> dict:
    sp, _ = _load_space_and_optional_companion(space_id)
    tid = str(terminal_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="terminal_id is required")
    with session_scope() as s:
        t = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == tid)
            .one_or_none()
        )
        if t is None or int(t.space_id) != int(sp.id):
            raise HTTPException(status_code=404, detail="Terminal not found")
    enabled = bool((payload or {}).get("enabled"))
    prefix = str((payload or {}).get("prefix") or "").strip()
    if enabled and prefix:
        _TTYD_AGENT_MODE[tid] = {"enabled": True, "prefix": prefix}
    else:
        _TTYD_AGENT_MODE.pop(tid, None)
    return {"ok": True, "enabled": enabled}


@app.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/mouse_mode")
async def terminals_mouse_mode(space_id: int, terminal_id: str, payload: dict) -> dict:
    sp, comp = _load_space_and_optional_companion(space_id)
    conn = _require_companion_online(sp=sp, comp=comp)
    tid = str(terminal_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="terminal_id is required")
    with session_scope() as s:
        t = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == tid)
            .one_or_none()
        )
        if t is None or int(t.space_id) != int(sp.id):
            raise HTTPException(status_code=404, detail="Terminal not found")
    enabled = bool((payload or {}).get("enabled"))
    try:
        res = await conn.request_terminal_mouse_mode(
            terminal_id=tid, enabled=enabled, timeout_seconds=10.0
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"terminal mouse mode failed: {e}")
    return {"ok": True, "enabled": bool(getattr(res, "enabled", enabled))}


@app.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/close")
async def terminals_close(space_id: int, terminal_id: str) -> dict:
    sp, comp = _load_space_and_optional_companion(space_id)

    tid = str(terminal_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="terminal_id is required")

    with session_scope() as s:
        t = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == tid)
            .one_or_none()
        )
        if t is None or int(t.space_id) != int(sp.id):
            raise HTTPException(status_code=404, detail="Terminal not found")

    # best-effort stop on Companion (offline 也允许 close：只保证 OpenFocus 侧不再展示)
    cid = int(getattr(comp, "id", 0) or 0) if comp is not None else 0
    conn = COMPANION_GRPC.registry.get(cid) if cid else None
    if conn is not None:
        try:
            await conn.request_terminal_stop(terminal_id=tid, timeout_seconds=10.0)
        except Exception:
            pass

    with session_scope() as s:
        # 关闭即删除记录（避免刷新后重新出现 tab）
        terminal_service.delete_terminal_record(s, terminal_id=tid)
    _TTYD_AGENT_MODE.pop(tid, None)

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
        t = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == tid)
            .one_or_none()
        )
        if t is None or int(t.space_id) != int(sp.id):
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


@app.api_route(
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
        if k.lower() not in {"host", "connection", "content-length", "accept-encoding"}
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
        return Response(content=data, status_code=int(e.code), media_type=media_type)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ttyd proxy failed: {e}")

    excluded = {"content-encoding", "transfer-encoding", "connection", "content-length"}
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


@app.websocket("/api/agent_spaces/{space_id}/terminals/{terminal_id}/ttyd/{path:path}")
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
                            _rewrite_ttyd_input_for_agent_mode(terminal_id, msg["text"])
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


@app.api_route(
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
        if k.lower() not in {"host", "connection", "content-length", "accept-encoding"}
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
        return Response(content=data, status_code=int(e.code), media_type=media_type)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ttyd proxy failed: {e}")
    excluded = {"content-encoding", "transfer-encoding", "connection", "content-length"}
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


@app.websocket(
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
                            _rewrite_ttyd_input_for_agent_mode(
                                terminal_id, msg["bytes"]
                            )
                        )
                    elif msg.get("text") is not None:
                        await upstream.send(
                            _rewrite_ttyd_input_for_agent_mode(terminal_id, msg["text"])
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


@app.get("/api/agent_spaces/{space_id}/terminals/{terminal_id}/history")
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
        t = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == tid)
            .one_or_none()
        )
        if t is None or int(t.space_id) != int(sp.id):
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
        last_alt_enter = max((b.rfind(pat) for pat in alt_enter_markers), default=-1)
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


@app.get("/api/agent_spaces/{space_id}/agent/sessions")
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
    online = bool(cid and (COMPANION_GRPC.registry.get(cid) is not None))
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


@app.post("/api/agent_spaces/{space_id}/agent/sessions/new")
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
        raise HTTPException(status_code=502, detail=f"Companion Agent 启动失败：{e}")

    real_sid = (res.session_id or "").strip() or session_id
    with session_scope() as s:
        ss = AgentSession(
            session_id=real_sid,
            space_id=int(sp.id),
            task_public_id=str(sp.task_public_id or ""),
            companion_id=int(getattr(comp, "id", 0) or 0) if comp is not None else None,
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


@app.get("/api/agent_spaces/{space_id}/agent/sessions/{session_id}/messages")
def agent_session_messages(space_id: int, session_id: str) -> dict:
    sp, _comp = _load_space_and_optional_companion(space_id)
    sid = str(session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")

    with session_scope() as s:
        sess = (
            s.query(AgentSession).filter(AgentSession.session_id == sid).one_or_none()
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


@app.post("/api/agent_spaces/{space_id}/agent/sessions/{session_id}/terminate")
async def agent_session_terminate(space_id: int, session_id: str) -> dict:
    sp, comp = _load_space_and_optional_companion(space_id)
    conn = _require_companion_online(sp=sp, comp=comp)
    sid = str(session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")

    with session_scope() as s:
        sess = (
            s.query(AgentSession).filter(AgentSession.session_id == sid).one_or_none()
        )
        if sess is None or int(sess.space_id) != int(sp.id):
            raise HTTPException(status_code=404, detail="Agent session not found")

    try:
        await conn.request_agent_terminate(session_id=sid, timeout_seconds=10.0)
    except CompanionGrpcError as e:
        raise HTTPException(status_code=502, detail=f"Companion Agent 终止失败：{e}")

    with session_scope() as s:
        sess = (
            s.query(AgentSession).filter(AgentSession.session_id == sid).one_or_none()
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


@app.post("/api/agent_spaces/{space_id}/agent/sessions/{session_id}/send")
async def agent_session_send(request: Request, space_id: int, session_id: str) -> dict:
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
            s.query(AgentSession).filter(AgentSession.session_id == sid).one_or_none()
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
        raise HTTPException(status_code=502, detail=f"Companion Agent 发送失败：{e}")

    return {"ok": True, "request_id": rid}


@app.get("/api/agent_spaces/{space_id}/agent/sessions/{session_id}/sse")
async def agent_session_sse(space_id: int, session_id: str) -> StreamingResponse:
    sp, _comp = _load_space_and_optional_companion(space_id)
    sid = str(session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")

    with session_scope() as s:
        sess = (
            s.query(AgentSession).filter(AgentSession.session_id == sid).one_or_none()
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
