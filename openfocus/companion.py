from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import secrets
import string
import subprocess
import sys
import threading
import time
from pathlib import Path

import grpc

from . import companion_rpc_pb2 as pb2
from . import companion_rpc_pb2_grpc as pb2_grpc


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_ms() -> int:
    return int(_utcnow().timestamp() * 1000)


def _state_path() -> Path:
    p = Path(os.environ.get("OPENFOCUS_COMPANION_STATE", "~/.openfocus/companion_state.json"))
    p = p.expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_state() -> dict:
    p = _state_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    p = _state_path()
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def _gen_device_id() -> str:
    return "dev_" + secrets.token_hex(16)


def _gen_token() -> str:
    return "tok_" + secrets.token_hex(16)


def _gen_pair_code() -> str:
    # 10 位字母或数字，用户输入友好：大写。
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))


class _CompanionRuntime:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        st = _load_state()
        self.server_companion_id = int(st.get("server_companion_id") or 0)
        self.device_id = str(st.get("device_id") or _gen_device_id())
        self.auth_token = str(st.get("auth_token") or "")
        self.name = str(st.get("name") or os.environ.get("OPENFOCUS_COMPANION_NAME") or "local")

        # 配对码：
        # - 每次用户点击“认证”后生成一个，有效期 10 分钟
        # - 每次用户尝试输入后立即轮换
        self._pair_code = str(st.get("pair_code") or "")
        self._pair_code_expire_at: dt.datetime | None = None
        # 不主动生成：由 OpenFocus 侧“点击认证”触发生成

        self._persist()

    def _persist(self) -> None:
        _save_state(
            {
                "server_companion_id": self.server_companion_id,
                "device_id": self.device_id,
                "auth_token": self.auth_token,
                "name": self.name,
                "pair_code": self._pair_code,
            }
        )

    def _emit_pair_code(self) -> None:
        # 让用户在 Companion 终端始终能看到“当前可用”的配对码。
        # 注意：该输出是用户交互提示，不参与协议。
        code = self._pair_code
        exp = self._pair_code_expire_at
        exp_s = exp.isoformat() if exp else ""
        try:
            print(f"pairing_code={code} (expires_at={exp_s})", flush=True)
        except Exception:
            # best-effort
            pass

    def _rotate_code(self, *, force: bool) -> None:
        # 测试专用：固定配对码
        fixed = os.environ.get("OPENFOCUS_TEST_PAIRING_CODE")
        if fixed:
            old = self._pair_code
            self._pair_code = str(fixed).strip().upper()
            self._pair_code_expire_at = _utcnow() + dt.timedelta(minutes=10)
            self._persist()
            # 固定码场景也只在首次/变化时输出一次，避免测试日志噪音。
            if self._pair_code != old:
                self._emit_pair_code()
            return

        if not force and self._pair_code and self._pair_code_expire_at and _utcnow() < self._pair_code_expire_at:
            return

        old = self._pair_code
        now = _utcnow()
        # 10 分钟后过期（由“点击认证”触发生成）
        next_exp = now + dt.timedelta(minutes=10)
        self._pair_code = _gen_pair_code()
        self._pair_code_expire_at = next_exp
        self._persist()
        if self._pair_code != old:
            self._emit_pair_code()

    def current_code(self, *, force_new: bool = False) -> tuple[str, dt.datetime]:
        """获取当前配对码。

        - force_new=True：用户点击“认证”时调用，生成一个新的 10 分钟有效码
        - force_new=False：仅确保未过期
        """
        with self._lock:
            if force_new or (not self._pair_code) or (self._pair_code_expire_at and _utcnow() >= self._pair_code_expire_at):
                self._rotate_code(force=True)
            else:
                self._rotate_code(force=False)
            exp = self._pair_code_expire_at or (_utcnow() + dt.timedelta(minutes=10))
            return self._pair_code, exp

    def confirm_pair(self, code: str) -> str:
        code = str(code or "").strip().upper()
        if not code:
            raise ValueError("code is required")

        with self._lock:
            # 确保当前 code 未过期
            self._rotate_code(force=False)
            cur = self._pair_code
            ok = secrets.compare_digest(code, cur)

            # 每尝试一次都轮换认证码（无论成功/失败）
            self._rotate_code(force=True)
            if not ok:
                raise ValueError("认证码错误")

            if not self.auth_token:
                self.auth_token = _gen_token()
                self._persist()
            return self.auth_token


RUNTIME = _CompanionRuntime()


def _openfocus_grpc_addr() -> str:
    return str(os.environ.get("OPENFOCUS_SERVER_GRPC_ADDR") or "127.0.0.1:17891").strip()


def _capabilities() -> list[str]:
    # MVP：先声明已实现的能力
    return ["pairing", "choose_directory"]


