# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = [
    pytest.mark.blackbox,
    pytest.mark.skipif(
        os.environ.get("OPENFOCUS_RUN_BLACKBOX") != "1",
        reason="black-box tests are opt-in; run `make test-blackbox`",
    ),
]

PAIRING_CODE = "A1B2C3D4E5"


class _FakeCompanionRuntime:
    def __init__(self) -> None:
        self._terminals: dict[str, dict[str, object]] = {}
        self._ready_emitted: set[str] = set()

    async def request_pairing_code(
        self, *, force_new: bool, timeout_seconds: float = 10.0
    ) -> tuple[str, str]:
        return PAIRING_CODE, "2099-01-01T00:00:00Z"

    async def request_pair(self, code: str, *, timeout_seconds: float = 10.0) -> str:
        if str(code or "").strip() != PAIRING_CODE:
            raise RuntimeError("invalid pairing code")
        return "blackbox-auth-token"

    async def request_files_list(
        self, *, root_path: str, rel_path: str, timeout_seconds: float = 10.0
    ) -> SimpleNamespace:
        root = Path(root_path).resolve(strict=True)
        path = (root / str(rel_path or "")).resolve(strict=True)
        path.relative_to(root)
        entries = []
        for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
            stat = child.stat()
            entries.append(
                SimpleNamespace(
                    name=child.name,
                    rel_path=str(child.relative_to(root)),
                    kind="dir" if child.is_dir() else "file",
                    size=int(stat.st_size),
                    mtime=float(stat.st_mtime),
                )
            )
        return SimpleNamespace(path=str(rel_path or ""), entries=entries)

    async def request_files_read(
        self,
        *,
        root_path: str,
        rel_path: str,
        max_bytes: int,
        timeout_seconds: float = 10.0,
    ) -> SimpleNamespace:
        root = Path(root_path).resolve(strict=True)
        path = (root / str(rel_path or "")).resolve(strict=True)
        path.relative_to(root)
        content = path.read_text(encoding="utf-8")
        return SimpleNamespace(
            path=str(rel_path or ""),
            content=content[:max_bytes],
            truncated=len(content.encode("utf-8")) > int(max_bytes),
            mime="text/markdown",
        )

    async def request_terminal_start(
        self,
        *,
        terminal_id: str,
        root_path: str,
        base_path: str,
        task_public_id: str,
        timeout_seconds: float = 10.0,
    ) -> SimpleNamespace:
        self._terminals[terminal_id] = {
            "root_path": root_path,
            "base_path": base_path,
            "task_public_id": task_public_id,
        }
        return SimpleNamespace(
            terminal_id=terminal_id, backend="test_echo", connect_url=""
        )

    async def request_terminal_list_sessions(
        self, *, timeout_seconds: float = 3.0
    ) -> SimpleNamespace:
        for terminal_id in list(self._terminals):
            if terminal_id not in self._ready_emitted:
                await self._publish_terminal_output(terminal_id, b"terminal-ready\n")
                self._ready_emitted.add(terminal_id)
        return SimpleNamespace(
            sessions=[
                SimpleNamespace(
                    terminal_id=terminal_id,
                    root_path=str(info.get("root_path") or ""),
                    created_at=0.0,
                )
                for terminal_id, info in self._terminals.items()
            ]
        )

    async def request_terminal_input(
        self, *, terminal_id: str, data: bytes, timeout_seconds: float = 10.0
    ) -> SimpleNamespace:
        if terminal_id not in self._terminals:
            raise RuntimeError("terminal not found")
        await self._publish_terminal_output(terminal_id, bytes(data or b""))
        return SimpleNamespace(ok=True)

    async def request_terminal_stop(
        self, *, terminal_id: str, timeout_seconds: float = 10.0
    ) -> SimpleNamespace:
        self._terminals.pop(terminal_id, None)
        await self._publish_terminal_output(terminal_id, b"", closed=True)
        return SimpleNamespace(ok=True)

    def close(self) -> None:
        pass

    async def _publish_terminal_output(
        self, terminal_id: str, data: bytes, *, closed: bool = False
    ) -> None:
        from openfocus.infrastructure.streaming import handle_terminal_output

        await handle_terminal_output(
            SimpleNamespace(
                terminal_id=terminal_id,
                data=data,
                closed=closed,
                error="",
            )
        )


