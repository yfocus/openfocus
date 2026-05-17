# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import json

from sqlalchemy import text

from ..companion.grpc import (
    add_agent_chunk_listener,
    add_runtime_signal_listener,
    add_terminal_output_listener,
)
from ..db import session_scope
from ..domains.agent_activity import service as agent_activity_service
from ..domains.memory import service as memory_service
from ..models import (
    AgentMessage,
    AgentSession,
    RemoteTerminalOutput,
    RemoteTerminalSession,
)

# Remote Terminal：每个 terminal 最多保留最近 1GB 历史（用于刷新/重进页面回放）。
# 注意：该值会影响 SQLite 持久化体积与清理频率。
TERM_HISTORY_MAX_BYTES = 1024 * 1024 * 1024

# 回放接口单次返回的最大体积（避免把 1GB 直接塞给浏览器/WS）。
TERM_HISTORY_PUBLIC_MAX_BYTES = 4 * 1024 * 1024


class AgentSSEHub:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subs: dict[str, set[asyncio.Queue[dict]]] = {}

    async def subscribe(self, session_id: str) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
        sid = str(session_id or "").strip()
        async with self._lock:
            self._subs.setdefault(sid, set()).add(q)
        return q

    async def unsubscribe(self, session_id: str, q: asyncio.Queue[dict]) -> None:
        sid = str(session_id or "").strip()
        async with self._lock:
            subs = self._subs.get(sid)
            if not subs:
                return
            subs.discard(q)
            if not subs:
                self._subs.pop(sid, None)

    def publish(self, session_id: str, ev: dict) -> None:
        sid = str(session_id or "").strip()
        subs = self._subs.get(sid)
        if not subs:
            return
        for q in list(subs):
            try:
                q.put_nowait(ev)
            except Exception:
                # 队列满/关闭：丢弃即可（前端可用 history 兜底）。
                pass


class TerminalEventHub:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subs: dict[str, set[asyncio.Queue[dict]]] = {}
        self.ttyd_auto_prompts: dict[str, dict[str, object]] = {}

    async def subscribe(self, terminal_id: str) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=500)
        tid = str(terminal_id or "").strip()
        async with self._lock:
            self._subs.setdefault(tid, set()).add(q)
        return q

    async def unsubscribe(self, terminal_id: str, q: asyncio.Queue[dict]) -> None:
        tid = str(terminal_id or "").strip()
        async with self._lock:
            subs = self._subs.get(tid)
            if not subs:
                return
            subs.discard(q)
            if not subs:
                self._subs.pop(tid, None)

    def publish(self, terminal_id: str, ev: dict) -> None:
        tid = str(terminal_id or "").strip()
        subs = self._subs.get(tid)
        if not subs:
            return
        for q in list(subs):
            try:
                q.put_nowait(ev)
            except Exception:
                pass

    def rewrite_ttyd_input_for_auto_prompts(self, terminal_id: str, msg):
        st = self.ttyd_auto_prompts.get(str(terminal_id or "")) or {}
        if not bool(st.get("enabled")):
            return msg
        prompt = " ".join(str(st.get("prompt") or "").split())
        if not prompt:
            return msg
        paste_s = f"\x1b[200~ {prompt}\x1b[201~"
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


agent_sse_hub = AgentSSEHub()
terminal_event_hub = TerminalEventHub()

_AGENT_LISTENER_INSTALLED = False
_TERM_LISTENER_INSTALLED = False
_RUNTIME_SIGNAL_LISTENER_INSTALLED = False


