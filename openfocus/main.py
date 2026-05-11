from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import json
import mimetypes
import os
import re
import shutil
import threading
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
from .companion_grpc import (
    CompanionGrpcError,
    CompanionGrpcServer,
    add_agent_chunk_listener,
    add_terminal_output_listener,
)
from .db import get_engine, session_scope
from .models import (
    AgentMessage,
    AgentSession,
    AgentSpace,
    Base,
    Companion,
    Event,
    Goal,
    GoalPlanMessage,
    GoalPlanSession,
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


# 静态资源：远程终端前端（remote-terminal/）
_REMOTE_TERMINAL_DIR = (APP_DIR.parent / "remote-terminal").resolve()
if _REMOTE_TERMINAL_DIR.exists() and _REMOTE_TERMINAL_DIR.is_dir():
    app.mount(
        "/remote-terminal",
        StaticFiles(directory=str(_REMOTE_TERMINAL_DIR)),
        name="remote-terminal",
    )

# 静态资源：内置资源（resources/，例如 icons）
_RESOURCES_DIR = (APP_DIR.parent / "resources").resolve()
if _RESOURCES_DIR.exists() and _RESOURCES_DIR.is_dir():
    app.mount(
        "/resources", StaticFiles(directory=str(_RESOURCES_DIR)), name="resources"
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


_MEMORY_LOCK = threading.RLock()


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _memory_audit_window_seconds() -> int:
    raw = str(os.environ.get("OPENFOCUS_MEMORY_AUDIT_WINDOW_SECONDS") or "").strip()
    try:
        return max(60, int(raw or 3600))
    except Exception:
        return 3600


def _memory_audit_max_entries() -> int:
    raw = str(os.environ.get("OPENFOCUS_MEMORY_AUDIT_MAX_ENTRIES") or "").strip()
    try:
        return max(1, int(raw or 2000))
    except Exception:
        return 2000


def _memory_audit_ttl_days() -> int:
    raw = str(os.environ.get("OPENFOCUS_MEMORY_AUDIT_TTL_DAYS") or "").strip()
    try:
        return max(1, int(raw or 7))
    except Exception:
        return 7


def _memory_state_path() -> Path:
    return _memory_dir() / ".memory_state.json"


def _memory_audit_root() -> Path:
    p = _memory_dir() / "audit"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _memory_daily_root() -> Path:
    p = _memory_dir() / "daily"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _memory_long_term_path() -> Path:
    return _memory_dir() / "MEMORY.md"


def _memory_path_from_rel(rel_path: str) -> Path:
    rel = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
    p = (_memory_dir() / rel).resolve()
    base = _memory_dir().resolve()
    if p != base and base not in p.parents:
        raise ValueError("invalid memory path")
    return p


def _memory_rel_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(_memory_dir().resolve()).as_posix()
    except Exception:
        return path.name


def _memory_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _memory_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _memory_append_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(content)


def _memory_load_state_unlocked() -> dict:
    raw = _memory_read_text(_memory_state_path()).strip()
    if not raw:
        return {
            "current_audit": None,
            "summarized_audits": [],
            "finalized_days": [],
            "last_maintenance_at": None,
        }
    try:
        data = json.loads(raw)
    except Exception:
        return {
            "current_audit": None,
            "summarized_audits": [],
            "finalized_days": [],
            "last_maintenance_at": None,
        }
    if not isinstance(data, dict):
        data = {}
    summarized = data.get("summarized_audits")
    data["summarized_audits"] = [
        str(x)
        for x in (summarized if isinstance(summarized, list) else [])
        if str(x).strip()
    ]
    finalized = data.get("finalized_days")
    data["finalized_days"] = [
        str(x)
        for x in (finalized if isinstance(finalized, list) else [])
        if str(x).strip()
    ]
    if not isinstance(data.get("current_audit"), dict):
        data["current_audit"] = None
    return data


def _memory_save_state_unlocked(state: dict) -> None:
    payload = {
        "current_audit": state.get("current_audit"),
        "summarized_audits": list(
            dict.fromkeys(
                [str(x) for x in state.get("summarized_audits") or [] if str(x).strip()]
            )
        ),
        "finalized_days": list(
            dict.fromkeys(
                [str(x) for x in state.get("finalized_days") or [] if str(x).strip()]
            )
        ),
        "last_maintenance_at": state.get("last_maintenance_at"),
    }
    _memory_write_text(
        _memory_state_path(), json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _memory_parse_ts(value: object) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        raw = raw.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None


def _memory_iso(ts: dt.datetime | None) -> str:
    if ts is None:
        ts = _utcnow()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _memory_decode_terminal_bytes(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("utf-8", errors="replace")


def _memory_extract_json_blocks(text: str) -> list[dict]:
    out: list[dict] = []
    for m in re.finditer(r"```json\n(.*?)\n```", text or "", flags=re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _memory_entry_markdown(entry: dict) -> str:
    ts = str(entry.get("timestamp") or _memory_iso(None))
    kind = str(entry.get("kind") or "memory.event")
    source = str(entry.get("source") or "system")
    summary = str(entry.get("summary") or kind).strip()
    detail = str(entry.get("detail") or "").strip()
    task_id = str(entry.get("task_public_id") or "").strip()
    goal_id = entry.get("goal_id")
    lines = [f"## {ts} · {kind}", f"- Source: {source}", f"- Summary: {summary}"]
    if task_id:
        lines.append(f"- Task: {task_id}")
    if goal_id not in (None, ""):
        lines.append(f"- Goal: {goal_id}")
    if detail:
        lines.append("")
        lines.append(detail)
    lines.extend(
        ["", "```json", json.dumps(entry, ensure_ascii=False, indent=2), "```", ""]
    )
    return "\n".join(lines)


def _memory_render_audit_header(*, started_at: dt.datetime) -> str:
    return (
        "# Audit Memory\n\n"
        f"- Started At: {_memory_iso(started_at)}\n"
        f"- Rotation: {int(_memory_audit_window_seconds() / 60)} minutes or {_memory_audit_max_entries()} entries\n"
        f"- TTL: {_memory_audit_ttl_days()} days\n\n"
        "---\n\n"
    )


def _memory_render_daily_summary(
    *, day: str, file_label: str, started_at: str, ended_at: str, entries: list[dict]
) -> str:
    counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    highlights: list[str] = []
    for entry in entries:
        kind = str(entry.get("kind") or "memory.event")
        counts[kind] = counts.get(kind, 0) + 1
        source = str(entry.get("source") or "system")
        source_counts[source] = source_counts.get(source, 0) + 1
        summary = str(entry.get("summary") or "").strip()
        if summary and summary not in highlights:
            highlights.append(summary)
    top_kinds = sorted(counts.items(), key=lambda it: (-it[1], it[0]))[:5]
    top_sources = sorted(source_counts.items(), key=lambda it: (-it[1], it[0]))[:5]
    lines = [
        f"## Audit Window · {file_label}",
        f"- Start: {started_at}",
        f"- End: {ended_at}",
        f"- Entries: {len(entries)}",
    ]
    if top_sources:
        lines.append(
            "- Sources: "
            + ", ".join(f"{name} ({count})" for name, count in top_sources)
        )
    if top_kinds:
        lines.append(
            "- Top Kinds: "
            + ", ".join(f"{name} ({count})" for name, count in top_kinds)
        )
    if highlights:
        lines.append("")
        lines.append("### Highlights")
        for item in highlights[:8]:
            lines.append(f"- {item}")
    lines.extend(["", "---", ""])
    return "\n".join(lines)


def _memory_render_daily_final(day: str, content: str) -> str:
    lines = [ln.rstrip() for ln in (content or "").splitlines()]
    cleaned = [ln for ln in lines if ln.strip()]
    highlights: list[str] = []
    for ln in cleaned:
        if ln.startswith("- "):
            bullet = ln[2:].strip()
            if bullet and bullet not in highlights:
                highlights.append(bullet)
        if len(highlights) >= 12:
            break
    out = [f"# Daily Memory · {day}", "", f"- Finalized At: {_memory_iso(None)}", ""]
    if highlights:
        out.append("## Final Highlights")
        for item in highlights[:12]:
            out.append(f"- {item}")
        out.append("")
    out.append("## Source Material")
    out.append("")
    out.append(content.strip() or "No daily material.")
    out.append("")
    return "\n".join(out)


def _memory_extract_long_term_items(day: str, daily_text: str) -> list[str]:
    items: list[str] = []
    text = daily_text or ""
    lower = text.lower()
    if "trae-cli" in lower:
        items.append(f"- {day}: Uses `trae-cli` in AgentSpace workflows.")
    if "plan mode" in lower:
        items.append(f"- {day}: Uses Plan Mode for task decomposition.")
    if "terminal" in lower or "web shell" in lower:
        items.append(
            f"- {day}: Works through AgentSpace terminal / web shell interactions."
        )
    if not items:
        return [f"- {day}: No stable preference or fact extracted yet."]
    return items


def _memory_write_long_term_unlocked(*, day: str, items: list[str]) -> None:
    path = _memory_long_term_path()
    existing = _memory_read_text(path).strip()
    kept: list[str] = []
    if existing:
        for ln in existing.splitlines():
            stripped = ln.rstrip()
            if stripped.startswith(f"- {day}:"):
                continue
            kept.append(stripped)
    else:
        kept = ["# Long-term Memory", "", "## Stable Facts", ""]
    if not any((ln.strip() == "## Stable Facts") for ln in kept):
        if kept and kept[-1] != "":
            kept.append("")
        kept.extend(["## Stable Facts", ""])
    if kept and kept[-1] != "":
        kept.append("")
    kept.extend(items)
    kept.append("")
    _memory_write_text(path, "\n".join(kept).rstrip() + "\n")


def _memory_cleanup_audit_files_unlocked(now: dt.datetime) -> None:
    cutoff = now - dt.timedelta(days=_memory_audit_ttl_days())
    for path in sorted(_memory_audit_root().glob("**/*.md")):
        try:
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        except Exception:
            continue
        if mtime >= cutoff:
            continue
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    for day_dir in sorted(_memory_audit_root().glob("*")):
        try:
            if day_dir.is_dir() and not any(day_dir.iterdir()):
                day_dir.rmdir()
        except Exception:
            pass


def _memory_ensure_daily_file(day: str) -> Path:
    path = _memory_daily_root() / f"{day}.md"
    if not path.exists():
        _memory_write_text(path, f"# Daily Memory · {day}\n\n")
    return path


def _memory_start_audit_file_unlocked(state: dict, now: dt.datetime) -> dict:
    day = now.date().isoformat()
    day_dir = _memory_audit_root() / day
    day_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{now.strftime('%Y-%m-%d_%H-%M-%S')}.md"
    path = day_dir / filename
    counter = 1
    while path.exists():
        filename = f"{now.strftime('%Y-%m-%d_%H-%M-%S')}_{counter}.md"
        path = day_dir / filename
        counter += 1
    _memory_write_text(path, _memory_render_audit_header(started_at=now))
    current = {
        "rel_path": _memory_rel_path(path),
        "started_at": _memory_iso(now),
        "entries": 0,
        "day": day,
    }
    state["current_audit"] = current
    return current


def _memory_mark_summarized_audit_unlocked(state: dict, rel_path: str) -> None:
    rel = str(rel_path or "").strip()
    if not rel:
        return
    items = [
        str(x)
        for x in state.get("summarized_audits") or []
        if str(x).strip() and str(x).strip() != rel
    ]
    items.append(rel)
    state["summarized_audits"] = items[-2000:]


def _memory_finalize_day_unlocked(day: str, state: dict) -> None:
    path = _memory_daily_root() / f"{day}.md"
    if not path.exists():
        return
    current = _memory_read_text(path)
    finalized = _memory_render_daily_final(day, current)
    _memory_write_text(path, finalized)
    _memory_write_long_term_unlocked(
        day=day, items=_memory_extract_long_term_items(day, finalized)
    )
    finalized_days = [
        str(x)
        for x in state.get("finalized_days") or []
        if str(x).strip() and str(x) != day
    ]
    finalized_days.append(day)
    state["finalized_days"] = finalized_days


def _memory_rotate_current_audit_unlocked(
    state: dict,
    now: dt.datetime,
    *,
    force: bool = False,
    create_next: bool = True,
) -> tuple[str | None, str | None]:
    current = (
        state.get("current_audit")
        if isinstance(state.get("current_audit"), dict)
        else None
    )
    if not current:
        return None, None
    started_at = _memory_parse_ts(current.get("started_at")) or now
    entries = int(current.get("entries") or 0)
    age_seconds = max(0, int((now - started_at).total_seconds()))
    if (
        (not force)
        and entries < _memory_audit_max_entries()
        and age_seconds < _memory_audit_window_seconds()
    ):
        return None, None
    rel_path = str(current.get("rel_path") or "").strip()
    if not rel_path:
        state["current_audit"] = None
        return None, None
    if entries <= 0:
        return None, None
    path = _memory_path_from_rel(rel_path)
    if path.exists():
        entries_data = _memory_extract_json_blocks(_memory_read_text(path))
        day = str(current.get("day") or started_at.date().isoformat())
        daily_path = _memory_ensure_daily_file(day)
        label = path.name
        started_iso = _memory_iso(started_at)
        ended_iso = _memory_iso(now)
        summary = _memory_render_daily_summary(
            day=day,
            file_label=label,
            started_at=started_iso,
            ended_at=ended_iso,
            entries=entries_data,
        )
        _memory_append_text(daily_path, summary)
        _memory_mark_summarized_audit_unlocked(state, rel_path)
        state["finalized_days"] = [
            str(x) for x in state.get("finalized_days") or [] if str(x) != day
        ]
    state["current_audit"] = None
    next_rel: str | None = None
    if create_next:
        next_rel = (
            str(
                _memory_start_audit_file_unlocked(state, now).get("rel_path") or ""
            ).strip()
            or None
        )
    return rel_path, next_rel


def _memory_finalize_due_days_unlocked(state: dict, now: dt.datetime) -> None:
    today = now.date().isoformat()
    finalized = {str(x) for x in state.get("finalized_days") or [] if str(x).strip()}
    for path in sorted(_memory_daily_root().glob("*.md")):
        day = path.stem
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        if day >= today or day in finalized:
            continue
        _memory_finalize_day_unlocked(day, state)


def _memory_maintenance(now: dt.datetime | None = None) -> None:
    now = now or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    with _MEMORY_LOCK:
        state = _memory_load_state_unlocked()
        _memory_rotate_current_audit_unlocked(state, now, force=False, create_next=True)
        _memory_finalize_due_days_unlocked(state, now)
        _memory_cleanup_audit_files_unlocked(now)
        state["last_maintenance_at"] = _memory_iso(now)
        _memory_save_state_unlocked(state)


def _memory_append_audit_entry(
    *,
    kind: str,
    source: str,
    summary: str,
    detail: str = "",
    task_public_id: str | None = None,
    goal_id: int | None = None,
    metadata: dict | None = None,
    occurred_at: dt.datetime | None = None,
) -> None:
    now = occurred_at or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    entry = {
        "timestamp": _memory_iso(now),
        "kind": str(kind or "memory.event"),
        "source": str(source or "system"),
        "summary": str(summary or kind or "memory event").strip(),
        "detail": str(detail or "").strip(),
        "task_public_id": str(task_public_id or "").strip() or None,
        "goal_id": goal_id,
        "metadata": metadata or {},
    }
    with _MEMORY_LOCK:
        state = _memory_load_state_unlocked()
        _memory_rotate_current_audit_unlocked(state, now, force=False, create_next=True)
        current = (
            state.get("current_audit")
            if isinstance(state.get("current_audit"), dict)
            else None
        )
        if current is None:
            current = _memory_start_audit_file_unlocked(state, now)
        path = _memory_path_from_rel(str(current.get("rel_path") or ""))
        _memory_append_text(path, _memory_entry_markdown(entry))
        current["entries"] = int(current.get("entries") or 0) + 1
        state["current_audit"] = current
        _memory_finalize_due_days_unlocked(state, now)
        _memory_cleanup_audit_files_unlocked(now)
        state["last_maintenance_at"] = _memory_iso(now)
        _memory_save_state_unlocked(state)


def _memory_file_display_name(path: Path) -> str:
    if path.suffix.lower() == ".md":
        stem = path.stem
        if re.fullmatch(r"\d{2}-\d{2}-\d{2}", stem):
            day = path.parent.name
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                return f"{day} {stem.replace('-', ':')}"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", stem):
            day, tm = stem.split("_", 1)
            return f"{day} {tm.replace('-', ':')}"
    return path.name


def _memory_collect_file_items(root: Path, pattern: str) -> list[dict]:
    state = _memory_load_state_unlocked()
    summarized = {
        str(x) for x in state.get("summarized_audits") or [] if str(x).strip()
    }
    current_rel = ""
    if isinstance(state.get("current_audit"), dict):
        current_rel = str(
            (state.get("current_audit") or {}).get("rel_path") or ""
        ).strip()
    items: list[dict] = []
    for path in sorted(root.glob(pattern), reverse=True):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            updated_at = dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc)
        except Exception:
            updated_at = _utcnow()
        rel_path = _memory_rel_path(path)
        items.append(
            {
                "name": _memory_file_display_name(path),
                "rel_path": rel_path,
                "updated_at": _memory_iso(updated_at),
                "size": int(getattr(stat, "st_size", 0) if "stat" in locals() else 0),
                "summarized": rel_path in summarized,
                "current": rel_path == current_rel,
            }
        )
    return items


def _memory_read_selected_file(rel_path: str | None) -> str:
    raw = str(rel_path or "").strip()
    if not raw:
        return ""
    try:
        return _memory_read_text(_memory_path_from_rel(raw))
    except Exception:
        return ""


def _try_audit_memory(**kwargs) -> None:
    try:
        _memory_append_audit_entry(**kwargs)
    except Exception:
        pass


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


def _map_companion_files_error(e: CompanionGrpcError) -> HTTPException:
    msg = str(e or "").strip()
    low = msg.lower()
    if ("not found" in low) or ("no such file" in low):
        return HTTPException(status_code=404, detail=msg or "not found")
    if ("too large" in low) or ("file too large" in low):
        return HTTPException(status_code=413, detail=msg or "file too large")
    if (
        ("traversal" in low)
        or ("invalid path" in low)
        or ("must be absolute" in low)
        or ("not a directory" in low)
        or ("root_path" in low)
    ):
        return HTTPException(status_code=400, detail=msg or "bad request")
    return HTTPException(status_code=502, detail=f"Companion 文件服务错误：{msg}")


@app.on_event("startup")
def _startup() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)

    # 轻量 SQLite 迁移：给 goals 表补齐新增字段（避免引入 alembic 的复杂度）
    with engine.begin() as conn:
        cols = [
            r[1] for r in conn.exec_driver_sql("PRAGMA table_info(goals)").fetchall()
        ]
        if "title" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE goals ADD COLUMN title VARCHAR(2000) NOT NULL DEFAULT ''"
                )
            )
            # Older schemas stored the goal title in `content`.
            conn.execute(text("UPDATE goals SET title = content WHERE title = ''"))
            if "description" in cols:
                # Older schemas stored the goal body/content in `description`.
                # Only run while adding `title` so future startups do not overwrite
                # user-edited content from the legacy physical column.
                conn.execute(
                    text(
                        "UPDATE goals SET content = description "
                        "WHERE COALESCE(description, '') != ''"
                    )
                )
        if "status" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE goals ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'active'"
                )
            )
        if "priority" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE goals ADD COLUMN priority VARCHAR(32) NOT NULL DEFAULT 'normal'"
                )
            )
        if "importance" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE goals ADD COLUMN importance VARCHAR(32) NOT NULL DEFAULT 'normal'"
                )
            )
        if "source_inspiration_space_id" not in cols:
            conn.execute(
                text("ALTER TABLE goals ADD COLUMN source_inspiration_space_id INTEGER")
            )
        if "source_inspiration_draft_id" not in cols:
            conn.execute(
                text("ALTER TABLE goals ADD COLUMN source_inspiration_draft_id INTEGER")
            )

        task_cols = [
            r[1] for r in conn.exec_driver_sql("PRAGMA table_info(tasks)").fetchall()
        ]
        if "content" not in task_cols:
            conn.execute(
                text(
                    "ALTER TABLE tasks ADD COLUMN content VARCHAR(4000) NOT NULL DEFAULT ''"
                )
            )
            if "description" in task_cols:
                conn.execute(
                    text(
                        "UPDATE tasks SET content = description "
                        "WHERE COALESCE(description, '') != ''"
                    )
                )

        if "task_type" not in task_cols:
            conn.execute(
                text(
                    "ALTER TABLE tasks ADD COLUMN task_type VARCHAR(32) NOT NULL DEFAULT ''"
                )
            )

        if "estimated_minutes" not in task_cols:
            conn.execute(
                text(
                    "ALTER TABLE tasks ADD COLUMN estimated_minutes INTEGER NOT NULL DEFAULT 0"
                )
            )

        if "context_key" not in task_cols:
            conn.execute(
                text(
                    "ALTER TABLE tasks ADD COLUMN context_key VARCHAR(256) NOT NULL DEFAULT ''"
                )
            )
        if "source_inspiration_space_id" not in task_cols:
            conn.execute(
                text("ALTER TABLE tasks ADD COLUMN source_inspiration_space_id INTEGER")
            )
        if "source_inspiration_draft_id" not in task_cols:
            conn.execute(
                text("ALTER TABLE tasks ADD COLUMN source_inspiration_draft_id INTEGER")
            )

        # inspiration_spaces 补字段（workspace + BYO Agent terminal）
        try:
            insp_space_cols = [
                r[1]
                for r in conn.exec_driver_sql(
                    "PRAGMA table_info(inspiration_spaces)"
                ).fetchall()
            ]
            if "mode" not in insp_space_cols:
                conn.execute(
                    text(
                        "ALTER TABLE inspiration_spaces ADD COLUMN mode VARCHAR(32) NOT NULL DEFAULT 'built_in'"
                    )
                )
            if "workspace_path" not in insp_space_cols:
                conn.execute(
                    text(
                        "ALTER TABLE inspiration_spaces ADD COLUMN workspace_path VARCHAR(4000) NOT NULL DEFAULT ''"
                    )
                )
        except Exception:
            pass

        # inspiration_resources 补字段（文件桥接来源）
        try:
            insp_res_cols = [
                r[1]
                for r in conn.exec_driver_sql(
                    "PRAGMA table_info(inspiration_resources)"
                ).fetchall()
            ]
            if "external_path" not in insp_res_cols:
                conn.execute(
                    text(
                        "ALTER TABLE inspiration_resources ADD COLUMN external_path VARCHAR(4000) NOT NULL DEFAULT ''"
                    )
                )
            if "source" not in insp_res_cols:
                conn.execute(
                    text(
                        "ALTER TABLE inspiration_resources ADD COLUMN source VARCHAR(64) NOT NULL DEFAULT 'user'"
                    )
                )
        except Exception:
            pass

        # goal_plan_sessions 补字段（用于“已有 goal 进入 plan”）
        sess_cols = [
            r[1]
            for r in conn.exec_driver_sql(
                "PRAGMA table_info(goal_plan_sessions)"
            ).fetchall()
        ]
        if "source_goal_id" not in sess_cols:
            conn.execute(
                text("ALTER TABLE goal_plan_sessions ADD COLUMN source_goal_id INTEGER")
            )

        # agent_spaces 补字段（Companion 架构升级）
        space_cols = [
            r[1]
            for r in conn.exec_driver_sql("PRAGMA table_info(agent_spaces)").fetchall()
        ]
        if "companion_id" not in space_cols:
            conn.execute(
                text("ALTER TABLE agent_spaces ADD COLUMN companion_id INTEGER")
            )

        # remote_terminal_sessions 补字段（terminal tab rename）
        try:
            term_cols = [
                r[1]
                for r in conn.exec_driver_sql(
                    "PRAGMA table_info(remote_terminal_sessions)"
                ).fetchall()
            ]
            if "name" not in term_cols:
                conn.execute(
                    text(
                        "ALTER TABLE remote_terminal_sessions ADD COLUMN name VARCHAR(128) NOT NULL DEFAULT ''"
                    )
                )
                term_cols.append("name")
            if "backend" not in term_cols:
                conn.execute(
                    text(
                        "ALTER TABLE remote_terminal_sessions ADD COLUMN backend VARCHAR(32) NOT NULL DEFAULT 'ttyd'"
                    )
                )
            if "connect_url" not in term_cols:
                conn.execute(
                    text(
                        "ALTER TABLE remote_terminal_sessions ADD COLUMN connect_url VARCHAR(1024) NOT NULL DEFAULT ''"
                    )
                )
        except Exception:
            # 表不存在时忽略（首次启动由 create_all 创建）
            pass

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
            "Missing LLM configuration. Plan Mode is unavailable.\n"
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


