from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Callable

import grpc

from .db import session_scope
from .models import Companion


# 由 grpc_tools.protoc 生成
from . import companion_rpc_pb2 as pb2
from . import companion_rpc_pb2_grpc as pb2_grpc


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_ms() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


class CompanionGrpcError(RuntimeError):
    pass


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
                await self._out_q.put(pb2.ServerToClient(ping=pb2.Ping(ts_unix_ms=_now_ms())))

        self._ping_task = asyncio.create_task(_loop(), name=f"companion-ping:{self.companion_id}")

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
        await self._out_q.put(pb2.ServerToClient(pair=pb2.PairRequest(request_id=rid, code=code)))
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
        await self._out_q.put(pb2.ServerToClient(choose_directory=pb2.ChooseDirectoryRequest(request_id=rid)))
        try:
            res: pb2.ChooseDirectoryResponse = await asyncio.wait_for(fut, timeout=timeout_seconds)
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "choose_directory failed")
        path = (res.path or "").strip()
        if not path:
            raise CompanionGrpcError("missing path")
        return path

    async def request_pairing_code(self, *, force_new: bool, timeout_seconds: float = 10.0) -> tuple[str, str]:
        rid = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = _Pending(fut=fut, kind="pairing_code")
        await self._out_q.put(
            pb2.ServerToClient(pairing_code=pb2.PairingCodeRequest(request_id=rid, force_new=bool(force_new)))
        )
        try:
            res: pb2.PairingCodeResponse = await asyncio.wait_for(fut, timeout=timeout_seconds)
        finally:
            self._pending.pop(rid, None)
        if not res.ok:
            raise CompanionGrpcError(res.error or "pairing_code failed")
        code = (res.code or "").strip()
        exp = (res.expires_at or "").strip()
        if not code:
            raise CompanionGrpcError("missing code")
        return code, exp

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
        if which == "choose_directory_resp":
            r: pb2.ChooseDirectoryResponse = msg.choose_directory_resp
            p = self._pending.get(r.request_id)
            if p and p.kind == "choose_directory" and not p.fut.done():
                p.fut.set_result(r)
            return


class CompanionRegistry:
    """在内存中维护在线连接；落库更新 last_seen/status。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._by_companion_id: dict[int, CompanionConnection] = {}
        self._on_connect: list[Callable[[CompanionConnection, pb2.Hello], None]] = []

    def get(self, companion_id: int) -> CompanionConnection | None:
        return self._by_companion_id.get(int(companion_id))

    async def set_connected(self, companion_id: int, conn: CompanionConnection) -> None:
        cid = int(companion_id)
        async with self._lock:
            old = self._by_companion_id.get(cid)
            self._by_companion_id[cid] = conn
        if old and old is not conn:
            old.close()

    async def set_disconnected(self, companion_id: int, conn: CompanionConnection) -> None:
        cid = int(companion_id)
        async with self._lock:
            cur = self._by_companion_id.get(cid)
            if cur is conn:
                self._by_companion_id.pop(cid, None)


class CompanionControlServicer(pb2_grpc.CompanionControlServicer):
    def __init__(self, registry: CompanionRegistry) -> None:
        self.registry = registry

    async def Connect(self, request_iterator: AsyncIterator[pb2.ClientToServer], context: grpc.aio.ServicerContext):
        # 约定：第一条必须是 hello
        try:
            first = await request_iterator.__anext__()
        except StopAsyncIteration:
            return
        if first.WhichOneof("msg") != "hello":
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "first message must be hello")

        hello = first.hello
        device_id = (hello.device_id or "").strip()
        if not device_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "device_id is required")

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
                c = s.query(Companion).filter(Companion.device_id == device_id).one_or_none()
            if c is None:
                c = Companion(device_id=device_id, name=hello.name or "", base_url="grpc://")
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
        await conn._out_q.put(pb2.ServerToClient(welcome=pb2.Welcome(companion_id=assigned_id)))
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

        consumer = asyncio.create_task(_consume(), name=f"companion-consume:{assigned_id}")
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

        server = grpc.aio.server(options=[("grpc.keepalive_time_ms", 20000), ("grpc.keepalive_timeout_ms", 10000)])
        pb2_grpc.add_CompanionControlServicer_to_server(CompanionControlServicer(self.registry), server)
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
        await self._server.stop(grace=None)
        self._server = None
        self.bound_addr = None
