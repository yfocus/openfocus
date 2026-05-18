# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import socket
import stat
import string
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import grpc

from ..infrastructure import env as env_config
from . import companion_rpc_pb2 as pb2
from . import companion_rpc_pb2_grpc as pb2_grpc

env_config.load_dotenv_once()

LOG = logging.getLogger("openfocus.companion")

_CONNECT_BACKOFF_INITIAL_SECONDS = 0.2
_CONNECT_BACKOFF_MAX_SECONDS = 5.0
_CONNECT_STABLE_RESET_SECONDS = 30.0
_CONNECT_LOG_LIMIT_SECONDS = 10.0


@dataclass(frozen=True)
class _ConnectOnceResult:
    connected: bool
    stable: bool
    reason: str = ""
    detail: str = ""


class _LogRateLimiter:
    def __init__(self, *, interval_seconds: float) -> None:
        self.interval_seconds = max(0.0, float(interval_seconds or 0.0))
        self._last_by_key: dict[str, dt.datetime] = {}

    def should_log(self, key: str, *, now: dt.datetime | None = None) -> bool:
        k = str(key or "default")
        t = now or _utcnow()
        last = self._last_by_key.get(k)
        if last is None or (t - last).total_seconds() >= self.interval_seconds:
            self._last_by_key[k] = t
            return True
        return False


def _setup_logging() -> None:
    level_s = (
        str(os.environ.get("OPENFOCUS_COMPANION_LOG_LEVEL") or "INFO").upper().strip()
    )
    level = getattr(logging, level_s, logging.INFO)
    # Companion 通常作为独立进程运行：直接输出到 stdout，便于用户排查。
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [companion] %(message)s",
    )


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_ms() -> int:
    return int(_utcnow().timestamp() * 1000)


def _state_path() -> Path:
    p = Path(
        os.environ.get("OPENFOCUS_COMPANION_STATE", "~/.openfocus/companion_state.json")
    )
    p = p.expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def _load_state() -> dict:
    try:
        p = _state_path()
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    try:
        p = _state_path()
        p.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # Companion can still run in restricted environments; it simply loses
        # persisted pairing state until a writable path is configured.
        pass


def _gen_device_id() -> str:
    return "dev_" + secrets.token_hex(16)


def _gen_token() -> str:
    return "tok_" + secrets.token_hex(16)


def _gen_pair_code() -> str:
    # 10 位字母或数字，用户输入友好：大写。
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(10))


def _stdout_supports_ansi() -> bool:
    try:
        if str(os.environ.get("NO_COLOR") or "").strip():
            return False
        if str(os.environ.get("TERM") or "").strip().lower() == "dumb":
            return False
        return bool(getattr(sys.stdout, "isatty", lambda: False)())
    except Exception:
        return False


def _format_pairing_code_line(code: str, exp_s: str, *, use_color: bool) -> str:
    base = f"PAIRING CODE: {code}"
    suffix = f" (expires_at={exp_s})" if exp_s else ""
    if use_color:
        label = "\033[1;30;42m PAIRING CODE \033[0m"
        value = f"\033[1;92m{code}\033[0m"
        meta = f" \033[2m(expires_at={exp_s})\033[0m" if exp_s else ""
        return f"\n{label} {value}{meta}"
    return f"*** {base} ***{suffix}"


class _CompanionRuntime:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        st = _load_state()
        self.server_companion_id = int(st.get("server_companion_id") or 0)
        self.device_id = str(st.get("device_id") or _gen_device_id())
        self.auth_token = str(st.get("auth_token") or "")
        self.name = str(
            st.get("name") or os.environ.get("OPENFOCUS_COMPANION_NAME") or "local"
        )

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
            print(
                _format_pairing_code_line(
                    code, exp_s, use_color=_stdout_supports_ansi()
                ),
                flush=True,
            )
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

        if (
            not force
            and self._pair_code
            and self._pair_code_expire_at
            and _utcnow() < self._pair_code_expire_at
        ):
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
            if (
                force_new
                or (not self._pair_code)
                or (
                    self._pair_code_expire_at and _utcnow() >= self._pair_code_expire_at
                )
            ):
                self._rotate_code(force=True)
            else:
                self._rotate_code(force=False)
            exp = self._pair_code_expire_at or (_utcnow() + dt.timedelta(minutes=10))
            try:
                LOG.info(
                    "生成/返回配对码 force_new=%s expires_at=%s",
                    bool(force_new),
                    exp.isoformat(),
                )
            except Exception:
                pass
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
                try:
                    LOG.warning("配对失败：认证码错误")
                except Exception:
                    pass
                raise ValueError("认证码错误")

            if not self.auth_token:
                self.auth_token = _gen_token()
                self._persist()
            try:
                LOG.info(
                    "配对成功：已生成/复用 auth_token（长度=%s）",
                    len(self.auth_token or ""),
                )
            except Exception:
                pass
            return self.auth_token


RUNTIME = _CompanionRuntime()
_setup_logging()


def _openfocus_grpc_addr() -> str:
    return str(
        os.environ.get("OPENFOCUS_SERVER_GRPC_ADDR") or "127.0.0.1:17891"
    ).strip()


def _capabilities() -> list[str]:
    caps = ["pairing", "choose_directory", "agent", "terminal", "runtime_hooks"]
    backend = _float_ball_backend()
    if backend != "unsupported":
        caps.extend(["system_float_ball", f"system_float_ball.{backend}"])
    return caps


def _float_ball_backend() -> str:
    configured = (
        str(os.environ.get("OPENFOCUS_SYSTEM_FLOAT_BALL_BACKEND") or "").strip().lower()
    )
    if configured:
        return configured if configured in {"test", "tk", "swift"} else "unsupported"
    disabled = (
        str(os.environ.get("OPENFOCUS_DISABLE_SYSTEM_FLOAT_BALL") or "").strip().lower()
    )
    if disabled in {"1", "true", "yes", "on"}:
        return "unsupported"
    if sys.platform == "darwin" and os.environ.get("SSH_CONNECTION") is None:
        if shutil.which("swift"):
            return "swift"
        if _python_supports_tk(_float_ball_helper_python()):
            return "tk"
    return "unsupported"