def _goal_id_from_location(location: str | None) -> int:
    match = re.search(r"[?&]goal=(\d+)", str(location or ""))
    assert match is not None, f"missing goal id in redirect: {location!r}"
    return int(match.group(1))


def _task_public_id_for_title(html: str, title: str) -> str:
    for match in re.finditer(r'<tr class="js-open-task"(?P<attrs>[^>]*)>', html):
        attrs = match.group("attrs")
        if f'data-sort-title="{title}"' not in attrs:
            continue
        public_match = re.search(r'data-task="([^"]+)"', attrs)
        assert public_match is not None
        return public_match.group(1)
    raise AssertionError(f"task row for {title!r} not found")


async def _wait_until_pairing_code_available(
    client: AsyncClient, companion_id: int, *, timeout_s: float = 4.0
) -> None:
    deadline = asyncio.get_running_loop().time() + float(timeout_s)
    last = ""
    while asyncio.get_running_loop().time() < deadline:
        response = await client.post(f"/api/companions/{companion_id}/pairing_code")
        if response.status_code == 200:
            return
        last = response.text
        await asyncio.sleep(0.02)
    raise AssertionError(f"companion pairing code not available, last={last!r}")


async def _terminal_history_bytes(
    client: AsyncClient, *, space_id: int, terminal_id: str
) -> bytes:
    response = await client.get(
        f"/api/agent_spaces/{space_id}/terminals/{terminal_id}/history",
        params={"max_bytes": 1024 * 1024},
    )
    assert response.status_code == 200
    return base64.b64decode(response.json().get("data_b64") or "")


async def _wait_for_terminal_history(
    client: AsyncClient,
    *,
    space_id: int,
    terminal_id: str,
    needle: bytes,
    timeout_s: float = 4.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + float(timeout_s)
    last = b""
    while asyncio.get_running_loop().time() < deadline:
        last = await _terminal_history_bytes(
            client, space_id=space_id, terminal_id=terminal_id
        )
        if needle in last:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"terminal history missing {needle!r}; last={last[-500:]!r}")


