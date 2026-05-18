# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Callable

import grpc

from ..db import session_scope
from ..models import Companion

# 由 grpc_tools.protoc 生成
from . import companion_rpc_pb2 as pb2
from . import companion_rpc_pb2_grpc as pb2_grpc


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_ms() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


class CompanionGrpcError(RuntimeError):
    pass


# Agent 输出（AgentChunk）为流式事件：通过监听器回调转发到 HTTP/SSE 层。
_AGENT_CHUNK_LISTENERS: list[Callable[[pb2.AgentChunk], None]] = []
_TERMINAL_OUTPUT_LISTENERS: list[Callable[[pb2.TerminalOutput], None]] = []
_RUNTIME_SIGNAL_LISTENERS: list[Callable[[pb2.AgentRuntimeSignal], None]] = []
_BROWSER_BIND_PROOF_LISTENERS: list[Callable[[pb2.BrowserBindProof], None]] = []
_FLOAT_BALL_ACTION_LISTENERS: list[Callable[[pb2.FloatBallAction], None]] = []


def add_agent_chunk_listener(listener: Callable[[pb2.AgentChunk], None]) -> None:
    _AGENT_CHUNK_LISTENERS.append(listener)


def add_terminal_output_listener(
    listener: Callable[[pb2.TerminalOutput], None],
) -> None:
    _TERMINAL_OUTPUT_LISTENERS.append(listener)


def add_runtime_signal_listener(
    listener: Callable[[pb2.AgentRuntimeSignal], None],
) -> None:
    _RUNTIME_SIGNAL_LISTENERS.append(listener)


def add_browser_bind_proof_listener(
    listener: Callable[[pb2.BrowserBindProof], None]
) -> None:
    _BROWSER_BIND_PROOF_LISTENERS.append(listener)


def add_float_ball_action_listener(
    listener: Callable[[pb2.FloatBallAction], None]
) -> None:
    _FLOAT_BALL_ACTION_LISTENERS.append(listener)


@dataclass
class _Pending:
    fut: asyncio.Future
    kind: str