def _python_supports_tk(executable: str) -> bool:
    exe = str(executable or "").strip()
    if not exe:
        return False
    try:
        probe = subprocess.run(
            [
                exe,
                "-c",
                "import tkinter as tk; root=tk.Tk(); root.withdraw(); root.update_idletasks(); root.destroy()",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        return probe.returncode == 0
    except Exception:
        return False


def _float_ball_helper_python() -> str:
    configured = str(os.environ.get("OPENFOCUS_FLOAT_BALL_HELPER_PYTHON") or "").strip()
    if configured:
        return configured
    if _python_supports_tk(sys.executable):
        return sys.executable
    if sys.platform == "darwin" and _python_supports_tk("/usr/bin/python3"):
        return "/usr/bin/python3"
    return sys.executable


def _coco_bin() -> str:
    return str(os.environ.get("OPENFOCUS_COCO_BIN") or "coco").strip() or "coco"


def _safe_instance_id(value: str | None) -> str:
    raw = str(value or "").strip() or "default"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-._")
    return safe or "default"


def _openfocus_instance_id() -> str:
    return _safe_instance_id(os.environ.get("OPENFOCUS_INSTANCE_ID") or "default")


def _hook_sock_path() -> Path:
    configured = str(os.environ.get("OPENFOCUS_HOOK_SOCK") or "").strip()
    if configured:
        p = Path(configured)
    else:
        instance_id = _openfocus_instance_id()
        if instance_id == "default":
            p = Path("~/.openfocus/hooks.sock")
        else:
            p = Path(f"~/.openfocus/hooks-{instance_id}.sock")
    return p.expanduser()


def _hook_spool_dir() -> Path:
    configured = str(os.environ.get("OPENFOCUS_HOOK_SPOOL_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(f"/tmp/openfocus-agent-hooks-{os.getuid()}") / _openfocus_instance_id()


def _accept_hook_instance(origin_instance_id: object) -> bool:
    own = _openfocus_instance_id()
    origin = (
        _safe_instance_id(str(origin_instance_id or "")) if origin_instance_id else ""
    )
    if origin:
        return origin == own
    return own == "default"


def _payload_json(value: object, *, max_bytes: int) -> tuple[str, bool]:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except Exception:
        text = json.dumps({"value": str(value)}, ensure_ascii=False)
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    clipped = raw[:max_bytes]
    try:
        return clipped.decode("utf-8"), True
    except Exception:
        return clipped.decode("utf-8", errors="replace"), True


def _hook_text(value: object, *, max_len: int = 4000) -> str:
    text = str(value or "").strip()
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _hook_payload_field(payload: object, *keys: str) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _hook_text(value, max_len=4000)
    return ""


def _hook_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _hook_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def _handle_hook_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    out_q: asyncio.Queue[pb2.ClientToServer],
) -> None:
    max_bytes = int(os.environ.get("OPENFOCUS_HOOK_MAX_BYTES") or str(256 * 1024))
    max_bytes = max(4096, min(max_bytes, 2 * 1024 * 1024))
    try:
        raw = await reader.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except Exception:
            envelope = {"payload": raw.decode("utf-8", errors="replace")}
        if not isinstance(envelope, dict):
            envelope = {"payload": envelope}

        await _enqueue_hook_envelope(
            envelope, out_q, max_bytes=max_bytes, truncated=truncated
        )
    except Exception as e:
        try:
            LOG.exception("hook signal 处理失败：%s", e)
        except Exception:
            pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


async def _enqueue_hook_envelope(
    envelope: dict[str, object],
    out_q: asyncio.Queue[pb2.ClientToServer],
    *,
    max_bytes: int,
    truncated: bool = False,
) -> bool:
    payload = envelope.get("payload")
    runtime = (
        envelope.get("runtime") if isinstance(envelope.get("runtime"), dict) else {}
    )
    if payload is None:
        payload = {}
    if not _accept_hook_instance(runtime.get("openfocus_instance_id")):
        try:
            LOG.debug(
                "忽略非本实例 hook signal：own=%s origin=%s",
                _openfocus_instance_id(),
                runtime.get("openfocus_instance_id") or "",
            )
        except Exception:
            pass
        return False
    raw_kind = _hook_text(
        envelope.get("hook_kind")
        or envelope.get("kind")
        or _hook_payload_field(payload, "hook_event_name", "event")
        or "unknown",
        max_len=128,
    )
    agent_runtime = _hook_text(
        envelope.get("agent_runtime") or envelope.get("agent") or "agent",
        max_len=64,
    )
    payload_text, payload_truncated = _payload_json(payload, max_bytes=max_bytes)
    signal = pb2.AgentRuntimeSignal(
        signal_id=str(uuid.uuid4()),
        raw_kind=raw_kind,
        agent_runtime=agent_runtime,
        session_id=_hook_text(
            runtime.get("openfocus_session_id")
            or runtime.get("session_id")
            or _hook_payload_field(payload, "session_id"),
            max_len=128,
        ),
        turn_id=_hook_text(_hook_payload_field(payload, "turn_id"), max_len=64),
        task_public_id=_hook_text(
            runtime.get("openfocus_task_id")
            or runtime.get("task_public_id")
            or runtime.get("task_id")
            or _hook_payload_field(payload, "task_public_id", "task_id"),
            max_len=36,
        ),
        terminal_id=_hook_text(
            runtime.get("openfocus_terminal_id")
            or runtime.get("terminal_id")
            or _hook_payload_field(payload, "terminal_id"),
            max_len=64,
        ),
        cwd=_hook_text(runtime.get("cwd"), max_len=4000),
        tty=_hook_text(runtime.get("tty"), max_len=256),
        ppid=_hook_int(runtime.get("ppid")),
        runtime_ts=_hook_float(envelope.get("runtime_ts") or envelope.get("ts")),
        source="hook",
        payload_json=payload_text,
        payload_truncated=bool(truncated or payload_truncated),
    )
    await out_q.put(pb2.ClientToServer(runtime_signal=signal))
    return True


async def _drain_hook_spool_once(out_q: asyncio.Queue[pb2.ClientToServer]) -> int:
    spool_dir = _hook_spool_dir()
    max_bytes = int(os.environ.get("OPENFOCUS_HOOK_MAX_BYTES") or str(256 * 1024))
    max_bytes = max(4096, min(max_bytes, 2 * 1024 * 1024))
    try:
        spool_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        try:
            LOG.warning("OpenFocus hook spool 创建失败：%s", e)
        except Exception:
            pass
        return 0

    processed = 0
    for path in sorted(spool_dir.glob("*.json"))[:100]:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            continue
        except Exception as e:
            try:
                LOG.debug("读取 hook spool 失败：path=%s error=%s", path, e)
            except Exception:
                pass
            continue
        with contextlib.suppress(Exception):
            path.unlink()
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except Exception:
            envelope = {"payload": raw.decode("utf-8", errors="replace")}
        if not isinstance(envelope, dict):
            envelope = {"payload": envelope}
        if await _enqueue_hook_envelope(
            envelope, out_q, max_bytes=max_bytes, truncated=truncated
        ):
            processed += 1
    return processed


async def _hook_spool_poller(
    out_q: asyncio.Queue[pb2.ClientToServer], stop_event: asyncio.Event
) -> None:
    poll_s = float(os.environ.get("OPENFOCUS_HOOK_SPOOL_POLL_SECONDS") or "0.25")
    poll_s = max(0.05, min(poll_s, 5.0))
    while not stop_event.is_set():
        with contextlib.suppress(Exception):
            await _drain_hook_spool_once(out_q)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=poll_s)


async def _start_hook_server(
    out_q: asyncio.Queue[pb2.ClientToServer],
) -> asyncio.AbstractServer | None:
    if not hasattr(asyncio, "start_unix_server"):
        return None
    sock_path = _hook_sock_path()
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        try:
            mode = sock_path.stat().st_mode
            if not stat.S_ISSOCK(mode):
                raise RuntimeError(
                    f"hook socket path exists and is not a socket: {sock_path}"
                )
            sock_path.unlink()
        except FileNotFoundError:
            pass
    server = await asyncio.start_unix_server(
        lambda r, w: _handle_hook_client(r, w, out_q), path=str(sock_path)
    )
    with contextlib.suppress(Exception):
        os.chmod(sock_path, 0o600)
    try:
        LOG.info("OpenFocus hook socket listening: %s", sock_path)
    except Exception:
        pass
    return server


class _AgentSessionRuntime:
    def __init__(
        self, *, session_id: str, root_path: str, agent_type: str, task_public_id: str
    ) -> None:
        self.session_id = session_id
        self.root_path = root_path
        self.agent_type = agent_type
        self.task_public_id = task_public_id
        self.created_at = _utcnow()
        self._running: asyncio.Task | None = None


class _AgentManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, _AgentSessionRuntime] = {}

    async def start(
        self, *, session_id: str, root_path: str, agent_type: str, task_public_id: str
    ) -> str:
        sid = (session_id or "").strip() or str(uuid.uuid4())
        async with self._lock:
            self._sessions[sid] = _AgentSessionRuntime(
                session_id=sid,
                root_path=str(root_path or ""),
                agent_type=str(agent_type or "trae-cli"),
                task_public_id=str(task_public_id or ""),
            )
        return sid

    async def terminate(self, *, session_id: str) -> None:
        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("session_id is required")
        async with self._lock:
            rt = self._sessions.pop(sid, None)
        if rt and rt._running and not rt._running.done():
            rt._running.cancel()

    async def send(
        self,
        *,
        request_id: str,
        session_id: str,
        prompt: str,
        out_q: asyncio.Queue[pb2.ClientToServer],
    ) -> None:
        rid = (request_id or "").strip()
        if not rid:
            rid = str(uuid.uuid4())
        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("session_id is required")

        async with self._lock:
            rt = self._sessions.get(sid)
            if rt is None:
                raise ValueError("session not found")
            if rt._running and not rt._running.done():
                raise RuntimeError("session is busy")

            try:
                LOG.info(
                    "准备执行 agent：session_id=%s agent_type=%s root_path=%s task=%s",
                    rt.session_id,
                    rt.agent_type,
                    rt.root_path,
                    rt.task_public_id,
                )
            except Exception:
                pass

            rt._running = asyncio.create_task(
                self._run_print(rid=rid, rt=rt, prompt=str(prompt or ""), out_q=out_q),
                name=f"agent-send:{sid}",
            )

    async def _run_print(
        self,
        *,
        rid: str,
        rt: _AgentSessionRuntime,
        prompt: str,
        out_q: asyncio.Queue[pb2.ClientToServer],
    ) -> None:
        # 测试兜底：不依赖 coco，直接回显 prompt。
        if (os.environ.get("OPENFOCUS_TEST_AGENT_ECHO") or "").strip() == "1":
            await out_q.put(
                pb2.ClientToServer(
                    agent_chunk=pb2.AgentChunk(
                        request_id=rid,
                        session_id=rt.session_id,
                        ok=True,
                        text=prompt,
                        done=True,
                    )
                )
            )
            return

        cmd = [
            _coco_bin(),
            "--print",
            "--query-timeout",
            str(os.environ.get("OPENFOCUS_COCO_QUERY_TIMEOUT") or "300s"),
            "--session-id",
            rt.session_id,
            "-w",
            rt.root_path,
            prompt,
        ]

        # coco/底层 gRPC 可能会输出一些 info 级别的内部日志到 stderr（例如 ev_poll_posix.cc）。
        # 这里尽量压低噪音，避免污染对话回显。
        env = os.environ.copy()
        env.setdefault("GRPC_VERBOSITY", "ERROR")
        env.setdefault("ABSL_MIN_LOG_LEVEL", "2")
        env.setdefault("GLOG_minloglevel", "2")
        env["OPENFOCUS_INSTANCE_ID"] = _openfocus_instance_id()
        env["OPENFOCUS_HOOK_SOCK"] = str(_hook_sock_path())
        env["OPENFOCUS_HOOK_SPOOL_DIR"] = str(_hook_spool_dir())
        env["OPENFOCUS_TASK_ID"] = str(rt.task_public_id or "")
        env["OPENFOCUS_AGENT_SESSION_ID"] = str(rt.session_id or "")

        try:
            LOG.info("启动命令：%s", " ".join([str(x) for x in cmd[:6]]) + " ...")
        except Exception:
            pass

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            try:
                LOG.error("coco 不存在：%s", _coco_bin())
            except Exception:
                pass
            await out_q.put(
                pb2.ClientToServer(
                    agent_chunk=pb2.AgentChunk(
                        request_id=rid,
                        session_id=rt.session_id,
                        ok=False,
                        done=True,
                        error=f"coco not found: {_coco_bin()}",
                    )
                )
            )
            return
        except Exception as e:
            try:
                LOG.exception("启动命令失败：%s", e)
            except Exception:
                pass
            await out_q.put(
                pb2.ClientToServer(
                    agent_chunk=pb2.AgentChunk(
                        request_id=rid,
                        session_id=rt.session_id,
                        ok=False,
                        done=True,
                        error=str(e),
                    )
                )
            )
            return

        def _is_noise_line(line: str) -> bool:
            s = (line or "").strip()
            if not s:
                return True
            # gRPC C-core fork/poll 提示：常见于 fork 后仍保留旧 fd 的 info 日志。
            if "ev_poll_posix.cc" in s and "FD from fork parent" in s:
                return True
            if "FD from fork parent still in poll list" in s:
                return True
            return False

        async def _pump(stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            buf = ""
            while True:
                chunk = await stream.read(1024)
                if not chunk:
                    # flush remaining
                    if buf and not _is_noise_line(buf):
                        await out_q.put(
                            pb2.ClientToServer(
                                agent_chunk=pb2.AgentChunk(
                                    request_id=rid,
                                    session_id=rt.session_id,
                                    ok=True,
                                    text=buf,
                                    done=False,
                                )
                            )
                        )
                    return
                try:
                    text = chunk.decode("utf-8")
                except Exception:
                    text = chunk.decode("utf-8", errors="replace")

                if not text:
                    continue

                buf += text
                # 按行过滤噪音（保留换行）
                while True:
                    i = buf.find("\n")
                    if i < 0:
                        break
                    line = buf[: i + 1]
                    buf = buf[i + 1 :]
                    if _is_noise_line(line):
                        continue
                    await out_q.put(
                        pb2.ClientToServer(
                            agent_chunk=pb2.AgentChunk(
                                request_id=rid,
                                session_id=rt.session_id,
                                ok=True,
                                text=line,
                                done=False,
                            )
                        )
                    )

        # stdout/stderr 都作为可见文本 chunk 上报（便于排查）
        pump_out = asyncio.create_task(
            _pump(proc.stdout), name=f"agent-stdout:{rt.session_id}"
        )
        pump_err = asyncio.create_task(
            _pump(proc.stderr), name=f"agent-stderr:{rt.session_id}"
        )

        try:
            timeout_s = int(os.environ.get("OPENFOCUS_AGENT_TIMEOUT_SECONDS") or "600")
            timeout_s = max(10, min(timeout_s, 3600))
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=float(timeout_s))
            except asyncio.TimeoutError:
                try:
                    LOG.error(
                        "agent 超时：timeout_s=%s session_id=%s",
                        timeout_s,
                        rt.session_id,
                    )
                except Exception:
                    pass
                try:
                    proc.kill()
                except Exception:
                    pass
                # 尽量等待 pump 结束，避免任务泄露
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        asyncio.gather(pump_out, pump_err), timeout=1.0
                    )
                await out_q.put(
                    pb2.ClientToServer(
                        agent_chunk=pb2.AgentChunk(
                            request_id=rid,
                            session_id=rt.session_id,
                            ok=False,
                            done=True,
                            error=f"agent timeout after {timeout_s}s",
                        )
                    )
                )
                return

            await asyncio.gather(pump_out, pump_err)
        except asyncio.CancelledError:
            try:
                proc.kill()
            except Exception:
                pass
            raise

        try:
            LOG.info("命令结束：exit_code=%s session_id=%s", rc, rt.session_id)
        except Exception:
            pass

        if rc == 0:
            await out_q.put(
                pb2.ClientToServer(
                    agent_chunk=pb2.AgentChunk(
                        request_id=rid,
                        session_id=rt.session_id,
                        ok=True,
                        done=True,
                    )
                )
            )
        else:
            await out_q.put(
                pb2.ClientToServer(
                    agent_chunk=pb2.AgentChunk(
                        request_id=rid,
                        session_id=rt.session_id,
                        ok=False,
                        done=True,
                        error=f"coco exit code: {rc}",
                    )
                )
            )