def _add_goal_created_event(s, goal: Goal, *, agent: str = "ui") -> None:
    s.add(
        Event(
            kind="goal.created",
            agent=agent,
            task_id=None,
            payload={"goal_id": int(goal.id), "title": str(goal.title or "")},
        )
    )


def _add_task_created_event(s, task: Task, *, agent: str = "ui") -> None:
    s.add(
        Event(
            kind="task.created",
            agent=agent,
            task_id=str(task.public_id or ""),
            payload={
                "goal_id": int(task.goal_id),
                "task_public_id": str(task.public_id or ""),
                "title": str(task.title or ""),
            },
        )
    )


def _openfocus_data_root() -> Path:
    env_path = str(os.environ.get("OPENFOCUS_DB_PATH") or "").strip()
    if env_path:
        try:
            return Path(env_path).expanduser().resolve().parent
        except Exception:
            pass
    return APP_DIR.parent / ".data"


def _inspiration_files_root() -> Path:
    root = _openfocus_data_root() / "inspirations"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _inspiration_space_files_dir(space_id: int) -> Path:
    path = _inspiration_files_root() / f"space_{int(space_id)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _inspiration_workspace_path(space: InspirationSpace | None, space_id: int) -> Path:
    raw = str(getattr(space, "workspace_path", "") or "").strip()
    if raw:
        p = Path(raw).expanduser()
    else:
        p = _inspiration_space_files_dir(int(space_id))
    p.mkdir(parents=True, exist_ok=True)
    (p / "resources").mkdir(parents=True, exist_ok=True)
    return p


def _inspiration_resources_dir(space: InspirationSpace | None, space_id: int) -> Path:
    p = _inspiration_workspace_path(space, int(space_id)) / "resources"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_resource_filename(name: str, fallback: str) -> str:
    raw = str(name or "").strip() or str(fallback or "resource")
    raw = re.sub(r"[\\/\x00-\x1f]+", "-", raw)
    raw = re.sub(r"\s+", " ", raw).strip().strip(".")
    if not raw:
        raw = str(fallback or "resource")
    return raw[:160]


def _inspiration_resource_file_path(
    *, space: InspirationSpace | None, space_id: int, seq_id: int, name: str, ext: str
) -> Path:
    suffix = ext if str(ext or "").startswith(".") else f".{str(ext or 'txt')}"
    stem = _safe_resource_filename(name, f"resource_{int(seq_id)}")
    return (
        _inspiration_resources_dir(space, int(space_id))
        / f"resource_{int(seq_id)}_{stem}{suffix}"
    )