def _choose_directory() -> str:
    # 测试专用：直接返回指定路径（避免依赖 osascript）
    test_path = os.environ.get("OPENFOCUS_TEST_CHOOSE_DIRECTORY")
    if test_path:
        return str(test_path)

    if sys.platform != "darwin":
        raise RuntimeError("当前仅支持 macOS 目录选择器")

    script = 'POSIX path of (choose folder with prompt "选择工作目录")'
    try:
        res = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("目录选择超时")

    if res.returncode != 0:
        raise RuntimeError("未选择目录（可能已取消）")
    path = (res.stdout or "").strip()
    if not path:
        raise RuntimeError("目录选择失败")
    return path


async def run_companion(*, grpc_addr: str | None = None, stop_event: asyncio.Event | None = None) -> None:
    """运行 Companion 主循环（gRPC 客户端）。

    - Companion 作为客户端发起到 OpenFocus 的长连接
    - 用 ping/pong 确认心跳
    - 在同一条双向流里承载命令与响应
    """

    addr = (grpc_addr or _openfocus_grpc_addr()).strip()
    if not addr:
        raise RuntimeError("OPENFOCUS_SERVER_GRPC_ADDR is empty")

    stop_event = stop_event or asyncio.Event()
    backoff = 0.2

    while not stop_event.is_set():
        try:
            await _connect_once(addr, stop_event)
            backoff = 0.2
        except asyncio.CancelledError:
            raise
        except Exception:
            # 断线/失败：指数退避重连
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)


async def _connect_once(addr: str, stop_event: asyncio.Event) -> None:
    out_q: asyncio.Queue[pb2.ClientToServer] = asyncio.Queue()

    hello = pb2.Hello(
        device_id=RUNTIME.device_id,
        name=RUNTIME.name,
        capabilities=_capabilities(),
        auth_token=RUNTIME.auth_token or "",
        server_companion_id=int(getattr(RUNTIME, "server_companion_id", 0) or 0),
    )
    await out_q.put(pb2.ClientToServer(hello=hello))

    async def _outgoing() -> AsyncIterator[pb2.ClientToServer]:
        while not stop_event.is_set():
            msg = await out_q.get()
            yield msg

    async with grpc.aio.insecure_channel(addr) as channel:
        stub = pb2_grpc.CompanionControlStub(channel)
        async for msg in stub.Connect(_outgoing()):
            which = msg.WhichOneof("msg")
            if which == "welcome":
                cid = int(msg.welcome.companion_id or 0)
                if cid > 0 and cid != RUNTIME.server_companion_id:
                    RUNTIME.server_companion_id = cid
                    RUNTIME._persist()
                continue
            if which == "pairing_code":
                req = msg.pairing_code
                try:
                    code, exp = RUNTIME.current_code(force_new=bool(req.force_new))
                    resp = pb2.PairingCodeResponse(
                        request_id=req.request_id,
                        ok=True,
                        code=code,
                        expires_at=exp.isoformat(),
                    )
                except Exception as e:
                    resp = pb2.PairingCodeResponse(request_id=req.request_id, ok=False, error=str(e))
                await out_q.put(pb2.ClientToServer(pairing_code_resp=resp))
                continue
            if which == "ping":
                p = msg.ping
                await out_q.put(
                    pb2.ClientToServer(
                        pong=pb2.Pong(ts_unix_ms=_now_ms(), ping_ts_unix_ms=p.ts_unix_ms)
                    )
                )
                continue

            if which == "pair":
                req = msg.pair
                try:
                    token = RUNTIME.confirm_pair(req.code)
                    resp = pb2.PairResponse(request_id=req.request_id, ok=True, auth_token=token)
                except Exception as e:
                    resp = pb2.PairResponse(request_id=req.request_id, ok=False, error=str(e))
                await out_q.put(pb2.ClientToServer(pair_resp=resp))
                continue

            if which == "choose_directory":
                req = msg.choose_directory
                try:
                    if not (RUNTIME.auth_token or "").strip():
                        raise RuntimeError("Companion 尚未配对")
                    path = _choose_directory()
                    resp = pb2.ChooseDirectoryResponse(request_id=req.request_id, ok=True, path=path)
                except Exception as e:
                    resp = pb2.ChooseDirectoryResponse(request_id=req.request_id, ok=False, error=str(e))
                await out_q.put(pb2.ClientToServer(choose_directory_resp=resp))
                continue


def _print_banner() -> None:
    print("OpenFocus Companion (gRPC client)")
    if RUNTIME.server_companion_id:
        print(f"companion_id={RUNTIME.server_companion_id}")
    print(f"device_id={RUNTIME.device_id}")
    print(f"server_grpc={_openfocus_grpc_addr()}")
    print("pairing_code: <will be generated when you click 配对/认证 in OpenFocus UI>")


if __name__ == "__main__":
    _print_banner()
    try:
        asyncio.run(run_companion())
    except KeyboardInterrupt:
        raise SystemExit(0)
