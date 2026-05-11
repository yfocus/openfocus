from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import mimetypes
import os
import secrets
import shutil
import socket
import string
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import AsyncIterator

import grpc

from . import companion_rpc_pb2 as pb2
from . import companion_rpc_pb2_grpc as pb2_grpc

LOG = logging.getLogger("openfocus.companion")


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
    # MVP：先声明已实现的能力
    return ["pairing", "choose_directory", "agent", "terminal"]


def _coco_bin() -> str:
    return str(os.environ.get("OPENFOCUS_COCO_BIN") or "coco").strip() or "coco"


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
    *, tmux_bin: str, tmux_name: str, root_path: str, shell: str, mouse: bool = True
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
        create = await asyncio.create_subprocess_exec(
            tmux_bin,
            "new-session",
            "-d",
            "-s",
            tmux_name,
            "-c",
            root_path,
            shell,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await create.wait()
        if create.returncode != 0:
            raise RuntimeError("tmux new-session failed")

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
    backoff = 0.2

    while not stop_event.is_set():
        try:
            LOG.info(
                "连接 OpenFocus gRPC：addr=%s device_id=%s companion_id=%s",
                addr,
                RUNTIME.device_id,
                int(getattr(RUNTIME, "server_companion_id", 0) or 0) or "—",
            )
            await _connect_once(addr, stop_event)
            backoff = 0.2
        except asyncio.CancelledError:
            raise
        except Exception as e:
            try:
                LOG.exception("连接异常，将重试：%s", e)
            except Exception:
                pass
            # 断线/失败：指数退避重连
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)


async def _connect_once(addr: str, stop_event: asyncio.Event) -> None:
    out_q: asyncio.Queue[pb2.ClientToServer] = asyncio.Queue()
    agent_mgr = _AgentManager()
    term_mgr = _TerminalManager()

    hello = pb2.Hello(
        device_id=RUNTIME.device_id,
        name=RUNTIME.name,
        capabilities=_capabilities(),
        auth_token=RUNTIME.auth_token or "",
        server_companion_id=int(getattr(RUNTIME, "server_companion_id", 0) or 0),
    )
    await out_q.put(pb2.ClientToServer(hello=hello))
    try:
        LOG.info(
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
        except grpc.aio.AioRpcError as e:
            # 连接断开/服务端关闭
            try:
                LOG.warning(
                    "gRPC 连接断开：code=%s detail=%s",
                    getattr(e, "code", lambda: None)(),
                    getattr(e, "details", lambda: "")(),
                )
            except Exception:
                pass
            return
        except asyncio.CancelledError:
            # stop_event 触发的 call.cancel() 会导致本地 CANCELLED，这里视为正常退出。
            if stop_event.is_set():
                return
            raise
        except Exception as e:
            try:
                LOG.exception("gRPC 循环异常：%s", e)
            except Exception:
                pass
            raise
        finally:
            cancel_task.cancel()


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