def _inspiration_write_resource_file(
    resource: InspirationResource, space: InspirationSpace | None = None
) -> None:
    kind = str(getattr(resource, "type", "") or "text").strip().lower()
    sid = int(getattr(resource, "space_id", 0) or 0)
    seq = int(getattr(resource, "resource_seq_id", 0) or 0)
    if sid <= 0 or seq <= 0 or kind == "image":
        return
    name = str(getattr(resource, "name", "") or f"resource-{seq}")
    if kind == "url":
        path = _inspiration_resource_file_path(
            space=space, space_id=sid, seq_id=seq, name=name, ext=".url.md"
        )
        url = str(getattr(resource, "url_content", "") or "").strip()
        body = f"# {name}\n\nURL: {url}\n"
    else:
        path = _inspiration_resource_file_path(
            space=space, space_id=sid, seq_id=seq, name=name, ext=".md"
        )
        body = str(getattr(resource, "text_content", "") or "")
    path.write_text(body, encoding="utf-8")
    resource.file_path = str(path)
    resource.external_path = str(
        path.relative_to(_inspiration_workspace_path(space, sid))
    )


def _inspiration_create_initial_note_resource(
    s, space: InspirationSpace, *, title: str, first_note: str
) -> InspirationResource:
    sid = int(space.id)
    clean_title = str(title or "Inspiration").strip() or "Inspiration"
    clean_note = str(first_note or "").strip()
    body = f"# {clean_title}\n"
    if clean_note:
        body += f"\n{clean_note}\n"
    resource = InspirationResource(
        space_id=sid,
        resource_seq_id=_inspiration_next_resource_seq(s, sid),
        type="text",
        name="First Note",
        text_content=body[:20000],
        source="create_space",
        is_system_generated=True,
    )
    s.add(resource)
    s.flush()
    _inspiration_write_resource_file(resource, space)
    s.add(resource)
    return resource


async def _inspiration_store_uploaded_resource_file(
    *, space_id: int, seq_id: int, file: UploadFile
) -> tuple[Path, str]:
    original_name = str(file.filename or "image")
    ext = Path(original_name).suffix or ".bin"
    target_dir = _inspiration_resources_dir(None, int(space_id))
    target_path = target_dir / f"resource_{int(seq_id)}{ext}"
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    target_path.write_bytes(content)
    return target_path, original_name[:512]


def _guess_media_type(path: Path) -> str:
    guessed, _enc = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _inspiration_resource_reference(res: InspirationResource) -> str:
    rid = int(getattr(res, "resource_seq_id", 0) or 0)
    name = str(getattr(res, "name", "") or f"resource-{rid}")
    kind = str(getattr(res, "type", "") or "resource")
    hint = ""
    if kind == "url":
        hint = str(getattr(res, "url_content", "") or "").strip()
    elif kind == "text":
        body = str(getattr(res, "text_content", "") or "").strip()
        hint = body[:160] + ("…" if len(body) > 160 else "")
    elif kind == "summary":
        body = str(getattr(res, "text_content", "") or "").strip()
        hint = body[:160] + ("…" if len(body) > 160 else "")
    else:
        hint = "Use this image as supporting context."
    return (f"[Resource #{rid}]\nName: {name}\nType: {kind}\nHint: {hint}").strip()


def _inspiration_resource_preview(res: InspirationResource) -> str:
    kind = str(getattr(res, "type", "") or "")
    if kind == "url":
        return str(getattr(res, "url_content", "") or "").strip()
    return str(getattr(res, "text_content", "") or "").strip()


def _inspiration_sync_draft_summary_file(
    s, space: InspirationSpace
) -> InspirationResource | None:
    """Sync resources/draft_summary.md into a Summary resource.

    terminal agent 是“不受信协作者”：这里只把文件作为资源导入，不创建 Goal/Task。
    """

    sid = int(space.id)
    path = _inspiration_resources_dir(space, sid) / "draft_summary.md"
    if not path.exists() or not path.is_file():
        return None
    try:
        text_body = path.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"failed to read draft_summary.md: {e}"
        )
    text_body = str(text_body or "").strip()
    if not text_body:
        raise HTTPException(status_code=400, detail="draft_summary.md is empty")

    existing = (
        s.query(InspirationResource)
        .filter(InspirationResource.space_id == sid)
        .filter(InspirationResource.deleted_at.is_(None))
        .filter(InspirationResource.type == "summary")
        .filter(InspirationResource.name.in_(["Summary", "Draft Summary"]))
        .order_by(InspirationResource.id.desc())
        .first()
    )
    if existing is None:
        existing = InspirationResource(
            space_id=sid,
            resource_seq_id=_inspiration_next_resource_seq(s, sid),
            type="summary",
            name="Summary",
            source="terminal_agent",
            is_system_generated=True,
        )
        s.add(existing)
    existing.name = "Summary"
    existing.text_content = text_body[:20000]
    existing.file_path = str(path)
    existing.external_path = "resources/draft_summary.md"
    existing.source = "terminal_agent"
    existing.is_system_generated = True
    existing.updated_at = _utcnow()
    space.last_activity_at = _utcnow()
    s.flush()
    return existing


def _inspiration_resource_name_from_path(path: Path) -> str:
    name = path.name
    if name == "draft_summary.md":
        return "Summary"
    if name.endswith(".url.md"):
        name = name[: -len(".url.md")]
    else:
        name = path.stem
    name = re.sub(r"^resource_\d+_?", "", name).strip() or path.stem
    return name[:512]


def _inspiration_parse_url_resource_file(path: Path) -> str:
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    for line in body.splitlines():
        cleaned = str(line or "").strip()
        if cleaned.lower().startswith("url:"):
            return cleaned.split(":", 1)[1].strip()[:4000]
        if cleaned.startswith("http://") or cleaned.startswith("https://"):
            return cleaned[:4000]
    return ""


def _inspiration_sync_resources_dir(
    s, space: InspirationSpace
) -> list[InspirationResource]:
    """Refresh InspirationResource rows from files under workspace/resources/."""

    sid = int(space.id)
    workspace = _inspiration_workspace_path(space, sid)
    resources_dir = _inspiration_resources_dir(space, sid)
    synced: list[InspirationResource] = []
    paths = sorted(p for p in resources_dir.rglob("*") if p.is_file())
    for path in paths:
        try:
            external_path = str(path.relative_to(workspace))
        except Exception:
            continue
        if not external_path.startswith("resources/"):
            continue
        is_draft_summary = external_path == "resources/draft_summary.md"
        media_type = _guess_media_type(path)
        if is_draft_summary:
            kind = "summary"
        elif str(media_type or "").startswith("image/"):
            kind = "image"
        elif path.name.endswith(".url.md"):
            kind = "url"
        else:
            kind = "text"

        body = ""
        url = ""
        if kind in {"text", "summary"}:
            try:
                body = path.read_text(encoding="utf-8", errors="replace")[:20000]
            except Exception:
                continue
            if is_draft_summary and not str(body or "").strip():
                continue
        elif kind == "url":
            url = _inspiration_parse_url_resource_file(path)

        existing = (
            s.query(InspirationResource)
            .filter(InspirationResource.space_id == sid)
            .filter(InspirationResource.external_path == external_path)
            .order_by(InspirationResource.id.desc())
            .first()
        )
        if existing is None and is_draft_summary:
            existing = (
                s.query(InspirationResource)
                .filter(InspirationResource.space_id == sid)
                .filter(InspirationResource.type == "summary")
                .filter(InspirationResource.name.in_(["Summary", "Draft Summary"]))
                .order_by(InspirationResource.id.desc())
                .first()
            )
        if existing is None:
            existing = InspirationResource(
                space_id=sid,
                resource_seq_id=_inspiration_next_resource_seq(s, sid),
                type=kind,
                name=_inspiration_resource_name_from_path(path),
                source="terminal_agent",
                is_system_generated=is_draft_summary,
            )
            s.add(existing)
        existing.type = kind
        existing.file_path = str(path)
        existing.external_path = external_path
        existing.deleted_at = None
        existing.updated_at = _utcnow()
        if is_draft_summary:
            existing.name = "Summary"
            existing.source = "terminal_agent"
            existing.is_system_generated = True
        elif not str(existing.source or "").strip():
            existing.source = "terminal_agent"
        if kind in {"text", "summary"}:
            existing.text_content = body
            existing.url_content = ""
        elif kind == "url":
            existing.url_content = url
            existing.text_content = ""
        else:
            existing.text_content = ""
            existing.url_content = ""
        synced.append(existing)
    if synced:
        space.last_activity_at = _utcnow()
    s.flush()
    return synced


def _inspiration_non_deleted_resources(
    s, space_id: int, *, include_summary: bool = True
) -> list[InspirationResource]:
    q = (
        s.query(InspirationResource)
        .filter(InspirationResource.space_id == int(space_id))
        .filter(InspirationResource.deleted_at.is_(None))
    )
    if not include_summary:
        q = q.filter(InspirationResource.type != "summary")
    return q.order_by(
        InspirationResource.updated_at.desc(), InspirationResource.id.desc()
    ).all()


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


def _inspiration_context_lines(
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
    *,
    max_messages: int = 18,
) -> str:
    lines = [f"Space title: {space.title}"]
    if resources:
        lines.append("Resources:")
        for res in resources[:20]:
            lines.append(
                f"- {_inspiration_resource_reference(res).replace(chr(10), ' | ')}"
            )
    if messages:
        lines.append("Conversation:")
        for msg in messages[-max_messages:]:
            role = str(getattr(msg, "role", "assistant") or "assistant")
            body = str(getattr(msg, "content", "") or "").strip()
            if not body:
                continue
            lines.append(f"{role}: {body}")
    return "\n".join(lines)[:16000]


def _inspiration_fallback_reply(space: InspirationSpace, user_text: str) -> str:
    body = str(user_text or "").strip()
    if body.startswith("/draft_goal_tasks"):
        return "I created a fallback draft from the current discussion. Review it and refine in chat if needed."
    if body.startswith("/summary_title"):
        return "I suggested a few title options based on the current discussion."
    return (
        f"I noted your update about '{space.title}'. "
        "What is the most important outcome, constraint, or success signal we should clarify next?"
    )


def _inspiration_fallback_title_suggestions(
    space: InspirationSpace, messages: list[InspirationMessage]
) -> list[str]:
    base = str(space.title or "Inspiration").strip() or "Inspiration"
    latest = ""
    for msg in reversed(messages):
        if str(getattr(msg, "role", "") or "") == "user":
            latest = str(getattr(msg, "content", "") or "").strip()
            if latest:
                break
    suggestions = [base]
    if latest:
        suggestions.append(
            _truncate_zh(latest.replace("/summary_title", "").strip() or base, 20)
        )
    suggestions.append(_truncate_zh(base + " / Refined", 20))
    out: list[str] = []
    for item in suggestions:
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in out:
            out.append(cleaned[:80])
    return out[:3] or [base]


def _inspiration_fallback_draft(
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
) -> dict:
    context = _inspiration_context_lines(space, messages, resources)
    desc = context[:1800]
    tasks = [
        {
            "title": f"Clarify the scope of {space.title}",
            "description": "Define the expected outcome, non-goals, and constraints.",
        },
        {
            "title": f"Draft an execution approach for {space.title}",
            "description": "Turn the discussion into an actionable plan with key milestones.",
        },
        {
            "title": f"Review risks and open questions for {space.title}",
            "description": "List unresolved questions and confirm the next decision points.",
        },
    ]
    return {
        "goal_title": space.title,
        "goal_description": desc,
        "tasks": tasks,
        "open_questions": ["Which part should be implemented first?"],
        "rejected_or_deferred_ideas": [],
    }


def _inspiration_llm_reply(
    provider: OpenAICompatibleProvider,
    *,
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
) -> str:
    convo = [
        {
            "role": "system",
            "content": (
                "You are OpenFocus Inspiration assistant. "
                "Be a proactive planning partner. Ask one clarifying question or provide one concrete synthesis. "
                'Return strict JSON only: {"message":"..."}.'
            ),
        },
        {
            "role": "user",
            "content": _inspiration_context_lines(space, messages, resources),
        },
    ]
    data = json.loads(
        provider.chat_completions(
            messages=convo,
            temperature=0.2,
            max_tokens=500,
            response_format={"type": "json_object"},
        ).content
    )
    return str(
        data.get("message") or "Please tell me more about the desired outcome."
    ).strip()


def _inspiration_llm_title_suggestions(
    provider: OpenAICompatibleProvider,
    *,
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
) -> list[str]:
    convo = [
        {
            "role": "system",
            "content": (
                "You generate concise English or Chinese titles for an inspiration workspace. "
                'Return strict JSON only: {"titles":["...","...","..."]}. '
                "Each title should be <= 80 chars, distinct, and useful as a workspace title."
            ),
        },
        {
            "role": "user",
            "content": _inspiration_context_lines(space, messages, resources),
        },
    ]
    data = json.loads(
        provider.chat_completions(
            messages=convo,
            temperature=0.3,
            max_tokens=300,
            response_format={"type": "json_object"},
        ).content
    )
    out: list[str] = []
    for item in data.get("titles") or []:
        title = str(item or "").strip()
        if title and title not in out:
            out.append(title[:80])
    return out[:5]


def _inspiration_llm_draft(
    provider: OpenAICompatibleProvider,
    *,
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
) -> dict:
    convo = [
        {
            "role": "system",
            "content": (
                "You are OpenFocus Inspiration planning assistant. "
                "Generate a publish-ready draft from the discussion. "
                "Return strict JSON only with keys: goal_title, goal_description, tasks, open_questions, rejected_or_deferred_ideas. "
                "tasks must be an array of objects; each task object must include title and description."
            ),
        },
        {
            "role": "user",
            "content": _inspiration_context_lines(space, messages, resources),
        },
    ]
    data = json.loads(
        provider.chat_completions(
            messages=convo,
            temperature=0.1,
            max_tokens=1400,
            response_format={"type": "json_object"},
        ).content
    )
    tasks: list[dict] = []
    for raw in data.get("tasks") or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        tasks.append(
            {
                "title": title[:512],
                "description": str(raw.get("description") or "").strip()[:4000],
            }
        )
    return {
        "goal_title": str(data.get("goal_title") or space.title).strip()[:2000],
        "goal_description": str(data.get("goal_description") or "").strip()[:4000],
        "tasks": tasks,
        "open_questions": [
            str(x).strip()[:500]
            for x in (data.get("open_questions") or [])
            if str(x or "").strip()
        ][:20],
        "rejected_or_deferred_ideas": [
            str(x).strip()[:500]
            for x in (data.get("rejected_or_deferred_ideas") or [])
            if str(x or "").strip()
        ][:20],
    }