class CompanionConnection:
    """代表一个在线 Companion 的控制通道连接。"""

    PING_INTERVAL_SECONDS = 10

    def __init__(self, *, companion_id: int, device_id: str) -> None:
        self.companion_id = int(companion_id)
        self.device_id = device_id
        self.name = ""
        self.capabilities: list[str] = []
        self.connected_at = _utcnow()
        self.last_seen_at = _utcnow()
        self._out_q: asyncio.Queue[pb2.ServerToClient] = asyncio.Queue()
        self._pending: dict[str, _Pending] = {}
        self._closed = asyncio.Event()
        self._ping_task: asyncio.Task | None = None

    def close(self) -> None:
        if not self._closed.is_set():
            self._closed.set()
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        # 释放所有 pending
        for rid, p in list(self._pending.items()):
            if not p.fut.done():
                p.fut.set_exception(CompanionGrpcError("connection closed"))
        self._pending.clear()

    async def start_ping_loop(self) -> None:
        async def _loop() -> None:
            while not self._closed.is_set():
                await asyncio.sleep(self.PING_INTERVAL_SECONDS)
                await self._out_q.put(
                    pb2.ServerToClient(ping=pb2.Ping(ts_unix_ms=_now_ms()))
                )

        self._ping_task = asyncio.create_task(
            _loop(), name=f"companion-ping:{self.companion_id}"
        )

    async def outgoing(self) -> AsyncIterator[pb2.ServerToClient]:
        while not self._closed.is_set():
            msg = await self._out_q.get()
            yield msg

    def mark_seen(self) -> None:
        self.last_seen_at = _utcnow()

    async def request_pair(self, code: str, *, timeout_seconds: float = 10.0) -> str:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="pair")
        await self._out_q.put(
            pb2.ServerToClient(pair=pb2.PairRequest(request_id=rid, code=code))
        )
        try:
            res: pb2.PairResponse = await asyncio.wait_for(fut, timeout=timeout_seconds)
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "pair failed")
        token = (res.auth_token or "").strip()
        if not token:
            raise CompanionGrpcError("missing auth_token")
        return token

    async def request_choose_directory(self, *, timeout_seconds: float = 30.0) -> str:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="choose_directory")
        await self._out_q.put(
            pb2.ServerToClient(
                choose_directory=pb2.ChooseDirectoryRequest(request_id=rid)
            )
        )
        try:
            res: pb2.ChooseDirectoryResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "choose_directory failed")
        path = (res.path or "").strip()
        if not path:
            raise CompanionGrpcError("missing path")
        return path

    async def request_pairing_code(
        self, *, force_new: bool, timeout_seconds: float = 10.0
    ) -> tuple[str, str]:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="pairing_code")
        await self._out_q.put(
            pb2.ServerToClient(
                pairing_code=pb2.PairingCodeRequest(
                    request_id=rid, force_new=bool(force_new)
                )
            )
        )
        try:
            res: pb2.PairingCodeResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "pairing_code failed")
        code = (res.code or "").strip()
        exp = (res.expires_at or "").strip()
        if not code:
            raise CompanionGrpcError("missing code")
        return code, exp

    async def request_files_list(
        self, *, root_path: str, rel_path: str, timeout_seconds: float = 10.0
    ) -> pb2.FilesListResponse:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="files_list")
        await self._out_q.put(
            pb2.ServerToClient(
                files_list=pb2.FilesListRequest(
                    request_id=rid, root_path=root_path, rel_path=rel_path
                )
            )
        )
        try:
            res: pb2.FilesListResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "files_list failed")
        return res

    async def request_files_read(
        self,
        *,
        root_path: str,
        rel_path: str,
        max_bytes: int = 256 * 1024,
        timeout_seconds: float = 10.0,
    ) -> pb2.FilesReadResponse:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="files_read")
        await self._out_q.put(
            pb2.ServerToClient(
                files_read=pb2.FilesReadRequest(
                    request_id=rid,
                    root_path=root_path,
                    rel_path=rel_path,
                    max_bytes=int(max_bytes),
                )
            )
        )
        try:
            res: pb2.FilesReadResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "files_read failed")
        return res

    async def request_files_raw(
        self,
        *,
        root_path: str,
        rel_path: str,
        max_bytes: int = 2 * 1024 * 1024,
        timeout_seconds: float = 20.0,
    ) -> pb2.FilesRawResponse:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="files_raw")
        await self._out_q.put(
            pb2.ServerToClient(
                files_raw=pb2.FilesRawRequest(
                    request_id=rid,
                    root_path=root_path,
                    rel_path=rel_path,
                    max_bytes=int(max_bytes),
                )
            )
        )
        try:
            res: pb2.FilesRawResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "files_raw failed")
        return res

    async def request_agent_start(
        self,
        *,
        session_id: str,
        root_path: str,
        agent_type: str,
        task_public_id: str,
        timeout_seconds: float = 10.0,
    ) -> pb2.AgentStartResponse:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="agent_start")
        await self._out_q.put(
            pb2.ServerToClient(
                agent_start=pb2.AgentStartRequest(
                    request_id=rid,
                    session_id=str(session_id or ""),
                    root_path=str(root_path or ""),
                    agent_type=str(agent_type or ""),
                    task_public_id=str(task_public_id or ""),
                )
            )
        )
        try:
            res: pb2.AgentStartResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "agent_start failed")
        return res

    async def request_agent_terminate(
        self, *, session_id: str, timeout_seconds: float = 10.0
    ) -> pb2.AgentTerminateResponse:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="agent_terminate")
        await self._out_q.put(
            pb2.ServerToClient(
                agent_terminate=pb2.AgentTerminateRequest(
                    request_id=rid, session_id=str(session_id or "")
                )
            )
        )
        try:
            res: pb2.AgentTerminateResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "agent_terminate failed")
        return res

    async def request_agent_send(
        self,
        *,
        request_id: str,
        session_id: str,
        prompt: str,
        timeout_seconds: float = 10.0,
    ) -> pb2.AgentSendResponse:
        # request_id 由上层生成（用于 chunk 聚合），此处作为 AgentSendRequest.request_id 下发。
        rid = str(request_id or uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="agent_send")
        await self._out_q.put(
            pb2.ServerToClient(
                agent_send=pb2.AgentSendRequest(
                    request_id=rid,
                    session_id=str(session_id or ""),
                    prompt=str(prompt or ""),
                )
            )
        )
        try:
            res: pb2.AgentSendResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "agent_send failed")
        return res

    async def request_terminal_start(
        self,
        *,
        terminal_id: str,
        root_path: str,
        base_path: str = "",
        task_public_id: str = "",
        timeout_seconds: float = 10.0,
    ) -> pb2.TerminalStartResponse:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="terminal_start")
        await self._out_q.put(
            pb2.ServerToClient(
                terminal_start=pb2.TerminalStartRequest(
                    request_id=rid,
                    terminal_id=str(terminal_id or ""),
                    root_path=str(root_path or ""),
                    base_path=str(base_path or ""),
                    task_public_id=str(task_public_id or ""),
                )
            )
        )
        try:
            res: pb2.TerminalStartResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "terminal_start failed")
        return res

    async def request_terminal_stop(
        self, *, terminal_id: str, timeout_seconds: float = 10.0
    ) -> pb2.TerminalStopResponse:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="terminal_stop")
        await self._out_q.put(
            pb2.ServerToClient(
                terminal_stop=pb2.TerminalStopRequest(
                    request_id=rid, terminal_id=str(terminal_id or "")
                )
            )
        )
        try:
            res: pb2.TerminalStopResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "terminal_stop failed")
        return res

    async def request_terminal_input(
        self,
        *,
        terminal_id: str,
        data: bytes,
        timeout_seconds: float = 10.0,
    ) -> pb2.TerminalInputResponse:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="terminal_input")
        await self._out_q.put(
            pb2.ServerToClient(
                terminal_input=pb2.TerminalInputRequest(
                    request_id=rid,
                    terminal_id=str(terminal_id or ""),
                    data=bytes(data or b""),
                )
            )
        )
        try:
            res: pb2.TerminalInputResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "terminal_input failed")
        return res

    async def request_terminal_resize(
        self,
        *,
        terminal_id: str,
        cols: int,
        rows: int,
        timeout_seconds: float = 10.0,
    ) -> pb2.TerminalResizeResponse:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="terminal_resize")
        await self._out_q.put(
            pb2.ServerToClient(
                terminal_resize=pb2.TerminalResizeRequest(
                    request_id=rid,
                    terminal_id=str(terminal_id or ""),
                    cols=int(cols or 0),
                    rows=int(rows or 0),
                )
            )
        )
        try:
            res: pb2.TerminalResizeResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "terminal_resize failed")
        return res

    async def request_terminal_mouse_mode(
        self,
        *,
        terminal_id: str,
        enabled: bool,
        timeout_seconds: float = 10.0,
    ) -> pb2.TerminalMouseModeResponse:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="terminal_mouse_mode")
        await self._out_q.put(
            pb2.ServerToClient(
                terminal_mouse_mode=pb2.TerminalMouseModeRequest(
                    request_id=rid,
                    terminal_id=str(terminal_id or ""),
                    enabled=bool(enabled),
                )
            )
        )
        try:
            res: pb2.TerminalMouseModeResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "terminal_mouse_mode failed")
        return res

    async def request_float_ball_start(
        self,
        *,
        browser_session_id: str,
        openfocus_base_url: str,
        summary_json: str,
        timeout_seconds: float = 10.0,
    ) -> pb2.FloatBallStartResponse:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="float_ball_start")
        await self._out_q.put(
            pb2.ServerToClient(
                float_ball_start=pb2.FloatBallStartRequest(
                    request_id=rid,
                    browser_session_id=str(browser_session_id or ""),
                    openfocus_base_url=str(openfocus_base_url or ""),
                    summary_json=str(summary_json or ""),
                )
            )
        )
        try:
            res: pb2.FloatBallStartResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "float_ball_start failed")
        return res

    async def request_float_ball_update(
        self,
        *,
        browser_session_id: str,
        summary_json: str,
        timeout_seconds: float = 10.0,
    ) -> pb2.FloatBallUpdateResponse:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="float_ball_update")
        await self._out_q.put(
            pb2.ServerToClient(
                float_ball_update=pb2.FloatBallUpdateRequest(
                    request_id=rid,
                    browser_session_id=str(browser_session_id or ""),
                    summary_json=str(summary_json or ""),
                )
            )
        )
        try:
            res: pb2.FloatBallUpdateResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "float_ball_update failed")
        return res

    async def request_float_ball_stop(
        self, *, browser_session_id: str, timeout_seconds: float = 10.0
    ) -> pb2.FloatBallStopResponse:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="float_ball_stop")
        await self._out_q.put(
            pb2.ServerToClient(
                float_ball_stop=pb2.FloatBallStopRequest(
                    request_id=rid, browser_session_id=str(browser_session_id or "")
                )
            )
        )
        try:
            res: pb2.FloatBallStopResponse = await asyncio.wait_for(
                fut, timeout=timeout_seconds
            )
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "float_ball_stop failed")
        return res

    def handle_incoming(self, msg: pb2.ClientToServer) -> None:
        self.mark_seen()
        which = msg.WhichOneof("msg")
        if which == "hello":
            h: pb2.Hello = msg.hello
            self.name = h.name or ""
            self.capabilities = list(h.capabilities)
            # hello 仅用于更新元信息；鉴权/状态由上层决定
            return
        if which == "pong":
            return
        if which == "pair_resp":
            r: pb2.PairResponse = msg.pair_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "pair" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "pairing_code_resp":
            r: pb2.PairingCodeResponse = msg.pairing_code_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "pairing_code" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "files_list_resp":
            r: pb2.FilesListResponse = msg.files_list_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "files_list" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "files_read_resp":
            r: pb2.FilesReadResponse = msg.files_read_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "files_read" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "files_raw_resp":
            r: pb2.FilesRawResponse = msg.files_raw_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "files_raw" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "choose_directory_resp":
            r: pb2.ChooseDirectoryResponse = msg.choose_directory_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "choose_directory" and not p.fut.done():
                p.fut.set_result(r)
            return

        if which == "agent_start_resp":
            r: pb2.AgentStartResponse = msg.agent_start_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "agent_start" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "agent_terminate_resp":
            r: pb2.AgentTerminateResponse = msg.agent_terminate_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "agent_terminate" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "agent_send_resp":
            r: pb2.AgentSendResponse = msg.agent_send_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "agent_send" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "agent_chunk":
            ch: pb2.AgentChunk = msg.agent_chunk
            # best-effort 通知监听器（不要在这里阻塞）。
            for cb in list(_AGENT_CHUNK_LISTENERS):
                try:
                    cb(ch)
                except Exception:
                    pass
            return
        if which == "runtime_signal":
            sig: pb2.AgentRuntimeSignal = msg.runtime_signal
            if not int(sig.companion_id or 0):
                sig.companion_id = int(self.companion_id)
            for cb in list(_RUNTIME_SIGNAL_LISTENERS):
                try:
                    cb(sig)
                except Exception:
                    pass
            return
        if which == "browser_bind_proof":
            proof: pb2.BrowserBindProof = msg.browser_bind_proof
            if not int(proof.companion_id or 0):
                proof.companion_id = int(self.companion_id)
            for cb in list(_BROWSER_BIND_PROOF_LISTENERS):
                try:
                    cb(proof)
                except Exception:
                    pass
            return
        if which == "float_ball_action":
            action: pb2.FloatBallAction = msg.float_ball_action
            for cb in list(_FLOAT_BALL_ACTION_LISTENERS):
                try:
                    cb(action)
                except Exception:
                    pass
            return

        if which == "terminal_start_resp":
            r: pb2.TerminalStartResponse = msg.terminal_start_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "terminal_start" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "terminal_stop_resp":
            r: pb2.TerminalStopResponse = msg.terminal_stop_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "terminal_stop" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "terminal_input_resp":
            r: pb2.TerminalInputResponse = msg.terminal_input_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "terminal_input" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "terminal_resize_resp":
            r: pb2.TerminalResizeResponse = msg.terminal_resize_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "terminal_resize" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "terminal_mouse_mode_resp":
            r: pb2.TerminalMouseModeResponse = msg.terminal_mouse_mode_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "terminal_mouse_mode" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "float_ball_start_resp":
            r: pb2.FloatBallStartResponse = msg.float_ball_start_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "float_ball_start" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "float_ball_update_resp":
            r: pb2.FloatBallUpdateResponse = msg.float_ball_update_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "float_ball_update" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "float_ball_stop_resp":
            r: pb2.FloatBallStopResponse = msg.float_ball_stop_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "float_ball_stop" and not p.fut.done():
                p.fut.set_result(r)
            return
        if which == "terminal_output":
            out: pb2.TerminalOutput = msg.terminal_output
            for cb in list(_TERMINAL_OUTPUT_LISTENERS):
                try:
                    cb(out)
                except Exception:
                    pass
            return