async def persist_and_publish_agent_chunk(ch) -> None:
    # 1) SSE
    agent_sse_hub.publish(
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
            sess.updated_at = memory_service.utcnow()
            s.add(sess)

        if bool(ch.done) or (not bool(ch.ok)):
            sess_for_activity = (
                s.query(AgentSession)
                .filter(AgentSession.session_id == ch.session_id)
                .one_or_none()
            )
            agent_activity_service.handle_runtime_signal(
                s,
                kind="runtime.turn.completed"
                if bool(ch.ok) and bool(ch.done)
                else "runtime.turn.failed",
                agent_runtime=str(
                    getattr(sess_for_activity, "agent_type", "") or "agent"
                ),
                session_id=str(ch.session_id or ""),
                turn_id=str(ch.request_id or ""),
                task_public_id=str(
                    getattr(sess_for_activity, "task_public_id", "") or ""
                ),
                companion_id=int(getattr(sess_for_activity, "companion_id", 0) or 0)
                or None,
                source="companion.agent_chunk",
                payload={
                    "summary": "Agent turn completed."
                    if bool(ch.ok)
                    else "Agent turn failed.",
                    "error": str(ch.error or ""),
                },
            )

    if ch.text or ch.error or bool(ch.done):
        memory_service.try_audit_memory(
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


def install_agent_chunk_listener_once() -> None:
    global _AGENT_LISTENER_INSTALLED
    if _AGENT_LISTENER_INSTALLED:
        return

    def _on_chunk(ch) -> None:
        try:
            asyncio.get_running_loop().create_task(persist_and_publish_agent_chunk(ch))
        except RuntimeError:
            # 没有 event loop 时直接忽略（正常情况下不会发生）
            pass

    add_agent_chunk_listener(_on_chunk)
    _AGENT_LISTENER_INSTALLED = True


async def handle_terminal_output(out) -> None:
    raw = bytes(out.data or b"")
    data_b64 = base64.b64encode(raw).decode("ascii") if raw else ""

    terminal_event_hub.publish(
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
        decoded = memory_service.decode_terminal_bytes(raw)
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

                    if total > TERM_HISTORY_MAX_BYTES:
                        need = int(total - TERM_HISTORY_MAX_BYTES)
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
        memory_service.try_audit_memory(
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


def install_terminal_listener_once() -> None:
    global _TERM_LISTENER_INSTALLED
    if _TERM_LISTENER_INSTALLED:
        return

    def _on_out(out) -> None:
        try:
            asyncio.get_running_loop().create_task(handle_terminal_output(out))
        except RuntimeError:
            pass

    add_terminal_output_listener(_on_out)
    _TERM_LISTENER_INSTALLED = True


async def handle_runtime_signal(sig) -> None:
    payload: dict = {}
    raw_payload = str(getattr(sig, "payload_json", "") or "").strip()
    if raw_payload:
        try:
            loaded = json.loads(raw_payload)
            if isinstance(loaded, dict):
                payload = loaded
        except Exception:
            payload = {"raw_payload": raw_payload}

    with session_scope() as s:
        agent_activity_service.handle_runtime_signal(
            s,
            kind=str(getattr(sig, "kind", "") or ""),
            raw_kind=str(getattr(sig, "raw_kind", "") or ""),
            agent_runtime=str(getattr(sig, "agent_runtime", "") or ""),
            session_id=str(getattr(sig, "session_id", "") or ""),
            turn_id=str(getattr(sig, "turn_id", "") or ""),
            task_public_id=str(getattr(sig, "task_public_id", "") or ""),
            terminal_id=str(getattr(sig, "terminal_id", "") or ""),
            companion_id=int(getattr(sig, "companion_id", 0) or 0) or None,
            cwd=str(getattr(sig, "cwd", "") or ""),
            source=str(getattr(sig, "source", "") or "companion"),
            payload=payload,
        )


def install_runtime_signal_listener_once() -> None:
    global _RUNTIME_SIGNAL_LISTENER_INSTALLED
    if _RUNTIME_SIGNAL_LISTENER_INSTALLED:
        return

    def _on_signal(sig) -> None:
        try:
            asyncio.get_running_loop().create_task(handle_runtime_signal(sig))
        except RuntimeError:
            pass

    add_runtime_signal_listener(_on_signal)
    _RUNTIME_SIGNAL_LISTENER_INSTALLED = True