def _inspiration_make_phase_summary(
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
) -> str:
    recent_user = [
        str(m.content or "").strip()
        for m in messages[-20:]
        if str(getattr(m, "role", "") or "") == "user" and str(m.content or "").strip()
    ]
    resource_names = [
        str(r.name or f"resource-{r.resource_seq_id}") for r in resources[:8]
    ]
    lines = [f"Space: {space.title}"]
    if recent_user:
        lines.append("Recent user points:")
        lines.extend(f"- {item[:200]}" for item in recent_user[-6:])
    if resource_names:
        lines.append("Resources in use:")
        lines.extend(f"- {name}" for name in resource_names)
    return "\n".join(lines)


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


def _inspiration_next_resource_seq(s, space_id: int) -> int:
    rows = (
        s.query(InspirationResource.resource_seq_id)
        .filter(InspirationResource.space_id == int(space_id))
        .order_by(InspirationResource.resource_seq_id.desc())
        .first()
    )
    try:
        return int((rows[0] if rows else 0) or 0) + 1
    except Exception:
        return 1


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


def _inspiration_build_published_summary(
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


def _inspiration_clone_resource(
    *, s, source: InspirationResource, target_space_id: int, seq_id: int
) -> InspirationResource:
    cloned = InspirationResource(
        space_id=int(target_space_id),
        resource_seq_id=int(seq_id),
        type=str(source.type or "text"),
        name=str(source.name or f"resource-{seq_id}"),
        text_content=str(source.text_content or ""),
        url_content=str(source.url_content or ""),
        file_path="",
        external_path="",
        source=str(getattr(source, "source", "") or "user"),
        is_system_generated=bool(source.is_system_generated),
    )
    if str(source.file_path or "").strip():
        src_path = Path(str(source.file_path or "")).expanduser()
        if src_path.exists() and src_path.is_file():
            target_dir = _inspiration_resources_dir(None, int(target_space_id))
            ext = src_path.suffix or ""
            dst = target_dir / f"resource_{int(seq_id)}{ext}"
            shutil.copyfile(src_path, dst)
            cloned.file_path = str(dst)
            try:
                cloned.external_path = str(
                    dst.relative_to(
                        _inspiration_workspace_path(None, int(target_space_id))
                    )
                )
            except Exception:
                cloned.external_path = str(dst)
    elif str(cloned.type or "") in {"url", "text", "summary"}:
        _inspiration_write_resource_file(cloned, None)
    s.add(cloned)
    s.flush()
    return cloned


def _inspiration_space_payload(
    space: InspirationSpace,
    *,
    latest_draft: InspirationDraft | None = None,
    resource_count: int | None = None,
    draft_count: int | None = None,
    publish_count: int | None = None,
) -> dict:
    return {
        "id": int(space.id),
        "title": str(space.title or ""),
        "status": str(space.status or "open"),
        "mode": str(getattr(space, "mode", "") or "built_in"),
        "workspace_path": str(getattr(space, "workspace_path", "") or ""),
        "published_goal_id": (
            int(space.published_goal_id)
            if getattr(space, "published_goal_id", None)
            else None
        ),
        "forked_from_space_id": (
            int(space.forked_from_space_id)
            if getattr(space, "forked_from_space_id", None)
            else None
        ),
        "message_turn_count": int(space.message_turn_count or 0),
        "resource_count": int(resource_count or 0),
        "draft_count": int(draft_count or 0),
        "publish_count": int(publish_count or 0),
        "latest_draft_version": (
            int(latest_draft.version) if latest_draft is not None else None
        ),
        "created_at": space.created_at.isoformat() if space.created_at else None,
        "updated_at": space.updated_at.isoformat() if space.updated_at else None,
        "last_activity_at": (
            space.last_activity_at.isoformat()
            if getattr(space, "last_activity_at", None)
            else None
        ),
        "closed_at": space.closed_at.isoformat()
        if getattr(space, "closed_at", None)
        else None,
        "published_at": (
            space.published_at.isoformat()
            if getattr(space, "published_at", None)
            else None
        ),
    }


def _inspiration_message_payload(message: InspirationMessage) -> dict:
    return {
        "id": int(message.id),
        "space_id": int(message.space_id),
        "role": str(message.role or "assistant"),
        "kind": str(message.kind or "message"),
        "content": str(message.content or ""),
        "payload": message.payload or {},
        "draft_version": (
            int(message.draft_version)
            if getattr(message, "draft_version", None)
            else None
        ),
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def _inspiration_resource_payload(
    space_id: int, resource: InspirationResource, *, include_text: bool = False
) -> dict:
    file_path = str(resource.file_path or "").strip()
    return {
        "id": int(resource.id),
        "space_id": int(resource.space_id),
        "resource_seq_id": int(resource.resource_seq_id),
        "type": str(resource.type or "text"),
        "name": str(resource.name or f"resource-{int(resource.resource_seq_id or 0)}"),
        "preview": _inspiration_resource_preview(resource),
        "reference": _inspiration_resource_reference(resource),
        "url_content": str(resource.url_content or ""),
        "text_content": str(resource.text_content or "") if include_text else "",
        "external_path": str(getattr(resource, "external_path", "") or ""),
        "source": str(getattr(resource, "source", "") or "user"),
        "is_system_generated": bool(resource.is_system_generated),
        "has_file": bool(file_path),
        "raw_url": (
            f"/api/inspirations/{int(space_id)}/resources/{int(resource.id)}/raw"
            if file_path
            else None
        ),
        "created_at": resource.created_at.isoformat() if resource.created_at else None,
        "updated_at": resource.updated_at.isoformat() if resource.updated_at else None,
    }


def _inspiration_draft_payload(draft: InspirationDraft) -> dict:
    return {
        "id": int(draft.id),
        "space_id": int(draft.space_id),
        "version": int(draft.version),
        "goal_title": str(draft.goal_title or ""),
        "goal_description": str(draft.goal_description or ""),
        "tasks": draft.tasks or [],
        "open_questions": draft.open_questions or [],
        "rejected_or_deferred_ideas": draft.rejected_or_deferred_ideas or [],
        "source_message_id": (
            int(draft.source_message_id)
            if getattr(draft, "source_message_id", None)
            else None
        ),
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
    }


def _inspiration_publish_record_payload(record: InspirationPublishRecord) -> dict:
    return {
        "id": int(record.id),
        "space_id": int(record.space_id),
        "draft_id": int(record.draft_id),
        "created_goal_id": int(record.created_goal_id),
        "created_task_ids": [int(x) for x in (record.created_task_ids or [])],
        "deferred_tasks": record.deferred_tasks or [],
        "summary_resource_id": (
            int(record.summary_resource_id)
            if getattr(record, "summary_resource_id", None)
            else None
        ),
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


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
    with session_scope() as s:
        space = _inspiration_space_or_404(s, int(space_id))
        current_status = str(space.status or "open")
        if current_status not in {"open", "closed"}:
            raise HTTPException(
                status_code=400, detail="This space cannot be published"
            )
        if _inspiration_is_waiting(s, int(space_id)):
            raise HTTPException(status_code=409, detail="Agent is still responding")
        draft: InspirationDraft | None
        if draft_id is None:
            draft = _inspiration_latest_draft(s, int(space_id))
        else:
            draft = s.get(InspirationDraft, int(draft_id))
            if draft is not None and int(draft.space_id) != int(space_id):
                draft = None
        if draft is None:
            raise HTTPException(
                status_code=400, detail="No draft is available for publishing"
            )
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
        if not picked_tasks:
            raise HTTPException(
                status_code=400, detail="The draft does not contain publishable tasks"
            )
        space.status = "publishing"
        space.last_activity_at = _utcnow()
        return {
            "draft_id": int(draft.id),
            "previous_status": current_status,
            "due_date": due_date.isoformat(),
        }


def _inspiration_load_publish_snapshot(space_id: int, draft_id: int) -> dict:
    with session_scope() as s:
        space = _inspiration_space_or_404(s, int(space_id))
        if str(space.status or "") != "publishing":
            raise RuntimeError("Inspiration space is not in publishing state")
        draft = s.get(InspirationDraft, int(draft_id))
        if draft is None or int(draft.space_id) != int(space_id):
            raise RuntimeError("Draft not found during publishing")

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
        if not picked_tasks:
            raise RuntimeError("The draft does not contain publishable tasks")

        return {
            "space_title": str(space.title or "").strip(),
            "goal_title": str(draft.goal_title or space.title).strip()[:2000]
            or space.title,
            "goal_description": str(draft.goal_description or "").strip()[:4000],
            "picked_tasks": picked_tasks,
            "draft_payload": _inspiration_draft_payload(draft),
        }


def _inspiration_publish_sync(
    *,
    space_id: int,
    draft_id: int,
    due_date_iso: str,
    previous_status: str,
) -> None:
    due_date = dt.date.fromisoformat(str(due_date_iso))
    created_goal_id = 0
    created_task_ids: list[int] = []
    draft_payload: dict | None = None
    try:
        publish_snapshot = _inspiration_load_publish_snapshot(
            int(space_id), int(draft_id)
        )
        picked_tasks = list(publish_snapshot.get("picked_tasks") or [])
        draft_payload = publish_snapshot.get("draft_payload") or None
        goal_title = str(publish_snapshot.get("goal_title") or "").strip()
        goal_content = str(publish_snapshot.get("goal_description") or "").strip()

        with session_scope() as s:
            space = _inspiration_space_or_404(s, int(space_id))
            if str(space.status or "") != "publishing":
                raise RuntimeError("Inspiration space is not in publishing state")
            draft = s.get(InspirationDraft, int(draft_id))
            if draft is None or int(draft.space_id) != int(space_id):
                raise RuntimeError("Draft not found during publishing")
            goal = Goal(
                title=goal_title,
                content=goal_content,
                due_date=due_date,
                source_inspiration_space_id=int(space_id),
                source_inspiration_draft_id=int(draft.id),
            )
            s.add(goal)
            s.flush()
            _add_goal_created_event(s, goal, agent="inspiration")
            created_goal_id = int(goal.id)

            created_tasks: list[Task] = []
            for idx, item in enumerate(picked_tasks):
                title = str(item.get("title") or "").strip()
                description = str(item.get("description") or "").strip()
                task_type = _infer_task_type(title, description)
                estimated_minutes = _infer_estimated_minutes(
                    task_type, title, description
                )
                context_key = _infer_context_key(
                    title,
                    description,
                    goal_id=int(goal.id),
                )
                task = Task(
                    goal_id=int(goal.id),
                    title=title,
                    content=description,
                    status="todo",
                    task_type=task_type,
                    estimated_minutes=estimated_minutes,
                    context_key=context_key,
                    source_inspiration_space_id=int(space_id),
                    source_inspiration_draft_id=int(draft.id),
                )
                s.add(task)
                s.flush()
                _add_task_created_event(s, task, agent="inspiration")
                created_tasks.append(task)
                created_task_ids.append(int(task.id))

            summary_text = _inspiration_build_published_summary(
                space=space,
                draft=draft,
                goal=goal,
                created_tasks=created_tasks,
                deferred_tasks=[],
            )
            seq_id = _inspiration_next_resource_seq(s, int(space_id))
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
            _inspiration_write_resource_file(summary_resource, space)
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
            space.status = "published"
            space.published_goal_id = int(goal.id)
            space.published_at = _utcnow()
            space.last_activity_at = _utcnow()
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
                space.last_activity_at = _utcnow()
                s.add(
                    InspirationMessage(
                        space_id=int(space_id),
                        role="assistant",
                        kind="error",
                        content=f"Failed to publish the draft: {str(e)}",
                        payload={"error": str(e), "draft_id": int(draft_id)},
                    )
                )
        _try_audit_memory(
            kind="inspiration.publish_error",
            source="web",
            summary=f"Failed publishing inspiration space {int(space_id)}.",
            detail=str(e),
            metadata={"space_id": int(space_id), "draft_id": int(draft_id)},
        )
        return

    _try_audit_memory(
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
        is_published = space is not None and str(space.status or "") == "published"
    if is_published:
        await _inspiration_release_terminals(int(space_id))


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


def _infer_task_type(title: str, description: str) -> str:
    text = f"{title}\n{description}".lower()
    if any(
        k in text
        for k in [
            "review",
            "approve",
            "comment",
            "code review",
            "qa",
            "test report",
            "验收",
            "评审",
            "reviewer",
            " pr",
            " mr",
        ]
    ):
        return "review"
    if any(
        k in text
        for k in [
            "sync",
            "meeting",
            "reply",
            "email",
            "message",
            "call",
            "沟通",
            "对齐",
            "联系",
            "回复",
            "会议",
        ]
    ):
        return "communication"
    if any(
        k in text
        for k in [
            "admin",
            "ops",
            "cleanup",
            "organize",
            "docs",
            "document",
            "整理",
            "记录",
            "文档",
            "行政",
        ]
    ):
        return "admin"
    if any(
        k in text
        for k in [
            "design",
            "investigate",
            "analysis",
            "analyze",
            "refactor",
            "architecture",
            "research",
            "规划",
            "设计",
            "排查",
            "分析",
            "重构",
        ]
    ):
        return "deep_work"
    return "execution"


def _infer_estimated_minutes(task_type: str, title: str, description: str) -> int:
    text = f"{title}\n{description}".lower()
    m = re.search(
        r"(\d{1,3})\s*(minutes?|mins?|min|小时|小時|hour|hours|hr|hrs|h|分钟|分鐘)",
        text,
    )
    if m:
        try:
            num = max(5, min(240, int(m.group(1))))
            unit = m.group(2)
            if unit in {"小时", "小時", "hour", "hours", "hr", "hrs", "h"}:
                return min(240, num * 60)
            return num
        except Exception:
            pass
    if re.search(
        r"\b(quick|small|tiny|minor|trivial|fast|马上|快速|小改|顺手)\b", text
    ):
        return 20
    if task_type == "review":
        return 25
    if task_type == "communication":
        return 20
    if task_type == "admin":
        return 15
    if task_type == "deep_work":
        return 90
    return 45


def _infer_context_key(
    title: str, description: str, *, goal_id: int, root_path: str | None = None
) -> str:
    rp = str(root_path or "").strip()
    if rp:
        try:
            name = Path(rp).name.strip().lower()
            if name:
                return f"space:{name[:80]}"
        except Exception:
            pass
    text = f"{title}\n{description}".lower()
    m = re.search(r"([a-z0-9_.-]+/[a-z0-9_.-]+)", text)
    if m:
        return f"topic:{m.group(1)[:80]}"
    tokens = [
        x for x in re.split(r"[^a-zA-Z0-9\u4e00-\u9fff]+", text) if len(x.strip()) >= 2
    ]
    seed = (tokens[0] if tokens else "")[:32].strip().lower()
    if seed:
        return f"goal:{goal_id}:{seed}"
    return f"goal:{goal_id}"


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
    now = _utcnow()
    daily_path = _memory_daily_root() / f"{now.date().isoformat()}.md"
    with _MEMORY_LOCK:
        existing_daily = _memory_read_text(daily_path)
        if note and note not in existing_daily:
            prefix = (
                "\n## Next Move Feedback\n\n"
                if "## Next Move Feedback" not in existing_daily
                else "\n"
            )
            _memory_append_text(daily_path, prefix + note + "\n")
        if memory_note:
            long_term_path = _memory_long_term_path()
            existing_long_term = _memory_read_text(long_term_path)
            if memory_note not in existing_long_term:
                prefix = (
                    "\n## Learned Preferences\n\n"
                    if "## Learned Preferences" not in existing_long_term
                    else "\n"
                )
                _memory_append_text(long_term_path, prefix + memory_note + "\n")


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
    plan_mode: str | None = Form(default=None),
) -> RedirectResponse:
    # Server-side guard: if Plan Mode is ON but JS didn't repoint form action,
    # we should still enter Plan flow instead of creating the goal directly.
    pm = str(plan_mode or "").strip().lower()
    if pm in {"1", "true", "on", "yes"}:
        return await goal_plan_create_session(
            due_date=due_date,
            title=title,
            content=content,
            draft_content=None,
        )

    parsed_due = dt.date.fromisoformat(due_date)
    created_goal_id = 0
    with session_scope() as s:
        goal = Goal(
            title=title.strip(),
            content=content.strip(),
            due_date=parsed_due,
        )
        s.add(goal)
        s.flush()
        _add_goal_created_event(s, goal, agent="ui")
        created_goal_id = int(goal.id or 0)
    _try_audit_memory(
        kind="goal.created",
        source="web",
        summary=f"Created goal: {title.strip()}",
        detail=f"Goal title:\n\n{title.strip()}\n\nContent:\n\n{content.strip()}",
        goal_id=created_goal_id or None,
        metadata={"due_date": parsed_due.isoformat()},
    )
    return RedirectResponse(
        url=f"/goals?goal={created_goal_id}&tab=tasks", status_code=303
    )


@app.post("/goals/{goal_id:int}/tasks", include_in_schema=False)
def tasks_create(
    goal_id: int,
    title: str = Form(..., min_length=1, max_length=512),
    content: str = Form(..., min_length=1, max_length=4000),
) -> RedirectResponse:
    created_task_id = ""
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        title_text = title.strip()
        content_text = content.strip()
        task_type = _infer_task_type(title_text, content_text)
        estimated_minutes = _infer_estimated_minutes(
            task_type, title_text, content_text
        )
        context_key = _infer_context_key(title_text, content_text, goal_id=goal_id)
        task = Task(
            goal_id=goal_id,
            title=title_text,
            content=content_text,
            status="todo",
            task_type=task_type,
            estimated_minutes=estimated_minutes,
            context_key=context_key,
        )
        s.add(task)
        s.flush()
        _add_task_created_event(s, task, agent="ui")
        created_task_id = str(task.public_id or "")
    _try_audit_memory(
        kind="task.created",
        source="web",
        summary=f"Created task: {title_text}",
        detail=f"Task title:\n\n{title_text}\n\nContent:\n\n{content_text}",
        goal_id=goal_id,
        task_public_id=created_task_id or None,
        metadata={
            "task_type": task_type,
            "estimated_minutes": estimated_minutes,
            "context_key": context_key,
        },
    )
    return RedirectResponse(url=f"/goals?goal={goal_id}&tab=tasks", status_code=303)


@app.post("/goals/{goal_id:int}/done", include_in_schema=False)
def goals_mark_done(goal_id: int) -> RedirectResponse:
    """将 Goal 标记为已完成（人工行为）。"""

    with session_scope() as s:
        g = s.get(Goal, goal_id)
        if g is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        if (g.status or "").strip() != "done":
            old = (g.status or "").strip() or "active"
            g.status = "done"
            s.add(
                Event(
                    kind="goal.confirmed_done_by_user",
                    agent="ui",
                    task_id=None,
                    payload={"goal_id": int(goal_id), "from": old},
                )
            )
            _try_audit_memory(
                kind="goal.finished",
                source="web",
                summary=f"Finished goal: {g.title}",
                detail=f"Goal moved from `{old}` to `done`.",
                goal_id=int(goal_id),
                metadata={"from": old, "to": "done"},
            )
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/goals/{goal_id:int}/reopen", include_in_schema=False)
def goals_reopen(goal_id: int) -> RedirectResponse:
    """将已完成的 Goal 重新打开（人工行为）。"""

    with session_scope() as s:
        g = s.get(Goal, goal_id)
        if g is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        if (g.status or "").strip() == "done":
            g.status = "active"
            s.add(
                Event(
                    kind="goal.reopened_by_user",
                    agent="ui",
                    task_id=None,
                    payload={"goal_id": int(goal_id)},
                )
            )
            _try_audit_memory(
                kind="goal.reopened",
                source="web",
                summary=f"Reopened goal: {g.title}",
                detail="Goal moved from `done` back to `active`.",
                goal_id=int(goal_id),
                metadata={"to": "active"},
            )
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/tasks/{task_id:int}/done", include_in_schema=False)
def tasks_mark_done(task_id: int) -> RedirectResponse:
    with session_scope() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if t.status != "done":
            old = t.status
            t.status = "done"
            t.completed_at = dt.datetime.now(dt.timezone.utc)
            s.add(
                Event(
                    kind="task.confirmed_done",
                    agent="ui",
                    task_id=t.public_id,
                    payload={"from": old},
                )
            )
            _try_audit_memory(
                kind="task.finished",
                source="web",
                summary=f"Finished task: {t.title}",
                detail=f"Task moved from `{old}` to `done`.",
                goal_id=int(t.goal_id),
                task_public_id=t.public_id,
                metadata={"from": old, "to": "done"},
            )
        goal_id = t.goal_id
        task_public_id = t.public_id

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
        t = s.get(Task, task_id)
        if t is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if t.status == "done":
            t.status = "todo"
            t.completed_at = None
            s.add(
                Event(
                    kind="task.reopened",
                    agent="ui",
                    task_id=t.public_id,
                    payload={},
                )
            )
            _try_audit_memory(
                kind="task.reopened",
                source="web",
                summary=f"Reopened task: {t.title}",
                detail="Task moved from `done` back to `todo`.",
                goal_id=int(t.goal_id),
                task_public_id=t.public_id,
                metadata={"to": "todo"},
            )
        goal_id = t.goal_id
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/tasks/{task_id:int}/edit", include_in_schema=False)
def tasks_update(
    task_id: int,
    title: str = Form(..., min_length=1, max_length=512),
    content: str = Form(..., min_length=1, max_length=4000),
) -> RedirectResponse:
    old_title = ""
    old_content = ""
    with session_scope() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise HTTPException(status_code=404, detail="Task not found")
        old_title = str(t.title or "")
        old_content = str(t.content or "")
        title_text = title.strip()
        content_text = content.strip()
        task_type = _infer_task_type(title_text, content_text)
        estimated_minutes = _infer_estimated_minutes(
            task_type, title_text, content_text
        )
        context_key = _infer_context_key(title_text, content_text, goal_id=t.goal_id)
        t.title = title_text
        t.content = content_text
        t.task_type = task_type
        t.estimated_minutes = estimated_minutes
        t.context_key = context_key
        goal_id = t.goal_id
        pid = t.public_id
    _try_audit_memory(
        kind="task.edited",
        source="web",
        summary=f"Edited task: {title_text}",
        detail=(
            f"Previous title: {old_title}\n\n"
            f"Previous content:\n\n{old_content}\n\n"
            f"Updated title: {title_text}\n\n"
            f"Updated content:\n\n{content_text}"
        ),
        goal_id=goal_id,
        task_public_id=pid,
        metadata={
            "task_type": task_type,
            "estimated_minutes": estimated_minutes,
            "context_key": context_key,
        },
    )
    # 保持 Dashboard 选中态
    return RedirectResponse(url=f"/goals?task={pid}&goal={goal_id}", status_code=303)


@app.post("/tasks/{task_id:int}/delete", include_in_schema=False)
def tasks_delete(task_id: int) -> RedirectResponse:
    deleted_title = ""
    deleted_public_id = ""
    with session_scope() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise HTTPException(status_code=404, detail="Task not found")
        goal_id = t.goal_id
        deleted_title = str(t.title or "")
        deleted_public_id = str(t.public_id or "")
        # 清理该 task 绑定的 AgentSpace（若存在）
        space = (
            s.query(AgentSpace)
            .filter(AgentSpace.task_public_id == t.public_id)
            .one_or_none()
        )
        if space is not None:
            # 同时清理 Agent 会话/消息（对话持久化属于 AgentSpace 生命周期）
            sessions = (
                s.query(AgentSession).filter(AgentSession.space_id == space.id).all()
            )
            sess_ids = [ss.session_id for ss in sessions]
            if sess_ids:
                s.query(AgentMessage).filter(
                    AgentMessage.session_id.in_(sess_ids)
                ).delete(synchronize_session=False)
                s.query(AgentSession).filter(
                    AgentSession.session_id.in_(sess_ids)
                ).delete(synchronize_session=False)
            s.delete(space)
        s.delete(t)
    _try_audit_memory(
        kind="task.deleted",
        source="web",
        summary=f"Deleted task: {deleted_title}",
        detail="Task and related AgentSpace resources were deleted.",
        goal_id=goal_id,
        task_public_id=deleted_public_id or None,
        metadata={},
    )
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


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
    old_title = ""
    old_content = ""
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        old_title = str(goal.title or "")
        old_content = str(goal.content or "")
        goal.title = title.strip()
        goal.content = content.strip()
        goal.due_date = parsed_due
        goal.status = status.strip() or "active"
        goal.priority = priority.strip() or "normal"
        goal.importance = importance.strip() or "normal"
    _try_audit_memory(
        kind="goal.edited",
        source="web",
        summary=f"Edited goal: {title.strip()}",
        detail=(
            f"Previous title: {old_title}\n\n"
            f"Previous content:\n\n{old_content}\n\n"
            f"Updated title: {title.strip()}\n\n"
            f"Updated content:\n\n{content.strip()}"
        ),
        goal_id=goal_id,
        metadata={
            "due_date": parsed_due.isoformat(),
            "status": status.strip() or "active",
            "priority": priority.strip() or "normal",
            "importance": importance.strip() or "normal",
        },
    )
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/goals/{goal_id:int}/delete", include_in_schema=False)
def goals_delete(goal_id: int) -> RedirectResponse:
    deleted_title = ""
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        deleted_title = str(goal.title or "")
        # 清理关联 tasks（MVP 先做简单级联）
        s.query(Task).filter(Task.goal_id == goal_id).delete()
        s.delete(goal)
    _try_audit_memory(
        kind="goal.deleted",
        source="web",
        summary=f"Deleted goal: {deleted_title}",
        detail="Goal and its tasks were deleted.",
        goal_id=goal_id,
        metadata={},
    )
    return RedirectResponse(url="/goals", status_code=303)


@app.get("/goals/plan", response_class=HTMLResponse)
def goal_plan_start(request: Request) -> HTMLResponse:
    # Optional prefill from:
    # - /goals/{goal_id}/plan/start when LLM not configured => ?goal_id=
    # - /goals/plan/start POST when LLM not configured => ?draft_content=&due_date=
    default_due_obj = dt.date.today() + dt.timedelta(days=7)
    draft_content = ""

    qp = getattr(request, "query_params", None)
    if qp is not None:
        try:
            # Preserve user input if POST fails due to missing LLM.
            draft_content = str(qp.get("draft_content") or "").strip()
        except Exception:
            draft_content = ""

        try:
            due = str(qp.get("due_date") or "").strip()
            if due:
                default_due_obj = dt.date.fromisoformat(due)
        except Exception:
            pass

        # If coming from a goal, prefill from that goal.
        if not draft_content:
            try:
                gid_raw = str(qp.get("goal_id") or "").strip()
                gid = int(gid_raw) if gid_raw else 0
            except Exception:
                gid = 0
            if gid:
                try:
                    with session_scope() as s:
                        g = s.get(Goal, gid)
                        if g is not None:
                            draft_content = str(g.title or "").strip()
                            if getattr(g, "due_date", None):
                                default_due_obj = g.due_date
                except Exception:
                    pass

    _provider, err = _get_llm_provider_or_error()
    return templates.TemplateResponse(
        request,
        "goal_plan.html",
        {
            "default_due": default_due_obj.isoformat(),
            "draft_content": draft_content,
            "error": err,
        },
    )


def _plan_system_prompt(*, remaining_turns: int) -> str:
    return (
        "You are a Goal planning assistant in Plan Mode.\n"
        "Your job is to clarify the goal through conversation, identify potential goal conflicts and goal relationships, and produce executable tasks.\n"
        "You must return strict JSON only, never Markdown.\n"
        "On each turn you may do exactly one of the following:\n"
        '1) Ask a follow-up question: {"type":"question", "question":"..."}\n'
        '2) Return a final plan: {"type":"final", "goal":{...}, "tasks":[...], "conflicts":[...], "relations":[...]}\n'
        f"Remaining follow-up turns: {remaining_turns}. When remaining_turns <= 0 you must return `final`.\n"
        "`final.goal` must include: title, content, status, priority, importance.\n"
        "Each item in `tasks` must include at least `title` as a string."
    )


def _plan_llm_step(
    *,
    provider: OpenAICompatibleProvider,
    session: GoalPlanSession,
    messages: list[GoalPlanMessage],
    source_goal: Goal | None = None,
    existing_tasks: list[Task] | None = None,
) -> dict:
    remaining = max(0, 3 - session.turns)
    sys = _plan_system_prompt(remaining_turns=remaining)
    convo: list[dict] = [{"role": "system", "content": sys}]
    extra = ""
    if source_goal is not None:
        extra += f"\nCurrent goal: {source_goal.title}\n"
        if source_goal.content:
            extra += f"Goal content: {source_goal.content}\n"
        extra += f"Goal status: {source_goal.status} · priority={source_goal.priority} · importance={source_goal.importance}\n"
    if existing_tasks:
        extra += "\nExisting tasks:\n"
        for t in existing_tasks[:50]:
            extra += f"- [{t.status}] {t.title} (taskId={t.public_id})\n"

    convo.append(
        {
            "role": "user",
            "content": (
                f"Draft goal: {session.draft_content}\nDue date: {session.due_date.isoformat()}\n"
                + extra
            ),
        }
    )
    for m in messages:
        convo.append({"role": m.role, "content": m.content})

    res = provider.chat_completions(
        messages=convo,
        temperature=0.0,
        max_tokens=900,
        response_format={"type": "json_object"},
    )
    import json as _json

    return _json.loads(res.content)


async def _kickoff_plan_session_first_step(session_id: int) -> None:
    """Start the first Plan Session step asynchronously without blocking creation."""

    try:
        provider, err = _get_llm_provider_or_error()
        if provider is None:
            with session_scope() as s:
                sess = s.get(GoalPlanSession, int(session_id))
                if sess is None:
                    return
                sess.status = "error"
                s.add(
                    GoalPlanMessage(
                        session_id=int(session_id),
                        role="assistant",
                        content=str(err or "LLM is not configured"),
                    )
                )
            _try_audit_memory(
                kind="plan.error",
                source="plan_mode",
                summary=f"Plan session {int(session_id)} is blocked by missing LLM.",
                detail=str(err or "LLM is not configured"),
                metadata={"session_id": int(session_id)},
            )
            return

        # Snapshot session/messages for LLM call (avoid holding DB session during network).
        with session_scope() as s:
            sess = s.get(GoalPlanSession, int(session_id))
            if sess is None:
                return
            msgs = (
                s.query(GoalPlanMessage)
                .filter(GoalPlanMessage.session_id == int(session_id))
                .order_by(GoalPlanMessage.id.asc())
                .all()
            )
            sess_snapshot = GoalPlanSession(
                id=sess.id,
                status=sess.status,
                draft_content=sess.draft_content,
                due_date=sess.due_date,
                source_goal_id=sess.source_goal_id,
                turns=sess.turns,
                result_json=sess.result_json,
                created_goal_id=sess.created_goal_id,
            )
            msgs_snapshot = [
                GoalPlanMessage(session_id=m.session_id, role=m.role, content=m.content)
                for m in msgs
            ]

        data = await asyncio.to_thread(
            _plan_llm_step,
            provider=provider,
            session=sess_snapshot,
            messages=msgs_snapshot,
        )

        with session_scope() as s:
            sess = s.get(GoalPlanSession, int(session_id))
            if sess is None:
                return

            if data.get("type") == "question":
                q = (
                    str(data.get("question") or "").strip()
                    or "Please share a bit more detail."
                )
                s.add(
                    GoalPlanMessage(
                        session_id=int(session_id), role="assistant", content=q
                    )
                )
                sess.status = "in_progress"
                _try_audit_memory(
                    kind="plan.assistant_message",
                    source="plan_mode",
                    summary=f"Plan session {int(session_id)} asked a follow-up question.",
                    detail=q,
                    metadata={
                        "session_id": int(session_id),
                        "message_type": "question",
                    },
                )
                return

            # Save the draft result but keep the conversation open for more iteration.
            sess.result_json = data
            sess.status = "in_progress"
            s.add(
                GoalPlanMessage(
                    session_id=int(session_id),
                    role="assistant",
                    content="I generated a draft task breakdown. You can keep refining it here, or click Create to write the goal and tasks.",
                )
            )
            _try_audit_memory(
                kind="plan.draft_generated",
                source="plan_mode",
                summary=f"Plan session {int(session_id)} generated a draft.",
                detail=json.dumps(data, ensure_ascii=False, indent=2),
                metadata={
                    "session_id": int(session_id),
                    "tasks": len(data.get("tasks") or []),
                },
            )
    except Exception as e:
        with session_scope() as s:
            sess = s.get(GoalPlanSession, int(session_id))
            if sess is None:
                return
            sess.status = "error"
            s.add(
                GoalPlanMessage(
                    session_id=int(session_id),
                    role="assistant",
                    content="Failed to start: " + str(e),
                )
            )
        _try_audit_memory(
            kind="plan.error",
            source="plan_mode",
            summary=f"Plan session {int(session_id)} failed to start.",
            detail=str(e),
            metadata={"session_id": int(session_id)},
        )


@app.post("/goals/plan/start", include_in_schema=False)
async def goal_plan_create_session(
    due_date: str = Form(...),
    draft_content: str | None = Form(default=None),
    title: str | None = Form(default=None),
    content: str | None = Form(default=None),
    description: str | None = Form(default=None),
) -> RedirectResponse:
    provider, err = _get_llm_provider_or_error()
    if provider is None:
        from urllib.parse import urlencode

        raw = (draft_content or "").strip()
        if not raw:
            title_text = (title or "").strip()
            content_text = (content or "").strip()
            # Backward compatibility for older callers that posted
            # content=<title>, description=<content>.
            if not title_text and description is not None:
                title_text = content_text
                content_text = (description or "").strip()
            raw = title_text
            if content_text:
                raw = (title_text + "\n\nContent:\n" + content_text).strip()
        _try_audit_memory(
            kind="plan.session_start_requested",
            source="web",
            summary="Requested plan session without available LLM provider.",
            detail=raw,
            metadata={
                "due_date": due_date,
                "error": str(err or "LLM is not configured"),
            },
        )
        qs = urlencode({"draft_content": raw, "due_date": due_date})
        return RedirectResponse(url="/goals/plan?" + qs, status_code=303)

    parsed_due = dt.date.fromisoformat(due_date)
    raw = (draft_content or "").strip()
    if not raw:
        title_text = (title or "").strip()
        content_text = (content or "").strip()
        # Backward compatibility for older callers that posted
        # content=<title>, description=<content>.
        if not title_text and description is not None:
            title_text = content_text
            content_text = (description or "").strip()
        raw = title_text
        if content_text:
            raw = (title_text + "\n\nContent:\n" + content_text).strip()
    if not raw:
        raise HTTPException(status_code=400, detail="draft_content/content is required")
    if len(raw) > 2000:
        raw = raw[:2000]

    with session_scope() as s:
        sess = GoalPlanSession(
            status="starting", draft_content=raw, due_date=parsed_due
        )
        s.add(sess)
        s.flush()
        sid = sess.id
        # Seed the session with an assistant intro message.
        s.add(
            GoalPlanMessage(
                session_id=sid,
                role="assistant",
                content="I will ask a few questions to clarify the goal, then draft an executable task breakdown.",
            )
        )

    _try_audit_memory(
        kind="plan.session_started",
        source="web",
        summary=f"Started plan session {int(sid)}.",
        detail=raw,
        metadata={"session_id": int(sid), "due_date": parsed_due.isoformat()},
    )

    try:
        asyncio.get_running_loop().create_task(
            _kickoff_plan_session_first_step(int(sid))
        )
    except RuntimeError:
        # no running loop (should not happen under uvicorn); best-effort fallback
        pass

    return RedirectResponse(url=f"/goals/plan/{sid}", status_code=303)


@app.get("/goals/plan/{session_id}", response_class=HTMLResponse)
def goal_plan_view(request: Request, session_id: int) -> HTMLResponse:
    with session_scope() as s:
        sess = s.get(GoalPlanSession, session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")
        msgs = (
            s.query(GoalPlanMessage)
            .filter(GoalPlanMessage.session_id == session_id)
            .order_by(GoalPlanMessage.id.asc())
            .all()
        )
    return templates.TemplateResponse(
        request,
        "goal_plan_session.html",
        {
            "session": sess,
            "messages": msgs,
            "created_goal_id": sess.created_goal_id,
        },
    )


@app.post("/goals/plan/{session_id}/reply", include_in_schema=False)
def goal_plan_reply(
    session_id: int, answer: str = Form(..., min_length=1, max_length=20000)
) -> RedirectResponse:
    provider, err = _get_llm_provider_or_error()
    if provider is None:
        return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)

    with session_scope() as s:
        sess = s.get(GoalPlanSession, session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if sess.status != "in_progress":
            return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)
        s.add(
            GoalPlanMessage(session_id=session_id, role="user", content=answer.strip())
        )
        sess.turns += 1
    _try_audit_memory(
        kind="plan.user_reply",
        source="web",
        summary=f"Plan session {int(session_id)} received a user reply.",
        detail=answer.strip(),
        metadata={"session_id": int(session_id)},
    )

    with session_scope() as s:
        sess = s.get(GoalPlanSession, session_id)
        msgs = (
            s.query(GoalPlanMessage)
            .filter(GoalPlanMessage.session_id == session_id)
            .order_by(GoalPlanMessage.id.asc())
            .all()
        )
        source_goal = None
        existing_tasks = None
        if getattr(sess, "source_goal_id", None):
            source_goal = s.get(Goal, sess.source_goal_id)
            existing_tasks = (
                s.query(Task)
                .filter(Task.goal_id == sess.source_goal_id)
                .order_by(Task.id.asc())
                .all()
            )
        data = _plan_llm_step(
            provider=provider,
            session=sess,
            messages=msgs,
            source_goal=source_goal,
            existing_tasks=existing_tasks,
        )

        if data.get("type") == "question":
            q = (
                str(data.get("question") or "").strip()
                or "Please share a bit more detail."
            )
            s.add(GoalPlanMessage(session_id=session_id, role="assistant", content=q))
            _try_audit_memory(
                kind="plan.assistant_message",
                source="plan_mode",
                summary=f"Plan session {int(session_id)} asked a follow-up question.",
                detail=q,
                metadata={"session_id": int(session_id), "message_type": "question"},
            )
            return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)

        # Save the draft result but keep the conversation open for more iteration.
        sess.result_json = data
        sess.status = "in_progress"
        s.add(
            GoalPlanMessage(
                session_id=session_id,
                role="assistant",
                content="I generated a draft task breakdown. You can keep refining it here, or click Create to write the goal and tasks.",
            )
        )
        _try_audit_memory(
            kind="plan.draft_generated",
            source="plan_mode",
            summary=f"Plan session {int(session_id)} generated a draft.",
            detail=json.dumps(data, ensure_ascii=False, indent=2),
            metadata={
                "session_id": int(session_id),
                "tasks": len(data.get("tasks") or []),
            },
        )
        return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)


@app.post("/goals/{goal_id:int}/plan/start", include_in_schema=False)
def goal_plan_create_session_from_goal(goal_id: int) -> RedirectResponse:
    # 交互约束：已创建的 goal 不支持 Plan Mode。
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/goals/plan/{session_id}/confirm", include_in_schema=False)
async def goal_plan_confirm(request: Request, session_id: int) -> RedirectResponse:
    form = await request.form()
    selected_task = list(form.getlist("selected_task"))

    # Optional edited titles from UI: task_title_{i}
    edited: dict[int, str] = {}
    try:
        for k, v in form.items():
            if not isinstance(k, str):
                continue
            if not k.startswith("task_title_"):
                continue
            try:
                idx = int(k.split("task_title_", 1)[1])
            except Exception:
                continue
            edited[idx] = str(v or "").strip()
    except Exception:
        edited = {}

    with session_scope() as s:
        sess = s.get(GoalPlanSession, session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")
        # Allow writes during `in_progress` so the latest draft can be created at any time.
        if sess.status not in {"in_progress", "awaiting_confirm"}:
            return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)

        data = sess.result_json or {}
        if not data:
            s.add(
                GoalPlanMessage(
                    session_id=session_id,
                    role="assistant",
                    content="There is no draft to create yet. Generate a plan first.",
                )
            )
            return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)
        tasks = data.get("tasks") or []

        # Use indices instead of titles so duplicate task names remain selectable.
        selected_idx: set[int] = set()
        for x in selected_task:
            try:
                selected_idx.add(int(x))
            except Exception:
                continue

        picked: list[str] = []
        for i, t in enumerate(tasks):
            if selected_idx and i not in selected_idx:
                continue
            if not isinstance(t, dict):
                continue
            title = str(edited.get(i) or t.get("title") or "").strip()
            if title:
                picked.append(title)

        # If nothing is selected, return to the session without making changes.
        if not picked:
            s.add(
                GoalPlanMessage(
                    session_id=session_id,
                    role="assistant",
                    content="No tasks were selected. No changes were made.",
                )
            )
            return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)

        target_goal_id: int
        if getattr(sess, "source_goal_id", None):
            target_goal_id = int(sess.source_goal_id)
        else:
            goal_obj = data.get("goal") or {}
            title = str(
                goal_obj.get("title") or goal_obj.get("content") or sess.draft_content
            ).strip()
            goal_content = str(
                goal_obj.get("content") or goal_obj.get("description") or ""
            ).strip()
            status = str(goal_obj.get("status") or "active").strip() or "active"
            priority = str(goal_obj.get("priority") or "normal").strip() or "normal"
            importance = str(goal_obj.get("importance") or "normal").strip() or "normal"

            g = Goal(
                title=title,
                content=goal_content,
                due_date=sess.due_date,
                status=status,
                priority=priority,
                importance=importance,
            )
            s.add(g)
            s.flush()
            _add_goal_created_event(s, g, agent="plan")
            target_goal_id = g.id
            sess.created_goal_id = g.id

        # Create tasks from the selected draft items.
        for title in picked:
            task = Task(
                goal_id=target_goal_id,
                title=title,
                content="",
                status="todo",
            )
            s.add(task)
            s.flush()
            _add_task_created_event(s, task, agent="plan")

        sess.status = "completed"
        if sess.created_goal_id is None:
            sess.created_goal_id = target_goal_id
        s.add(
            GoalPlanMessage(
                session_id=session_id, role="assistant", content="Applied to the goal."
            )
        )

    _try_audit_memory(
        kind="plan.confirmed",
        source="web",
        summary=f"Plan session {int(session_id)} created goal/tasks.",
        detail="\n".join(f"- {title}" for title in picked),
        goal_id=target_goal_id,
        metadata={"session_id": int(session_id), "created_tasks": picked},
    )

    return RedirectResponse(url=f"/goals?goal={target_goal_id}", status_code=303)