class CompanionRegistry:
    """在内存中维护在线连接；落库更新 last_seen/status。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._by_companion_id: dict[int, CompanionConnection] = {}

    def get(self, companion_id: int) -> CompanionConnection | None:
        return self._by_companion_id.get(int(companion_id))

    async def set_connected(self, companion_id: int, conn: CompanionConnection) -> None:
        cid = int(companion_id)
        async with self._lock:
            old = self._by_companion_id.get(cid)
            self._by_companion_id[cid] = conn
        if old and old is not conn:
            old.close()

    async def set_disconnected(
        self, companion_id: int, conn: CompanionConnection
    ) -> None:
        cid = int(companion_id)
        async with self._lock:
            cur = self._by_companion_id.get(cid)
            if cur is conn:
                self._by_companion_id.pop(cid, None)

        # Companion 连接/断连事件不再落库（避免污染 Dashboard 事件流）。


class CompanionControlServicer(pb2_grpc.CompanionControlServicer):
    def __init__(self, registry: CompanionRegistry) -> None:
        self.registry = registry

    async def Connect(
        self,
        request_iterator: AsyncIterator[pb2.ClientToServer],
        context: grpc.aio.ServicerContext,
    ):
        # 约定：第一条必须是 hello
        try:
            first = await request_iterator.__anext__()
        except StopAsyncIteration:
            return
        if first.WhichOneof("msg") != "hello":
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "first message must be hello"
            )

        hello = first.hello
        device_id = (hello.device_id or "").strip()
        if not device_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "device_id is required"
            )

        # upsert DB 记录（优先使用 server_companion_id 复用同一条记录）
        now = _utcnow()
        assigned_id: int
        with session_scope() as s:
            wanted_id = int(getattr(hello, "server_companion_id", 0) or 0)

            c: Companion | None = None
            if wanted_id > 0:
                c = s.get(Companion, wanted_id)
                # 安全约束：只有当 device_id 匹配时才允许复用该 id。
                if c is not None and (c.device_id or "") != device_id:
                    c = None

            if c is None:
                c = (
                    s.query(Companion)
                    .filter(Companion.device_id == device_id)
                    .one_or_none()
                )
            if c is None:
                c = Companion(
                    device_id=device_id, name=hello.name or "", base_url="grpc://"
                )
                s.add(c)
                s.flush()
            else:
                if hello.name:
                    c.name = hello.name
            c.last_seen_at = now

            # 若 companion 侧带了 token，且与 DB 一致，则直接视为 active。
            token_in_db = (c.auth_token or "").strip()
            token_in_hello = (hello.auth_token or "").strip()
            if token_in_db and token_in_hello and token_in_db == token_in_hello:
                c.status = "active"
            else:
                c.status = "pending_certification" if not token_in_db else c.status
            s.add(c)

            assigned_id = int(c.id)

        # 建立在线连接并下发 Welcome（服务端分配的稳定 companion_id）
        conn = CompanionConnection(companion_id=assigned_id, device_id=device_id)
        conn.handle_incoming(first)
        await self.registry.set_connected(assigned_id, conn)

        # Companion 连接/断连事件不再落库（避免污染 Dashboard 事件流）。

        await conn._out_q.put(
            pb2.ServerToClient(welcome=pb2.Welcome(companion_id=assigned_id))
        )
        await conn.start_ping_loop()

        async def _consume() -> None:
            try:
                async for msg in request_iterator:
                    conn.handle_incoming(msg)
                    # best-effort 落库 last_seen
                    with session_scope() as s:
                        c = s.get(Companion, assigned_id)
                        if c is not None:
                            c.last_seen_at = _utcnow()
                            s.add(c)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 连接断开/异常由 finally 处理
                pass

        consumer = asyncio.create_task(
            _consume(), name=f"companion-consume:{assigned_id}"
        )
        try:
            async for out in conn.outgoing():
                yield out
        finally:
            consumer.cancel()
            await self.registry.set_disconnected(assigned_id, conn)
            conn.close()


class CompanionGrpcServer:
    """gRPC server（OpenFocus 侧）。"""

    def __init__(self) -> None:
        self.registry = CompanionRegistry()
        self._server: grpc.aio.Server | None = None
        self.bound_addr: str | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        host = os.environ.get("OPENFOCUS_GRPC_HOST") or "127.0.0.1"
        port = int(os.environ.get("OPENFOCUS_GRPC_PORT") or "17891")

        server = grpc.aio.server(
            options=[
                ("grpc.keepalive_time_ms", 20000),
                ("grpc.keepalive_timeout_ms", 10000),
            ]
        )
        pb2_grpc.add_CompanionControlServicer_to_server(
            CompanionControlServicer(self.registry), server
        )
        bind = f"{host}:{port}"
        actual_port = server.add_insecure_port(bind)
        if actual_port == 0:
            raise RuntimeError(f"failed to bind grpc on {bind}")
        await server.start()
        self._server = server
        self.bound_addr = f"{host}:{actual_port}"

    async def stop(self) -> None:
        if self._server is None:
            return
        server = self._server
        # 尽量等待底层资源释放，避免在 pytest/anyio 频繁启动/关闭时触发 gRPC aio 的不稳定。
        await server.stop(grace=0)
        try:
            await server.wait_for_termination(timeout=1.0)
        except TypeError:
            # 兼容不同 grpcio 版本的签名
            await server.wait_for_termination()
        self._server = None
        self.bound_addr = None