class _TerminalSession:
    def __init__(self, *, terminal_id: str, root_path: str) -> None:
        self.terminal_id = terminal_id
        self.root_path = root_path
        self.created_at = _utcnow()
        self.backend = "ttyd"
        self.connect_url = ""
        self.process: asyncio.subprocess.Process | None = None
        self.tmux_session = ""
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _safe_tmux_session_name(tid: str) -> str:
    raw = "".join(
        ch if (ch.isalnum() or ch in {"_", "-"}) else "_" for ch in str(tid or "")
    )
    return ("of_" + raw)[:80]


async def _wait_tcp_ready(
    host: str, port: int, proc: asyncio.subprocess.Process
) -> None:
    deadline = asyncio.get_running_loop().time() + 5.0
    last_err: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        if proc.returncode is not None:
            raise RuntimeError(f"ttyd exited early with code {proc.returncode}")
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            with contextlib.suppress(Exception):
                reader.feed_eof()
            return
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.05)
    raise RuntimeError(f"ttyd did not become ready on {host}:{port}: {last_err}")


async def _ensure_tmux_terminal_session(
    *,
    tmux_bin: str,
    tmux_name: str,
    root_path: str,
    shell: str,
    mouse: bool = True,
    env: dict[str, str] | None = None,
    unset_env: list[str] | None = None,
) -> None:
    """Create/reuse a tmux session and configure wheel/copy behavior.

    Scroll mode keeps tmux mouse on so wheel scrolls tmux history.
    Copy mode can turn it off later so users can drag-select text in the browser.
    """

    has = await asyncio.create_subprocess_exec(
        tmux_bin,
        "has-session",
        "-t",
        tmux_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await has.wait()

    if has.returncode != 0:
        shell_cmd = shell
        env_parts: list[str] = []
        for key in unset_env or []:
            clean_key = str(key or "").strip()
            if clean_key:
                env_parts.extend(["-u", clean_key])
        for key, value in (env or {}).items():
            clean_key = str(key or "").strip()
            if clean_key:
                env_parts.append(f"{clean_key}={shlex.quote(str(value or ''))}")
        if env_parts:
            assignments = " ".join(
                shlex.quote(part) if part.startswith("-") else part
                for part in env_parts
            )
            shell_cmd = f"env {assignments} {shlex.quote(shell)}"
        create = await asyncio.create_subprocess_exec(
            tmux_bin,
            "new-session",
            "-d",
            "-s",
            tmux_name,
            "-c",
            root_path,
            shell_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await create.wait()
        if create.returncode != 0:
            raise RuntimeError("tmux new-session failed")

    for key in unset_env or []:
        clean_key = str(key or "").strip()
        if not clean_key:
            continue
        unset_proc = await asyncio.create_subprocess_exec(
            tmux_bin,
            "set-environment",
            "-u",
            "-t",
            tmux_name,
            clean_key,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await unset_proc.wait()
        if unset_proc.returncode != 0:
            raise RuntimeError(f"tmux unset-environment failed: {clean_key}")

    for key, value in (env or {}).items():
        clean_key = str(key or "").strip()
        if not clean_key:
            continue
        set_env = await asyncio.create_subprocess_exec(
            tmux_bin,
            "set-environment",
            "-t",
            tmux_name,
            clean_key,
            str(value or ""),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await set_env.wait()
        if set_env.returncode != 0:
            raise RuntimeError(f"tmux set-environment failed: {clean_key}")

    opt = await asyncio.create_subprocess_exec(
        tmux_bin,
        "set-option",
        "-t",
        tmux_name,
        "mouse",
        "on" if bool(mouse) else "off",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await opt.wait()
    if opt.returncode != 0:
        raise RuntimeError("tmux set-option mouse failed")


class _TerminalManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, _TerminalSession] = {}

    async def start(
        self,
        *,
        terminal_id: str,
        root_path: str,
        base_path: str,
        task_public_id: str = "",
        out_q: asyncio.Queue[pb2.ClientToServer],
    ) -> _TerminalSession:
        tid = (terminal_id or "").strip() or str(uuid.uuid4())
        rp = str(root_path or "").strip()
        if not rp:
            raise ValueError("root_path is required")

        try:
            LOG.info("启动 terminal：terminal_id=%s root_path=%s", tid, rp)
        except Exception:
            pass

        async with self._lock:
            if tid in self._sessions:
                return self._sessions[tid]
            sess = _TerminalSession(terminal_id=tid, root_path=rp)
            self._sessions[tid] = sess

        # 测试兜底：不依赖 PTY，回显输入（OpenFocus 可通过 input 接口触发 output）。
        if (os.environ.get("OPENFOCUS_TEST_TERMINAL_ECHO") or "").strip() == "1":
            await out_q.put(
                pb2.ClientToServer(
                    terminal_output=pb2.TerminalOutput(
                        terminal_id=tid, data=b"terminal-ready\n", closed=False
                    )
                )
            )
            return sess

        ttyd_bin = str(os.environ.get("OPENFOCUS_TTYD_BIN") or "ttyd").strip() or "ttyd"
        tmux_bin = str(os.environ.get("OPENFOCUS_TMUX_BIN") or "tmux").strip() or "tmux"
        if shutil.which(ttyd_bin) is None:
            raise RuntimeError(f"ttyd not found: {ttyd_bin}")
        if shutil.which(tmux_bin) is None:
            raise RuntimeError(f"tmux not found: {tmux_bin}")

        port = _find_free_port()
        tmux_name = _safe_tmux_session_name(tid)
        sess.backend = "ttyd"
        sess.connect_url = f"http://127.0.0.1:{port}/"
        sess.tmux_session = tmux_name

        shell = os.environ.get("SHELL") or "/bin/zsh"
        await _ensure_tmux_terminal_session(
            tmux_bin=tmux_bin,
            tmux_name=tmux_name,
            root_path=rp,
            shell=shell,
            mouse=True,
            env={
                "TERM": "xterm-256color",
                "COLORTERM": "truecolor",
                "CLICOLOR": "1",
                "OPENFOCUS_INSTANCE_ID": _openfocus_instance_id(),
                "OPENFOCUS_HOOK_SOCK": str(_hook_sock_path()),
                "OPENFOCUS_HOOK_SPOOL_DIR": str(_hook_spool_dir()),
                "OPENFOCUS_TASK_ID": str(task_public_id or ""),
                "OPENFOCUS_TERMINAL_ID": tid,
            },
            unset_env=["NO_COLOR"],
        )
        cmd = [
            ttyd_bin,
            "--interface",
            "127.0.0.1",
            "--port",
            str(port),
            "--writable",
            "--base-path",
            str(base_path or "/").rstrip("/") or "/",
            "--terminal-type",
            "xterm-256color",
            "--client-option",
            'theme={"background":"#000000"}',
            "--client-option",
            "rendererType=dom",
            "--client-option",
            "macOptionClickForcesSelection=true",
            "--client-option",
            "rightClickSelectsWord=true",
            tmux_bin,
            "attach-session",
            "-t",
            tmux_name,
        ]
        try:
            LOG.info(
                "启动 ttyd terminal：terminal_id=%s port=%s tmux=%s",
                tid,
                port,
                tmux_name,
            )
        except Exception:
            pass
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=rp,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        sess.process = proc
        try:
            await _wait_tcp_ready("127.0.0.1", port, proc)
        except Exception:
            with contextlib.suppress(Exception):
                if proc.returncode is None:
                    proc.terminate()
            async with self._lock:
                self._sessions.pop(tid, None)
            raise
        return sess

    async def stop(
        self, *, terminal_id: str, out_q: asyncio.Queue[pb2.ClientToServer]
    ) -> None:
        tid = (terminal_id or "").strip()
        if not tid:
            raise ValueError("terminal_id is required")
        try:
            LOG.info("停止 terminal：terminal_id=%s", tid)
        except Exception:
            pass
        async with self._lock:
            sess = self._sessions.pop(tid, None)
        if sess is None:
            return

        if (os.environ.get("OPENFOCUS_TEST_TERMINAL_ECHO") or "").strip() == "1":
            await out_q.put(
                pb2.ClientToServer(
                    terminal_output=pb2.TerminalOutput(
                        terminal_id=tid, data=b"", closed=True
                    )
                )
            )
            return

        try:
            if sess.process is not None and sess.process.returncode is None:
                sess.process.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(sess.process.wait(), timeout=2.0)
                if sess.process.returncode is None:
                    sess.process.kill()
        except Exception:
            pass
        if sess.tmux_session:
            tmux_bin = (
                str(os.environ.get("OPENFOCUS_TMUX_BIN") or "tmux").strip() or "tmux"
            )
            if shutil.which(tmux_bin) is not None:
                with contextlib.suppress(Exception):
                    proc = await asyncio.create_subprocess_exec(
                        tmux_bin,
                        "kill-session",
                        "-t",
                        sess.tmux_session,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
        sess.close()
        await out_q.put(
            pb2.ClientToServer(
                terminal_output=pb2.TerminalOutput(
                    terminal_id=tid, data=b"", closed=True
                )
            )
        )

    async def input(
        self, *, terminal_id: str, data: bytes, out_q: asyncio.Queue[pb2.ClientToServer]
    ) -> None:
        tid = (terminal_id or "").strip()
        if not tid:
            raise ValueError("terminal_id is required")
        async with self._lock:
            sess = self._sessions.get(tid)
        if sess is None:
            raise ValueError("terminal not found")
        raw = bytes(data or b"")

        # 输入频率可能很高，这里只在 debug 时输出，避免日志爆炸。
        if LOG.isEnabledFor(logging.DEBUG):
            try:
                LOG.debug("terminal_input：terminal_id=%s nbytes=%s", tid, len(raw))
            except Exception:
                pass

        if (os.environ.get("OPENFOCUS_TEST_TERMINAL_ECHO") or "").strip() == "1":
            # echo：直接把输入回传
            await out_q.put(
                pb2.ClientToServer(
                    terminal_output=pb2.TerminalOutput(
                        terminal_id=tid, data=raw, closed=False
                    )
                )
            )
            return

        await self._tmux_input(sess=sess, raw=raw)

    async def _tmux_input(self, *, sess: _TerminalSession, raw: bytes) -> None:
        if not sess.tmux_session:
            raise ValueError("tmux session missing")
        if not raw:
            return
        tmux_bin = str(os.environ.get("OPENFOCUS_TMUX_BIN") or "tmux").strip() or "tmux"
        if shutil.which(tmux_bin) is None:
            raise RuntimeError(f"tmux not found: {tmux_bin}")

        data = bytes(raw or b"")
        submit = False
        start = b"\x1b[200~"
        end = b"\x1b[201~"
        i = data.find(start)
        j = data.find(end, i + len(start)) if i >= 0 else -1
        if i >= 0 and j >= 0:
            paste = data[i + len(start) : j]
            suffix = data[j + len(end) :]
            submit = (b"\r" in suffix) or (b"\n" in suffix)
        else:
            paste = data.replace(b"\r", b"").replace(b"\n", b"")
            submit = (b"\r" in data) or (b"\n" in data)

        buf_name = f"openfocus_{sess.terminal_id[:24]}"
        if paste:
            proc = await asyncio.create_subprocess_exec(
                tmux_bin,
                "load-buffer",
                "-b",
                buf_name,
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate(paste)
            if proc.returncode != 0:
                raise RuntimeError("tmux load-buffer failed")
            proc = await asyncio.create_subprocess_exec(
                tmux_bin,
                "paste-buffer",
                "-d",
                "-b",
                buf_name,
                "-t",
                sess.tmux_session,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode != 0:
                raise RuntimeError("tmux paste-buffer failed")
        if submit:
            proc = await asyncio.create_subprocess_exec(
                tmux_bin,
                "send-keys",
                "-t",
                sess.tmux_session,
                "Enter",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode != 0:
                raise RuntimeError("tmux send-keys failed")

    async def resize(self, *, terminal_id: str, cols: int, rows: int) -> None:
        tid = (terminal_id or "").strip()
        if not tid:
            raise ValueError("terminal_id is required")
        async with self._lock:
            sess = self._sessions.get(tid)
        if sess is None:
            raise ValueError("terminal not found")
        if (os.environ.get("OPENFOCUS_TEST_TERMINAL_ECHO") or "").strip() == "1":
            return
        return

    async def set_mouse_mode(self, *, terminal_id: str, enabled: bool) -> bool:
        tid = (terminal_id or "").strip()
        if not tid:
            raise ValueError("terminal_id is required")
        async with self._lock:
            sess = self._sessions.get(tid)
        if (os.environ.get("OPENFOCUS_TEST_TERMINAL_ECHO") or "").strip() == "1":
            return bool(enabled)
        tmux_name = ""
        if sess is not None:
            tmux_name = str(sess.tmux_session or "").strip()
        if not tmux_name:
            tmux_name = _safe_tmux_session_name(tid)
        tmux_bin = str(os.environ.get("OPENFOCUS_TMUX_BIN") or "tmux").strip() or "tmux"
        if shutil.which(tmux_bin) is None:
            raise RuntimeError(f"tmux not found: {tmux_bin}")
        has = await asyncio.create_subprocess_exec(
            tmux_bin,
            "has-session",
            "-t",
            tmux_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await has.wait()
        if has.returncode != 0:
            raise ValueError("terminal not found")
        proc = await asyncio.create_subprocess_exec(
            tmux_bin,
            "set-option",
            "-t",
            tmux_name,
            "mouse",
            "on" if bool(enabled) else "off",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("tmux set-option mouse failed")
        return bool(enabled)


def _protocol_sock_path() -> Path:
    configured = str(os.environ.get("OPENFOCUS_PROTOCOL_SOCK") or "").strip()
    if configured:
        p = Path(configured)
    else:
        instance_id = _openfocus_instance_id()
        if instance_id == "default":
            p = Path("~/.openfocus/protocol.sock")
        else:
            p = Path(f"~/.openfocus/protocol-{instance_id}.sock")
    return p.expanduser()


def send_protocol_url(url: str) -> bool:
    payload = str(url or "").strip()
    if not payload:
        return False
    sock_path = _protocol_sock_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(str(sock_path))
            client.sendall(payload.encode("utf-8")[:8192])
        return True
    except Exception as exc:
        try:
            LOG.error("openfocus protocol delivery failed: %s", exc)
        except Exception:
            pass
        return False


def _parse_bind_protocol_url(url: str) -> str:
    raw = str(url or "").strip()
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme != "openfocus":
        raise ValueError("unsupported protocol")
    action = (parsed.netloc or parsed.path.lstrip("/")).strip()
    if action != "bind":
        raise ValueError("unsupported openfocus action")
    qs = urllib.parse.parse_qs(parsed.query)
    nonce = str((qs.get("nonce") or [""])[0] or "").strip()
    instance_id = str((qs.get("instance_id") or [""])[0] or "").strip()
    if instance_id and _safe_instance_id(instance_id) != _openfocus_instance_id():
        raise ValueError("protocol event belongs to another OpenFocus instance")
    if not nonce:
        raise ValueError("nonce is required")
    return nonce


async def _handle_protocol_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    out_q: asyncio.Queue[pb2.ClientToServer],
) -> None:
    try:
        data = await reader.read(8192)
        nonce = _parse_bind_protocol_url(data.decode("utf-8", errors="replace"))
        await out_q.put(
            pb2.ClientToServer(
                browser_bind_proof=pb2.BrowserBindProof(
                    nonce=nonce,
                    companion_id=int(getattr(RUNTIME, "server_companion_id", 0) or 0),
                    protocol="openfocus://",
                    received_ts_unix_ms=_now_ms(),
                )
            )
        )
    except Exception as exc:
        try:
            LOG.warning("openfocus protocol event ignored: %s", exc)
        except Exception:
            pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _start_protocol_server(
    out_q: asyncio.Queue[pb2.ClientToServer],
) -> asyncio.AbstractServer:
    sock_path = _protocol_sock_path()
    try:
        sock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    with contextlib.suppress(FileNotFoundError):
        sock_path.unlink()
    server = await asyncio.start_unix_server(
        lambda r, w: _handle_protocol_client(r, w, out_q), path=str(sock_path)
    )
    with contextlib.suppress(Exception):
        sock_path.chmod(0o600)
    return server


@dataclass
class _FloatBallSession:
    browser_session_id: str
    backend: str
    proc: subprocess.Popen | None = None
    summary_json: str = ""


async def _wait_for_float_ball_ready(
    proc: subprocess.Popen, ready_path: Path
) -> None:
    timeout_s = float(os.environ.get("OPENFOCUS_FLOAT_BALL_READY_TIMEOUT_SECONDS") or "8")
    timeout_s = max(0.5, min(timeout_s, 30.0))
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if ready_path.exists():
            return
        if proc.poll() is not None:
            err = ""
            with contextlib.suppress(Exception):
                if proc.stderr is not None:
                    err = proc.stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                "system float ball helper exited during startup"
                + (f": {err}" if err else f" (exit={proc.returncode})")
            )
        await asyncio.sleep(0.05)
    raise RuntimeError(
        f"system float ball helper did not become ready within {timeout_s:.1f}s"
    )


class _FloatBallManager:
    def __init__(self) -> None:
        self._sessions: dict[str, _FloatBallSession] = {}

    async def start(
        self, *, browser_session_id: str, openfocus_base_url: str, summary_json: str
    ) -> str:
        sid = str(browser_session_id or "").strip()
        if not sid:
            raise ValueError("browser_session_id is required")
        if not (RUNTIME.auth_token or "").strip():
            raise RuntimeError("Companion 尚未配对")
        backend = _float_ball_backend()
        if backend == "unsupported":
            raise RuntimeError("system float ball is unsupported in this environment")
        await self.stop(browser_session_id=sid)
        if backend == "test":
            self._sessions[sid] = _FloatBallSession(
                browser_session_id=sid, backend=backend, summary_json=summary_json
            )
            return backend
        env = os.environ.copy()
        env["OPENFOCUS_FLOAT_BALL_SUMMARY_JSON"] = str(summary_json or "")
        env["OPENFOCUS_FLOAT_BALL_BACKEND"] = backend
        ready_path = Path(tempfile.gettempdir()) / f"openfocus-float-ball-ready-{uuid.uuid4().hex}"
        env["OPENFOCUS_FLOAT_BALL_READY_FILE"] = str(ready_path)
        helper_python = _float_ball_helper_python()
        proc = subprocess.Popen(
            [
                helper_python,
                str(Path(__file__).with_name("float_ball_helper.py")),
                "--browser-session-id",
                sid,
                "--openfocus-base-url",
                str(openfocus_base_url or ""),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            await _wait_for_float_ball_ready(proc, ready_path)
        except Exception:
            with contextlib.suppress(Exception):
                if proc.poll() is None:
                    proc.terminate()
            raise
        finally:
            with contextlib.suppress(FileNotFoundError):
                ready_path.unlink()
        self._sessions[sid] = _FloatBallSession(
            browser_session_id=sid,
            backend=backend,
            proc=proc,
            summary_json=summary_json,
        )
        return backend

    async def update(self, *, browser_session_id: str, summary_json: str) -> None:
        sid = str(browser_session_id or "").strip()
        sess = self._sessions.get(sid)
        if sess is None:
            return
        sess.summary_json = str(summary_json or "")

    async def stop(self, *, browser_session_id: str) -> None:
        sid = str(browser_session_id or "").strip()
        sess = self._sessions.pop(sid, None)
        if sess is None or sess.proc is None:
            return
        proc = sess.proc
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            await asyncio.to_thread(proc.wait, 2)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()

    async def stop_all(self) -> None:
        for sid in list(self._sessions):
            await self.stop(browser_session_id=sid)


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


def _resolve_inside_root(*, root_path: str, rel_path: str) -> Path:
    root = Path(root_path).expanduser()
    if not root.is_absolute():
        raise ValueError("root_path must be absolute")
    if not root.exists() or not root.is_dir():
        raise ValueError("root_path not found")

    raw = str(rel_path or "")
    # 安全：不接受绝对路径（否则会被当成相对路径去读，语义不清且容易误用）。
    try:
        if raw.startswith("/") or Path(raw).is_absolute():
            raise ValueError("invalid path")
    except Exception:
        # Path(raw) 解析异常时也按 invalid path 处理
        raise ValueError("invalid path")

    rel = raw.lstrip("/")
    p = root / rel

    # 先 normalize，再做 resolve（包含 symlink）检查越界
    try:
        rp = p.resolve(strict=False)
        rr = root.resolve(strict=True)
    except Exception:
        raise ValueError("invalid path")
    try:
        rp.relative_to(rr)
    except Exception:
        raise ValueError("path traversal is not allowed")
    return p


def _list_dir(*, root_path: str, rel_path: str) -> tuple[str, list[pb2.FileEntry]]:
    p = _resolve_inside_root(root_path=root_path, rel_path=rel_path)
    if not p.exists():
        raise ValueError("path not found")
    if not p.is_dir():
        raise ValueError("not a directory")

    entries: list[pb2.FileEntry] = []
    rr = Path(root_path).expanduser().resolve(strict=True)
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        try:
            st = child.lstat()
        except Exception:
            continue
        kind = "dir" if child.is_dir() else "file"
        try:
            rel = str(child.resolve(strict=False).relative_to(rr))
        except Exception:
            # 如果 resolve 后越界，直接跳过
            continue
        entries.append(
            pb2.FileEntry(
                name=child.name,
                rel_path=rel,
                kind=kind,
                size=int(getattr(st, "st_size", 0) or 0),
                mtime=float(getattr(st, "st_mtime", 0.0) or 0.0),
            )
        )
    return str(rel_path or ""), entries


def _read_text(
    *, root_path: str, rel_path: str, max_bytes: int
) -> tuple[str, str, bool, str]:
    p = _resolve_inside_root(root_path=root_path, rel_path=rel_path)
    if not p.exists() or not p.is_file():
        raise ValueError("file not found")
    max_bytes = int(max_bytes or 0)
    if max_bytes <= 0:
        max_bytes = 256 * 1024
    max_bytes = min(max_bytes, 2 * 1024 * 1024)
    raw = p.read_bytes()
    truncated = len(raw) > max_bytes
    raw2 = raw[:max_bytes]
    mime, _ = mimetypes.guess_type(str(p))
    mime = mime or "text/plain"
    # UTF-8 优先，失败则 replace
    try:
        text = raw2.decode("utf-8")
    except Exception:
        text = raw2.decode("utf-8", errors="replace")
    return str(rel_path or ""), text, bool(truncated), mime


def _read_raw(
    *, root_path: str, rel_path: str, max_bytes: int
) -> tuple[str, bytes, str]:
    p = _resolve_inside_root(root_path=root_path, rel_path=rel_path)
    if not p.exists() or not p.is_file():
        raise ValueError("file not found")
    max_bytes = int(max_bytes or 0)
    if max_bytes <= 0:
        max_bytes = 2 * 1024 * 1024
    max_bytes = min(max_bytes, 10 * 1024 * 1024)
    raw = p.read_bytes()
    if len(raw) > max_bytes:
        raise ValueError("file too large")
    mime, _ = mimetypes.guess_type(str(p))
    mime = mime or "application/octet-stream"
    return str(rel_path or ""), raw, mime


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    wait_s = max(0.0, float(seconds or 0.0))
    if wait_s <= 0 or stop_event.is_set():
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=wait_s)
    except TimeoutError:
        return


def _connect_log_key(result: _ConnectOnceResult) -> str:
    detail = (result.detail or "").strip()
    if len(detail) > 160:
        detail = detail[:160]
    return f"{result.reason}:{detail}:{result.connected}:{result.stable}"


async def run_companion(
    *, grpc_addr: str | None = None, stop_event: asyncio.Event | None = None
) -> None:
    """运行 Companion 主循环（gRPC 客户端）。

    - Companion 作为客户端发起到 OpenFocus 的长连接
    - 用 ping/pong 确认心跳
    - 在同一条双向流里承载命令与响应
    """

    addr = (grpc_addr or _openfocus_grpc_addr()).strip()
    if not addr:
        raise RuntimeError("OPENFOCUS_SERVER_GRPC_ADDR is empty")

    stop_event = stop_event or asyncio.Event()
    backoff = _CONNECT_BACKOFF_INITIAL_SECONDS
    log_limiter = _LogRateLimiter(interval_seconds=_CONNECT_LOG_LIMIT_SECONDS)

    while not stop_event.is_set():
        try:
            if log_limiter.should_log("connect-attempt"):
                LOG.info(
                    "连接 OpenFocus gRPC：addr=%s device_id=%s companion_id=%s",
                    addr,
                    RUNTIME.device_id,
                    int(getattr(RUNTIME, "server_companion_id", 0) or 0) or "—",
                )

            result = await _connect_once(addr, stop_event)
            if stop_event.is_set():
                return

            if result.stable:
                # 已经稳定在线一段时间，下一次断线后从较小延迟开始恢复。
                backoff = _CONNECT_BACKOFF_INITIAL_SECONDS

            retry_in = backoff
            if log_limiter.should_log(_connect_log_key(result)):
                LOG.warning(
                    "OpenFocus gRPC 连接结束，将重试：reason=%s connected=%s stable=%s detail=%s retry_in=%.1fs",
                    result.reason or "unknown",
                    bool(result.connected),
                    bool(result.stable),
                    result.detail or "",
                    retry_in,
                )
            await _sleep_or_stop(stop_event, retry_in)
            backoff = min(backoff * 2, _CONNECT_BACKOFF_MAX_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            retry_in = backoff
            try:
                if log_limiter.should_log(
                    f"exception:{type(e).__name__}:{str(e)[:160]}"
                ):
                    LOG.exception("连接异常，将重试：%s retry_in=%.1fs", e, retry_in)
            except Exception:
                pass
            # 断线/失败：指数退避重连
            await _sleep_or_stop(stop_event, retry_in)
            backoff = min(backoff * 2, _CONNECT_BACKOFF_MAX_SECONDS)


async def _connect_once(addr: str, stop_event: asyncio.Event) -> _ConnectOnceResult:
    out_q: asyncio.Queue[pb2.ClientToServer] = asyncio.Queue()
    agent_mgr = _AgentManager()
    term_mgr = _TerminalManager()
    float_mgr = _FloatBallManager()
    hook_server: asyncio.AbstractServer | None = None
    protocol_server: asyncio.AbstractServer | None = None
    hook_spool_task: asyncio.Task | None = None
    connected = False
    connected_at: float | None = None

    def _result(*, reason: str, detail: str = "") -> _ConnectOnceResult:
        stable = False
        if connected and connected_at is not None:
            stable = (
                asyncio.get_running_loop().time() - connected_at
                >= _CONNECT_STABLE_RESET_SECONDS
            )
        return _ConnectOnceResult(
            connected=connected, stable=stable, reason=reason, detail=detail
        )

    hello = pb2.Hello(
        device_id=RUNTIME.device_id,
        name=RUNTIME.name,
        capabilities=_capabilities(),
        auth_token=RUNTIME.auth_token or "",
        server_companion_id=int(getattr(RUNTIME, "server_companion_id", 0) or 0),
    )
    await out_q.put(pb2.ClientToServer(hello=hello))
    try:
        hook_server = await _start_hook_server(out_q)
    except Exception as e:
        try:
            LOG.warning("OpenFocus hook socket 启动失败：%s", e)
        except Exception:
            pass
    try:
        protocol_server = await _start_protocol_server(out_q)
    except Exception as e:
        try:
            LOG.warning("OpenFocus protocol socket 启动失败：%s", e)
        except Exception:
            pass
    hook_spool_task = asyncio.create_task(
        _hook_spool_poller(out_q, stop_event), name="openfocus-hook-spool"
    )
    try:
        LOG.debug(
            "发送 hello：name=%s capabilities=%s has_token=%s",
            RUNTIME.name,
            ",".join(_capabilities()),
            bool((RUNTIME.auth_token or "").strip()),
        )
    except Exception:
        pass

    async def _outgoing() -> AsyncIterator[pb2.ClientToServer]:
        # 允许 stop_event 在 out_q 空时也能打断等待，避免测试/退出时只能靠取消任务。
        while True:
            if stop_event.is_set():
                return

            get_task = asyncio.create_task(out_q.get())
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                {get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

            if stop_task in done:
                return
            yield get_task.result()

    async with grpc.aio.insecure_channel(addr) as channel:
        stub = pb2_grpc.CompanionControlStub(channel)
        call = stub.Connect(_outgoing())

        async def _cancel_on_stop() -> None:
            await stop_event.wait()
            try:
                call.cancel()
            except Exception:
                pass

        cancel_task = asyncio.create_task(
            _cancel_on_stop(), name="companion-cancel-on-stop"
        )
        try:
            async for msg in call:
                which = msg.WhichOneof("msg")
                if which == "welcome":
                    connected = True
                    connected_at = asyncio.get_running_loop().time()
                    cid = int(msg.welcome.companion_id or 0)
                    if cid > 0 and cid != RUNTIME.server_companion_id:
                        RUNTIME.server_companion_id = cid
                        RUNTIME._persist()
                    try:
                        LOG.info("已连接：welcome companion_id=%s", cid or "—")
                    except Exception:
                        pass
                    continue
                if which == "pairing_code":
                    req = msg.pairing_code
                    try:
                        LOG.info(
                            "收到 pairing_code 请求：force_new=%s", bool(req.force_new)
                        )
                        code, exp = RUNTIME.current_code(force_new=bool(req.force_new))
                        resp = pb2.PairingCodeResponse(
                            request_id=req.request_id,
                            ok=True,
                            code=code,
                            expires_at=exp.isoformat(),
                        )
                    except Exception as e:
                        try:
                            LOG.exception("pairing_code 处理失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.PairingCodeResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(pairing_code_resp=resp))
                    continue
                if which == "ping":
                    p = msg.ping
                    await out_q.put(
                        pb2.ClientToServer(
                            pong=pb2.Pong(
                                ts_unix_ms=_now_ms(), ping_ts_unix_ms=p.ts_unix_ms
                            )
                        )
                    )
                    continue

                if which == "pair":
                    req = msg.pair
                    try:
                        LOG.info("收到 pair 请求")
                        token = RUNTIME.confirm_pair(req.code)
                        resp = pb2.PairResponse(
                            request_id=req.request_id, ok=True, auth_token=token
                        )
                    except Exception as e:
                        try:
                            LOG.exception("pair 处理失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.PairResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(pair_resp=resp))
                    continue

                if which == "choose_directory":
                    req = msg.choose_directory
                    try:
                        LOG.info("收到 choose_directory 请求")
                        if not (RUNTIME.auth_token or "").strip():
                            raise RuntimeError("Companion 尚未配对")
                        path = _choose_directory()
                        resp = pb2.ChooseDirectoryResponse(
                            request_id=req.request_id, ok=True, path=path
                        )
                    except Exception as e:
                        try:
                            LOG.exception("choose_directory 处理失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.ChooseDirectoryResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(choose_directory_resp=resp))
                    continue

                if which == "files_list":
                    req = msg.files_list
                    try:
                        LOG.info(
                            "收到 files_list：rel_path=%s", str(req.rel_path or "")
                        )
                        path, entries = _list_dir(
                            root_path=req.root_path, rel_path=req.rel_path
                        )
                        resp = pb2.FilesListResponse(
                            request_id=req.request_id,
                            ok=True,
                            path=path,
                            entries=entries,
                        )
                    except Exception as e:
                        try:
                            LOG.exception("files_list 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.FilesListResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(files_list_resp=resp))
                    continue

                if which == "files_read":
                    req = msg.files_read
                    try:
                        LOG.info(
                            "收到 files_read：rel_path=%s", str(req.rel_path or "")
                        )
                        path, content, truncated, mime = _read_text(
                            root_path=req.root_path,
                            rel_path=req.rel_path,
                            max_bytes=req.max_bytes,
                        )
                        resp = pb2.FilesReadResponse(
                            request_id=req.request_id,
                            ok=True,
                            path=path,
                            content=content,
                            truncated=bool(truncated),
                            mime=mime,
                        )
                    except Exception as e:
                        try:
                            LOG.exception("files_read 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.FilesReadResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(files_read_resp=resp))
                    continue

                if which == "files_raw":
                    req = msg.files_raw
                    try:
                        LOG.info("收到 files_raw：rel_path=%s", str(req.rel_path or ""))
                        path, data, mime = _read_raw(
                            root_path=req.root_path,
                            rel_path=req.rel_path,
                            max_bytes=req.max_bytes,
                        )
                        resp = pb2.FilesRawResponse(
                            request_id=req.request_id,
                            ok=True,
                            path=path,
                            data=data,
                            mime=mime,
                        )
                    except Exception as e:
                        try:
                            LOG.exception("files_raw 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.FilesRawResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(files_raw_resp=resp))
                    continue

                if which == "agent_start":
                    req = msg.agent_start
                    try:
                        LOG.info(
                            "agent_start: session_id=%s agent_type=%s task=%s",
                            str(req.session_id or ""),
                            str(req.agent_type or ""),
                            str(req.task_public_id or ""),
                        )
                        sid = await agent_mgr.start(
                            session_id=req.session_id,
                            root_path=req.root_path,
                            agent_type=req.agent_type,
                            task_public_id=req.task_public_id,
                        )
                        resp = pb2.AgentStartResponse(
                            request_id=req.request_id, ok=True, session_id=sid
                        )
                    except Exception as e:
                        try:
                            LOG.exception("agent_start 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.AgentStartResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(agent_start_resp=resp))
                    continue

                if which == "agent_terminate":
                    req = msg.agent_terminate
                    try:
                        LOG.info(
                            "agent_terminate: session_id=%s", str(req.session_id or "")
                        )
                        await agent_mgr.terminate(session_id=req.session_id)
                        resp = pb2.AgentTerminateResponse(
                            request_id=req.request_id, ok=True
                        )
                    except Exception as e:
                        try:
                            LOG.exception("agent_terminate 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.AgentTerminateResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(agent_terminate_resp=resp))
                    continue

                if which == "agent_send":
                    req = msg.agent_send
                    try:
                        LOG.info(
                            "agent_send: request_id=%s session_id=%s",
                            str(req.request_id or ""),
                            str(req.session_id or ""),
                        )
                        if not (RUNTIME.auth_token or "").strip():
                            raise RuntimeError("Companion 尚未配对")
                        # 先 ACK，再异步跑 agent 并通过 AgentChunk 回传
                        await agent_mgr.send(
                            request_id=req.request_id,
                            session_id=req.session_id,
                            prompt=req.prompt,
                            out_q=out_q,
                        )
                        resp = pb2.AgentSendResponse(request_id=req.request_id, ok=True)
                    except Exception as e:
                        try:
                            LOG.exception("agent_send 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.AgentSendResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(agent_send_resp=resp))
                    continue

                if which == "terminal_start":
                    req = msg.terminal_start
                    try:
                        LOG.info(
                            "terminal_start: terminal_id=%s", str(req.terminal_id or "")
                        )
                        if not (RUNTIME.auth_token or "").strip():
                            raise RuntimeError("Companion 尚未配对")
                        sess = await term_mgr.start(
                            terminal_id=req.terminal_id,
                            root_path=req.root_path,
                            base_path=req.base_path,
                            task_public_id=req.task_public_id,
                            out_q=out_q,
                        )
                        resp = pb2.TerminalStartResponse(
                            request_id=req.request_id,
                            ok=True,
                            terminal_id=sess.terminal_id,
                            backend=sess.backend,
                            connect_url=sess.connect_url,
                        )
                    except Exception as e:
                        try:
                            LOG.exception("terminal_start 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.TerminalStartResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(terminal_start_resp=resp))
                    continue

                if which == "terminal_stop":
                    req = msg.terminal_stop
                    try:
                        LOG.info(
                            "terminal_stop: terminal_id=%s", str(req.terminal_id or "")
                        )
                        await term_mgr.stop(terminal_id=req.terminal_id, out_q=out_q)
                        resp = pb2.TerminalStopResponse(
                            request_id=req.request_id, ok=True
                        )
                    except Exception as e:
                        try:
                            LOG.exception("terminal_stop 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.TerminalStopResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(terminal_stop_resp=resp))
                    continue

                if which == "terminal_input":
                    req = msg.terminal_input
                    try:
                        await term_mgr.input(
                            terminal_id=req.terminal_id,
                            data=bytes(req.data),
                            out_q=out_q,
                        )
                        resp = pb2.TerminalInputResponse(
                            request_id=req.request_id, ok=True
                        )
                    except Exception as e:
                        try:
                            LOG.exception("terminal_input 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.TerminalInputResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(terminal_input_resp=resp))
                    continue

                if which == "terminal_resize":
                    req = msg.terminal_resize
                    try:
                        await term_mgr.resize(
                            terminal_id=req.terminal_id, cols=req.cols, rows=req.rows
                        )
                        resp = pb2.TerminalResizeResponse(
                            request_id=req.request_id, ok=True
                        )
                    except Exception as e:
                        try:
                            LOG.exception("terminal_resize 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.TerminalResizeResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(terminal_resize_resp=resp))
                    continue

                if which == "terminal_mouse_mode":
                    req = msg.terminal_mouse_mode
                    try:
                        enabled = await term_mgr.set_mouse_mode(
                            terminal_id=req.terminal_id, enabled=bool(req.enabled)
                        )
                        resp = pb2.TerminalMouseModeResponse(
                            request_id=req.request_id,
                            ok=True,
                            enabled=bool(enabled),
                        )
                    except Exception as e:
                        try:
                            LOG.exception("terminal_mouse_mode 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.TerminalMouseModeResponse(
                            request_id=req.request_id,
                            ok=False,
                            error=str(e),
                            enabled=bool(req.enabled),
                        )
                    await out_q.put(pb2.ClientToServer(terminal_mouse_mode_resp=resp))
                    continue

                if which == "float_ball_start":
                    req = msg.float_ball_start
                    try:
                        backend = await float_mgr.start(
                            browser_session_id=req.browser_session_id,
                            openfocus_base_url=req.openfocus_base_url,
                            summary_json=req.summary_json,
                        )
                        resp = pb2.FloatBallStartResponse(
                            request_id=req.request_id, ok=True, backend=backend
                        )
                    except Exception as e:
                        try:
                            LOG.exception("float_ball_start 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.FloatBallStartResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(float_ball_start_resp=resp))
                    continue

                if which == "float_ball_update":
                    req = msg.float_ball_update
                    try:
                        await float_mgr.update(
                            browser_session_id=req.browser_session_id,
                            summary_json=req.summary_json,
                        )
                        resp = pb2.FloatBallUpdateResponse(
                            request_id=req.request_id, ok=True
                        )
                    except Exception as e:
                        try:
                            LOG.exception("float_ball_update 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.FloatBallUpdateResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(float_ball_update_resp=resp))
                    continue

                if which == "float_ball_stop":
                    req = msg.float_ball_stop
                    try:
                        await float_mgr.stop(browser_session_id=req.browser_session_id)
                        resp = pb2.FloatBallStopResponse(
                            request_id=req.request_id, ok=True
                        )
                    except Exception as e:
                        try:
                            LOG.exception("float_ball_stop 失败：%s", e)
                        except Exception:
                            pass
                        resp = pb2.FloatBallStopResponse(
                            request_id=req.request_id, ok=False, error=str(e)
                        )
                    await out_q.put(pb2.ClientToServer(float_ball_stop_resp=resp))
                    continue
            return _result(reason="stream_completed")
        except grpc.aio.AioRpcError as e:
            # 连接断开/服务端关闭
            code = getattr(e, "code", lambda: None)()
            detail = getattr(e, "details", lambda: "")()
            return _result(reason=str(code or "grpc_error"), detail=str(detail or ""))
        except asyncio.CancelledError:
            # stop_event 触发的 call.cancel() 会导致本地 CANCELLED，这里视为正常退出。
            if stop_event.is_set():
                return _result(reason="stopped")
            raise
        except Exception as e:
            try:
                LOG.exception("gRPC 循环异常：%s", e)
            except Exception:
                pass
            raise
        finally:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await cancel_task
            if hook_server is not None:
                hook_server.close()
                with contextlib.suppress(Exception):
                    await hook_server.wait_closed()
            if hook_spool_task is not None:
                hook_spool_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await hook_spool_task
            if protocol_server is not None:
                protocol_server.close()
                with contextlib.suppress(Exception):
                    await protocol_server.wait_closed()
            with contextlib.suppress(Exception):
                await float_mgr.stop_all()
            with contextlib.suppress(Exception):
                sock_path = _hook_sock_path()
                if sock_path.exists() and stat.S_ISSOCK(sock_path.stat().st_mode):
                    sock_path.unlink()
            with contextlib.suppress(Exception):
                proto_sock_path = _protocol_sock_path()
                if proto_sock_path.exists() and stat.S_ISSOCK(
                    proto_sock_path.stat().st_mode
                ):
                    proto_sock_path.unlink()


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