@app.post("/goals/plan/{session_id}/step/{step_index}/create", include_in_schema=False)
async def goal_plan_create_single_task(
    request: Request, session_id: int, step_index: int
) -> RedirectResponse:
    """Create a single task from one step in the Plan draft.

    - Create the goal first if the session has not created one yet.
    - Redirect back to the Dashboard with the new task opened.
    """

    form = await request.form()

    with session_scope() as s:
        sess = s.get(GoalPlanSession, session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")

        data = sess.result_json or {}
        tasks = data.get("tasks") or []
        if not isinstance(tasks, list) or step_index < 0 or step_index >= len(tasks):
            s.add(
                GoalPlanMessage(
                    session_id=session_id, role="assistant", content="Invalid step."
                )
            )
            return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)
        t_item = tasks[step_index]
        if not isinstance(t_item, dict):
            s.add(
                GoalPlanMessage(
                    session_id=session_id, role="assistant", content="Invalid step."
                )
            )
            return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)

        # Prefer edited title from UI if present.
        edited_title = ""
        try:
            edited_title = str(form.get(f"task_title_{step_index}") or "").strip()
        except Exception:
            edited_title = ""
        title = edited_title or str(t_item.get("title") or "").strip()
        if not title:
            s.add(
                GoalPlanMessage(
                    session_id=session_id,
                    role="assistant",
                    content="The step title is empty, so the task cannot be created.",
                )
            )
            return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)

        # Ensure a goal exists.
        target_goal_id: int
        if getattr(sess, "created_goal_id", None):
            target_goal_id = int(sess.created_goal_id)
        else:
            goal_obj = data.get("goal") or {}
            title = str(
                goal_obj.get("title") or goal_obj.get("content") or sess.draft_content
            ).strip()
            goal_content = str(
                goal_obj.get("content") or goal_obj.get("description") or ""
            ).strip()
            status = str(goal_obj.get("status") or "active").strip() or "active"
            priority = str(goal_obj.get("priority") or "normal").strip() or "normal"
            importance = str(goal_obj.get("importance") or "normal").strip() or "normal"

            g = Goal(
                title=title,
                content=goal_content,
                due_date=sess.due_date,
                status=status,
                priority=priority,
                importance=importance,
            )
            s.add(g)
            s.flush()
            _add_goal_created_event(s, g, agent="plan")
            target_goal_id = g.id
            sess.created_goal_id = g.id

        task = Task(goal_id=target_goal_id, title=title, content="", status="todo")
        s.add(task)
        s.flush()
        _add_task_created_event(s, task, agent="plan")

        s.add(
            GoalPlanMessage(
                session_id=session_id,
                role="assistant",
                content=f"Created task: {title}",
            )
        )
        # Keep session in progress for further iterations.
        sess.status = "in_progress"

        public_id = str(getattr(task, "public_id", "") or "").strip()
        if public_id:
            return RedirectResponse(url=f"/goals?task={public_id}", status_code=303)

    # Fallback
    return RedirectResponse(url=f"/goals?goal={target_goal_id}", status_code=303)


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
    with _MEMORY_LOCK:
        state = _memory_load_state_unlocked()
        _memory_rotate_current_audit_unlocked(state, now, force=True, create_next=True)
        _memory_finalize_due_days_unlocked(state, now)
        _memory_cleanup_audit_files_unlocked(now)
        state["last_maintenance_at"] = _memory_iso(now)
        _memory_save_state_unlocked(state)
    return RedirectResponse(url="/memory?tab=audit", status_code=303)