async def _inject_and_wait(
    client: AsyncClient, *, space_id: int, terminal_id: str, text: str
) -> None:
    response = await client.post(
        f"/api/agent_spaces/{space_id}/terminals/{terminal_id}/inject",
        json={"text": text},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    await _wait_for_terminal_history(
        client,
        space_id=space_id,
        terminal_id=terminal_id,
        needle=text.encode("utf-8"),
    )


def test_agent_space_backend_journey_covers_terminal_prompt_preview_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    async def _run() -> None:
        monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))
        monkeypatch.setenv(
            "OPENFOCUS_COMPANION_STATE", str(tmp_path / "companion_state.json")
        )

        from openfocus.app import COMPANION_GRPC, app

        workspace = tmp_path / "agent-space-workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text(
            "AgentSpace backend journey\n"
            "Preview line selected for terminal injection\n",
            encoding="utf-8",
        )

        fake_runtime = _FakeCompanionRuntime()
        companion_id = 0
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                register = await client.post(
                    "/api/companions/register",
                    json={
                        "device_id": "blackbox-agent-space-runtime",
                        "name": "Blackbox AgentSpace Runtime",
                        "base_url": "grpc://blackbox",
                    },
                )
                assert register.status_code == 200
                companion_id = int(register.json()["id"])
                await COMPANION_GRPC.registry.set_connected(companion_id, fake_runtime)

                await _wait_until_pairing_code_available(client, companion_id)
                pair = await client.post(
                    f"/api/companions/{companion_id}/pair",
                    json={"code": PAIRING_CODE},
                )
                assert pair.status_code == 200
                assert pair.json()["ok"] is True

                task_content = "Use the task content as the built-in send basic prompt."
                due_date = (dt.date.today() + dt.timedelta(days=7)).isoformat()

                create_goal = await client.post(
                    "/goals",
                    data={
                        "title": "AgentSpace backend blackbox goal",
                        "content": "Exercise AgentSpace through public APIs.",
                        "due_date": due_date,
                    },
                    follow_redirects=False,
                )
                assert create_goal.status_code == 303
                goal_id = _goal_id_from_location(create_goal.headers.get("location"))

                task_title = "AgentSpace backend blackbox task"
                create_task = await client.post(
                    f"/goals/{goal_id}/tasks",
                    data={"title": task_title, "content": task_content},
                    follow_redirects=False,
                )
                assert create_task.status_code == 303

                goal_page = await client.get(f"/goals?goal={goal_id}")
                assert goal_page.status_code == 200
                task_public_id = _task_public_id_for_title(goal_page.text, task_title)

                create_space = await client.post(
                    f"/api/tasks/{task_public_id}/agent_space",
                    json={
                        "companion_id": companion_id,
                        "root_path": str(workspace),
                    },
                )
                assert create_space.status_code == 200
                space_id = int(create_space.json()["space_id"])

                lookup = await client.get(f"/api/tasks/{task_public_id}/agent_space")
                assert lookup.status_code == 200
                assert lookup.json()["space"]["id"] == space_id
                assert lookup.json()["space"]["root_path"] == str(workspace)

                files = await client.get(
                    f"/api/agent_spaces/{space_id}/files/list",
                    params={"path": ""},
                )
                assert files.status_code == 200
                assert any(
                    entry["rel_path"] == "README.md"
                    for entry in files.json()["entries"]
                )

                preview = await client.get(
                    f"/api/agent_spaces/{space_id}/files/read",
                    params={"path": "README.md"},
                )
                assert preview.status_code == 200
                assert "Preview line selected" in preview.json()["content"]

                create_terminal = await client.post(
                    f"/api/agent_spaces/{space_id}/terminals/new"
                )
                assert create_terminal.status_code == 200
                terminal = create_terminal.json()["terminal"]
                terminal_id = str(terminal["terminal_id"])
                assert terminal_id
                assert terminal["backend"] == "test_echo"

                terminals = await client.get(f"/api/agent_spaces/{space_id}/terminals")
                assert terminals.status_code == 200
                assert terminals.json()["companion"]["online"] is True
                assert terminal_id in {
                    item["terminal_id"] for item in terminals.json()["terminals"]
                }

                await _wait_for_terminal_history(
                    client,
                    space_id=space_id,
                    terminal_id=terminal_id,
                    needle=b"terminal-ready\n",
                )
                await _inject_and_wait(
                    client,
                    space_id=space_id,
                    terminal_id=terminal_id,
                    text="ls\n",
                )
                await _inject_and_wait(
                    client,
                    space_id=space_id,
                    terminal_id=terminal_id,
                    text=f"{task_content}\n",
                )

                auto_prompt = await client.post(
                    f"/api/agent_spaces/{space_id}/terminals/{terminal_id}/auto_prompts",
                    json={
                        "enabled": True,
                        "prompt": "Report progress only for meaningful progress.",
                    },
                )
                assert auto_prompt.status_code == 200
                assert auto_prompt.json() == {"ok": True, "enabled": True}

                await _inject_and_wait(
                    client,
                    space_id=space_id,
                    terminal_id=terminal_id,
                    text="@README.md#L2\n",
                )

                close = await client.post(
                    f"/api/agent_spaces/{space_id}/terminals/{terminal_id}/close"
                )
                assert close.status_code == 200
                assert close.json()["ok"] is True

                after_close = await client.get(
                    f"/api/agent_spaces/{space_id}/terminals"
                )
                assert after_close.status_code == 200
                assert terminal_id not in {
                    item["terminal_id"] for item in after_close.json()["terminals"]
                }

                closed_history = await client.get(
                    f"/api/agent_spaces/{space_id}/terminals/{terminal_id}/history"
                )
                assert closed_history.status_code == 404

                release = await client.delete(
                    f"/api/tasks/{task_public_id}/agent_space"
                )
                assert release.status_code == 200
                assert release.json()["released"] is True
                assert release.json()["space_id"] == space_id

                after_release = await client.get(
                    f"/api/tasks/{task_public_id}/agent_space"
                )
                assert after_release.status_code == 200
                assert after_release.json()["space"] is None
        finally:
            if companion_id:
                with contextlib.suppress(Exception):
                    await COMPANION_GRPC.registry.set_disconnected(
                        companion_id, fake_runtime
                    )

    asyncio.run(_run())