@app.get("/companions", response_class=HTMLResponse)
def companions_view(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "companions.html", {})


def _companion_display_status(c: Companion, *, now: dt.datetime | None = None) -> str:
    """用于 Web/UI 展示的 Companion 状态。

    约束：配对成功后，以 gRPC 长连接是否存在作为 online/offline 的判定依据。
    - pending_certification: 未完成配对
    - active: 已配对 + gRPC 在线
    - offline: 已配对 + gRPC 不在线

    参数 now 保留仅为兼容旧调用方（当前不再基于心跳时间计算）。
    """

    if (c.status or "").strip() == "pending_certification" or not (
        c.auth_token or ""
    ).strip():
        return "pending_certification"

    cid = int(getattr(c, "id", 0) or 0)
    online = bool(cid and (COMPANION_GRPC.registry.get(cid) is not None))
    return "active" if online else "offline"


@app.post("/api/companions/register")
def companion_register(payload: dict) -> dict:
    """Companion -> OpenFocus 注册/心跳。

    Companion 进程启动后应定期调用该接口刷新 last_seen。
    """

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    device_id = str(payload.get("device_id") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not device_id or len(device_id) > 64:
        raise HTTPException(status_code=400, detail="device_id is required")
    if not base_url or len(base_url) > 1024:
        raise HTTPException(status_code=400, detail="base_url is required")

    now = _utcnow()
    with session_scope() as s:
        c = s.query(Companion).filter(Companion.device_id == device_id).one_or_none()
        if c is None:
            c = Companion(device_id=device_id, base_url=base_url, name=name)
            s.add(c)
            s.flush()
        else:
            c.base_url = base_url
            if name:
                c.name = name
        c.last_seen_at = now
        # 若已配对成功则保持 active
        if (c.auth_token or "").strip():
            c.status = "active"
        else:
            c.status = "pending_certification"
        s.add(c)
        cid = c.id
        status_out = c.status

    return {"ok": True, "id": cid, "status": status_out}


@app.get("/api/companions")
def companions_list(limit: int = 50) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    with session_scope() as s:
        comps = s.query(Companion).order_by(Companion.id.desc()).limit(limit).all()
        ids = [c.id for c in comps]
        spaces_by_comp: dict[int, list[dict]] = {cid: [] for cid in ids}
        if ids:
            spaces = (
                s.query(AgentSpace)
                .filter(AgentSpace.companion_id.in_(ids))
                .order_by(AgentSpace.id.desc())
                .all()
            )
            for sp in spaces:
                cid = int(getattr(sp, "companion_id", 0) or 0)
                if cid in spaces_by_comp:
                    spaces_by_comp[cid].append(
                        {"id": sp.id, "task_public_id": sp.task_public_id}
                    )

    items: list[dict] = []
    for c in comps:
        items.append(
            {
                "id": c.id,
                "device_id": c.device_id,
                "name": c.name,
                "base_url": c.base_url,
                "status": _companion_display_status(c),
                "last_seen_at": (
                    c.last_seen_at.isoformat() if c.last_seen_at else None
                ),
                "created_at": (
                    c.created_at.isoformat() if getattr(c, "created_at", None) else None
                ),
                "agent_spaces": spaces_by_comp.get(c.id, []),
            }
        )
    return {"ok": True, "items": items}


@app.delete("/api/companions/{companion_id:int}")
def companion_delete(companion_id: int) -> dict:
    """删除 Companion。

    行为：
    - best-effort 断开 gRPC 连接（若在线）
    - 将关联的 AgentSpace 解绑（companion_id=NULL），避免脏引用
    - 删除 Companion 记录
    - 记录事件
    """

    cid = int(companion_id)
    if cid <= 0:
        raise HTTPException(status_code=400, detail="invalid companion_id")

    # 先 best-effort 断开在线连接（真正从 registry 移除由 gRPC stream finally 处理）
    try:
        conn = COMPANION_GRPC.registry.get(cid)
        if conn is not None:
            conn.close()
    except Exception:
        pass

    unbound = 0
    device_id = ""
    with session_scope() as s:
        c = s.get(Companion, cid)
        if c is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        device_id = str(c.device_id or "")

        spaces = s.query(AgentSpace).filter(AgentSpace.companion_id == cid).all()
        unbound = len(spaces)
        for sp in spaces:
            sp.companion_id = None
            s.add(sp)

        s.delete(c)
        s.add(
            Event(
                kind="companion.deleted",
                agent="openfocus/ui",
                task_id=None,
                payload={
                    "companion_id": cid,
                    "device_id": device_id,
                    "unbound_spaces": unbound,
                },
            )
        )

    return {"ok": True, "companion_id": cid, "unbound_spaces": unbound}


@app.post("/api/companions/{companion_id:int}/pair")
async def companion_pair(companion_id: int, payload: dict) -> dict:
    code = str((payload.get("code") if isinstance(payload, dict) else "") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    if len(code) != 10:
        raise HTTPException(status_code=400, detail="认证码必须为 10 位")

    now = _utcnow()
    minute_start = now.replace(second=0, microsecond=0)

    with session_scope() as s:
        c = s.get(Companion, companion_id)
        if c is None:
            raise HTTPException(status_code=404, detail="Companion not found")

        # 每分钟最多 10 次尝试
        ws = c.pair_attempt_window_start
        if (
            ws is None
            or (ws.replace(tzinfo=dt.timezone.utc) if ws.tzinfo is None else ws)
            != minute_start
        ):
            c.pair_attempt_window_start = minute_start
            c.pair_attempt_count = 0
        if c.pair_attempt_count >= 10:
            raise HTTPException(
                status_code=429, detail="本分钟认证尝试次数已达上限（10 次）"
            )
        c.pair_attempt_count += 1
        s.add(c)

        device_id = c.device_id

        # 记录一次“提交认证码”的尝试（不落具体 code，避免泄露）
        s.add(
            Event(
                kind="companion.pair.attempted",
                agent="openfocus/ui",
                task_id=None,
                payload={"companion_id": companion_id, "device_id": device_id},
            )
        )

    # 通过 gRPC 长连接下发配对确认
    conn = COMPANION_GRPC.registry.get(companion_id)
    if conn is None:
        raise HTTPException(
            status_code=502, detail="Companion 未在线（无可用 gRPC 长连接）"
        )
    try:
        token = await conn.request_pair(code, timeout_seconds=10.0)
    except CompanionGrpcError as e:
        raise HTTPException(status_code=502, detail=f"Companion 配对失败：{e}")

    with session_scope() as s:
        c3 = s.get(Companion, companion_id)
        if c3 is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        c3.auth_token = token
        c3.status = "active"
        c3.last_seen_at = now
        s.add(c3)

        s.add(
            Event(
                kind="companion.paired",
                agent="openfocus/ui",
                task_id=None,
                payload={"companion_id": companion_id, "device_id": device_id},
            )
        )
    return {"ok": True}


@app.post("/api/companions/{companion_id:int}/pairing_code")
async def companion_pairing_code(companion_id: int) -> dict:
    """用户点击“认证”时获取（并刷新）当前配对码。

    设计约束：每次用户点击认证都生成一个新的 code，有效期 10 分钟。
    """

    with session_scope() as s:
        c = s.get(Companion, companion_id)
        if c is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        device_id = c.device_id

        # 记录一次“申请配对码”（用户点击认证）
        s.add(
            Event(
                kind="companion.pairing_code.requested",
                agent="openfocus/ui",
                task_id=None,
                payload={"companion_id": companion_id, "device_id": device_id},
            )
        )

        if _companion_display_status(c) == "offline":
            raise HTTPException(status_code=400, detail="Companion offline")

    conn = COMPANION_GRPC.registry.get(companion_id)
    if conn is None:
        raise HTTPException(
            status_code=502, detail="Companion 未在线（无可用 gRPC 长连接）"
        )

    try:
        _code, expires_at = await conn.request_pairing_code(
            force_new=True, timeout_seconds=10.0
        )
    except CompanionGrpcError as e:
        raise HTTPException(status_code=502, detail=f"Companion 获取配对码失败：{e}")

    # 安全要求：配对码只在 Companion 终端/本机侧展示；Web 侧不回传 code，避免“自动填充”绕过人工确认。
    return {"ok": True, "expires_at": expires_at}


@app.post("/api/companions/{companion_id:int}/choose_directory")
async def companion_choose_directory_proxy(companion_id: int) -> dict:
    with session_scope() as s:
        c = s.get(Companion, companion_id)
        if c is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        if (c.status or "").strip() == "pending_certification" or not (
            c.auth_token or ""
        ).strip():
            raise HTTPException(status_code=400, detail="Companion 未认证/不可用")

    conn = COMPANION_GRPC.registry.get(companion_id)
    if conn is None:
        raise HTTPException(
            status_code=502, detail="Companion 未在线（无可用 gRPC 长连接）"
        )
    try:
        path = await conn.request_choose_directory(timeout_seconds=30.0)
    except CompanionGrpcError as e:
        raise HTTPException(status_code=502, detail=f"Companion 目录选择失败：{e}")
    return {"ok": True, "path": path}


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
    # 通过 Companion(gRPC) 从远端节点列目录（只读）
    with session_scope() as s:
        sp = s.get(AgentSpace, int(space_id))
        if sp is None:
            raise HTTPException(status_code=404, detail="AgentSpace not found")
        if not sp.companion_id:
            raise HTTPException(status_code=400, detail="AgentSpace 未绑定 Companion")

        c = s.get(Companion, int(sp.companion_id))
        if c is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        if (c.status or "").strip() == "pending_certification" or not (
            c.auth_token or ""
        ).strip():
            raise HTTPException(status_code=400, detail="Companion 未认证/不可用")

        root_path = str(sp.root_path or "")
        cid = int(c.id)

    conn = COMPANION_GRPC.registry.get(cid)
    if conn is None:
        raise HTTPException(
            status_code=502, detail="Companion 未在线（无可用 gRPC 长连接）"
        )

    try:
        res = await conn.request_files_list(
            root_path=root_path, rel_path=str(path or ""), timeout_seconds=10.0
        )
    except CompanionGrpcError as e:
        raise _map_companion_files_error(e)

    entries = [
        {
            "name": it.name,
            "rel_path": it.rel_path,
            "kind": it.kind,
            "size": int(it.size),
            "mtime": float(it.mtime),
        }
        for it in (res.entries or [])
    ]
    return {"ok": True, "path": res.path, "entries": entries}


@app.get("/api/agent_spaces/{space_id}/files/read")
async def agent_space_files_read(space_id: int, path: str) -> dict:
    with session_scope() as s:
        sp = s.get(AgentSpace, int(space_id))
        if sp is None:
            raise HTTPException(status_code=404, detail="AgentSpace not found")
        if not sp.companion_id:
            raise HTTPException(status_code=400, detail="AgentSpace 未绑定 Companion")

        c = s.get(Companion, int(sp.companion_id))
        if c is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        if (c.status or "").strip() == "pending_certification" or not (
            c.auth_token or ""
        ).strip():
            raise HTTPException(status_code=400, detail="Companion 未认证/不可用")

        root_path = str(sp.root_path or "")
        cid = int(c.id)

    conn = COMPANION_GRPC.registry.get(cid)
    if conn is None:
        raise HTTPException(
            status_code=502, detail="Companion 未在线（无可用 gRPC 长连接）"
        )

    try:
        res = await conn.request_files_read(
            root_path=root_path, rel_path=str(path or ""), max_bytes=256 * 1024
        )
    except CompanionGrpcError as e:
        raise _map_companion_files_error(e)

    return {
        "ok": True,
        "path": res.path,
        "content": res.content,
        "truncated": bool(res.truncated),
        "mime": res.mime,
    }


@app.get("/api/agent_spaces/{space_id}/files/raw")
async def agent_space_files_raw(space_id: int, path: str) -> Response:
    with session_scope() as s:
        sp = s.get(AgentSpace, int(space_id))
        if sp is None:
            raise HTTPException(status_code=404, detail="AgentSpace not found")
        if not sp.companion_id:
            raise HTTPException(status_code=400, detail="AgentSpace 未绑定 Companion")

        c = s.get(Companion, int(sp.companion_id))
        if c is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        if (c.status or "").strip() == "pending_certification" or not (
            c.auth_token or ""
        ).strip():
            raise HTTPException(status_code=400, detail="Companion 未认证/不可用")

        root_path = str(sp.root_path or "")
        cid = int(c.id)

    conn = COMPANION_GRPC.registry.get(cid)
    if conn is None:
        raise HTTPException(
            status_code=502, detail="Companion 未在线（无可用 gRPC 长连接）"
        )

    try:
        res = await conn.request_files_raw(
            root_path=root_path, rel_path=str(path or ""), max_bytes=2 * 1024 * 1024
        )
    except CompanionGrpcError as e:
        raise _map_companion_files_error(e)

    return Response(
        content=bytes(res.data), media_type=(res.mime or "application/octet-stream")
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
    with session_scope() as s:
        sp = s.get(AgentSpace, int(space_id))
        if sp is None:
            raise HTTPException(status_code=404, detail="AgentSpace not found")
        comp = None
        if getattr(sp, "companion_id", None):
            comp = s.get(Companion, int(sp.companion_id))
        return sp, comp


def _require_companion_online(*, sp: AgentSpace, comp: Companion | None):
    if comp is None:
        raise HTTPException(status_code=400, detail="AgentSpace 未绑定 Companion")
    if (comp.status or "").strip() == "pending_certification" or not (
        comp.auth_token or ""
    ).strip():
        raise HTTPException(status_code=400, detail="Companion 未认证/不可用")
    conn = COMPANION_GRPC.registry.get(int(comp.id))
    if conn is None:
        raise HTTPException(
            status_code=502, detail="Companion 未在线（无可用 gRPC 长连接）"
        )
    return conn


def _inspiration_terminal_space_id(space_id: int) -> int:
    return -int(space_id)


def _select_online_companion(
    companion_id: int | None = None,
) -> tuple[Companion, object]:
    with session_scope() as s:
        q = s.query(Companion)
        if companion_id:
            q = q.filter(Companion.id == int(companion_id))
        comps = q.order_by(Companion.id.desc()).all()
        for comp in comps:
            if (comp.status or "").strip() == "pending_certification" or not (
                comp.auth_token or ""
            ).strip():
                continue
            conn = COMPANION_GRPC.registry.get(int(comp.id))
            if conn is None:
                continue
            return comp, conn
    raise HTTPException(status_code=502, detail="No online Companion is available")


def _has_online_companion() -> bool:
    with session_scope() as s:
        comps = s.query(Companion).order_by(Companion.id.desc()).all()
        for comp in comps:
            if (comp.status or "").strip() == "pending_certification" or not (
                comp.auth_token or ""
            ).strip():
                continue
            if COMPANION_GRPC.registry.get(int(comp.id)) is not None:
                return True
    return False


def _inspiration_terminal_payload(space_id: int, t: RemoteTerminalSession) -> dict:
    backend = str(getattr(t, "backend", "") or "ttyd").strip() or "ttyd"
    connect_url = str(getattr(t, "connect_url", "") or "").strip()
    tid = str(t.terminal_id or "")
    out = {
        "terminal_id": tid,
        "name": str(t.name or ""),
        "status": str(t.status or "active"),
        "backend": backend,
        "created_at": t.created_at.isoformat()
        if hasattr(t.created_at, "isoformat")
        else str(t.created_at),
    }
    if backend == "ttyd" and connect_url:
        out["embed_url"] = _inspiration_ttyd_embed_path(int(space_id), tid)
    return out


def _build_inspiration_draft_summary_prompt(space: InspirationSpace) -> str:
    base = str(os.environ.get("OPENFOCUS_BASE_URL") or "").strip()
    title = str(space.title or "Inspiration").strip()
    parts = [
        "You are collaborating with OpenFocus as a terminal agent.",
        "Read the current workspace and resources/ directory, ask the user in this terminal if key context is missing, then create or update resources/draft_summary.md.",
        "The file is the bridge from your custom agent to OpenFocus goal generation: it must be Markdown with one level-1 heading as the goal title, the text under that heading as the goal content, and then one level-2 heading per task with that task's content below it.",
        f"Inspiration title: {title}.",
    ]
    if base:
        parts.append(f"OpenFocus: {base}.")
    parts.append(
        "After saving resources/draft_summary.md, stop and tell the user it is ready to sync in OpenFocus."
    )
    return " ".join(parts)


def _inspiration_terminal_conn(companion_id: int | None):
    comp_id = int(companion_id or 0)
    if not comp_id:
        raise HTTPException(status_code=400, detail="Terminal has no Companion")
    _comp, conn = _select_online_companion(comp_id)
    return conn


async def _inspiration_release_terminals(space_id: int) -> int:
    owner_sid = _inspiration_terminal_space_id(int(space_id))
    with session_scope() as s:
        terms = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.space_id == owner_sid)
            .all()
        )
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
        s.query(RemoteTerminalSession).filter(
            RemoteTerminalSession.space_id == owner_sid
        ).delete(synchronize_session=False)
        s.query(RemoteTerminalOutput).filter(
            RemoteTerminalOutput.space_id == owner_sid
        ).delete(synchronize_session=False)
    for info in term_infos:
        _TTYD_AGENT_MODE.pop(str(info.get("terminal_id") or ""), None)
    return len(term_infos)


@app.get("/api/inspirations/{space_id:int}/terminals")
def inspiration_terminals_list(space_id: int) -> dict:
    with session_scope() as s:
        space = _inspiration_space_or_404(s, int(space_id))
        _inspiration_workspace_path(space, int(space_id))
        owner_sid = _inspiration_terminal_space_id(int(space_id))
        terms = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.space_id == owner_sid)
            .filter(RemoteTerminalSession.status != "closed")
            .order_by(RemoteTerminalSession.id.asc())
            .all()
        )
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
    owner_sid = _inspiration_terminal_space_id(int(space_id))
    with session_scope() as s:
        existing = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.space_id == owner_sid)
            .all()
        )
        used = {
            str((t.name or "").strip()) for t in existing if str((t.name or "").strip())
        }
        base_name = "terminal"
        name = base_name
        if name in used:
            i = 2
            while True:
                cand = f"{base_name}-{i}"
                if cand not in used:
                    name = cand
                    break
                i += 1
        t = RemoteTerminalSession(
            space_id=owner_sid,
            task_public_id="",
            companion_id=int(comp.id),
            root_path=workspace_path,
            name=name,
            terminal_id=real_tid,
            backend=backend,
            connect_url=connect_url,
            status="active",
        )
        s.add(t)
        s.flush()
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
    owner_sid = _inspiration_terminal_space_id(int(space_id))
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
        t = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == tid)
            .one_or_none()
        )
        if t is None or int(t.space_id) != owner_sid:
            raise HTTPException(status_code=404, detail="Terminal not found")
        dup = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.space_id == owner_sid)
            .filter(RemoteTerminalSession.terminal_id != tid)
            .filter(RemoteTerminalSession.name == raw_name)
            .one_or_none()
        )
        if dup is not None:
            raise HTTPException(status_code=400, detail="name 已存在")
        t.name = raw_name
        s.add(t)
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
    owner_sid = _inspiration_terminal_space_id(int(space_id))
    tid = str(terminal_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="terminal_id is required")
    comp_id = 0
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
    with contextlib.suppress(Exception):
        conn = _inspiration_terminal_conn(comp_id)
        await conn.request_terminal_stop(terminal_id=tid, timeout_seconds=10.0)
    with session_scope() as s:
        s.query(RemoteTerminalSession).filter(
            RemoteTerminalSession.terminal_id == tid
        ).delete(synchronize_session=False)
        s.query(RemoteTerminalOutput).filter(
            RemoteTerminalOutput.terminal_id == tid
        ).delete(synchronize_session=False)
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
        terms = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.space_id == int(sp.id))
            .filter(RemoteTerminalSession.status != "closed")
            .order_by(RemoteTerminalSession.id.asc())
            .all()
        )

    cid = int(getattr(comp, "id", 0) or 0) if comp is not None else 0
    online = bool(cid and (COMPANION_GRPC.registry.get(cid) is not None))

    def _terminal_payload(t: RemoteTerminalSession) -> dict:
        backend = str(getattr(t, "backend", "") or "ttyd").strip() or "ttyd"
        connect_url = str(getattr(t, "connect_url", "") or "").strip()
        tid = str(t.terminal_id or "")
        out = {
            "terminal_id": tid,
            "name": (t.name or ""),
            "status": t.status,
            "backend": backend,
            "created_at": t.created_at.isoformat()
            if hasattr(t.created_at, "isoformat")
            else str(t.created_at),
        }
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

    # 默认 name：terminal / terminal-2 / terminal-3 ...（同一 space 下不重复）
    with session_scope() as s:
        existing = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.space_id == int(sp.id))
            .all()
        )
        used = {
            str((t.name or "").strip()) for t in existing if str((t.name or "").strip())
        }
        base = "terminal"
        name = base
        if name in used:
            i = 2
            while True:
                cand = f"{base}-{i}"
                if cand not in used:
                    name = cand
                    break
                i += 1

    with session_scope() as s:
        t = RemoteTerminalSession(
            space_id=int(sp.id),
            task_public_id=str(sp.task_public_id or ""),
            companion_id=int(getattr(comp, "id", 0) or 0) if comp is not None else None,
            root_path=str(sp.root_path or ""),
            name=name,
            terminal_id=real_tid,
            backend=backend,
            connect_url=connect_url,
            status="active",
        )
        s.add(t)
        s.flush()

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
        t = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == tid)
            .one_or_none()
        )
        if t is None or int(t.space_id) != int(sp.id):
            raise HTTPException(status_code=404, detail="Terminal not found")

        dup = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.space_id == int(sp.id))
            .filter(RemoteTerminalSession.terminal_id != tid)
            .filter(RemoteTerminalSession.name == raw_name)
            .one_or_none()
        )
        if dup is not None:
            raise HTTPException(status_code=400, detail="name 已存在")

        t.name = raw_name
        s.add(t)

    return {"ok": True, "terminal": {"terminal_id": tid, "name": raw_name}}


@app.post("/api/agent_spaces/{space_id}/terminals/{terminal_id}/inject")
async def terminals_inject(space_id: int, terminal_id: str, payload: dict) -> dict:
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
        s.query(RemoteTerminalSession).filter(
            RemoteTerminalSession.terminal_id == tid
        ).delete(synchronize_session=False)
        s.query(RemoteTerminalOutput).filter(
            RemoteTerminalOutput.terminal_id == tid
        ).delete(synchronize_session=False)
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
