# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from collections.abc import Callable

from ...models import InspirationSpace, RemoteTerminalSession
from ..agent_spaces import terminals as terminal_service


def terminal_payload(
    space_id: int,
    terminal: RemoteTerminalSession,
    *,
    embed_path: Callable[[int, str], str],
) -> dict:
    out = terminal_service.terminal_payload(terminal)
    backend = str(getattr(terminal, "backend", "") or "ttyd").strip() or "ttyd"
    connect_url = str(getattr(terminal, "connect_url", "") or "").strip()
    terminal_id = str(terminal.terminal_id or "")
    if backend == "ttyd" and connect_url:
        out["embed_url"] = embed_path(int(space_id), terminal_id)
    return out


def draft_summary_prompt(space: InspirationSpace, *, base_url: str = "") -> str:
    title = str(space.title or "Inspiration").strip()
    parts = [
        "You are collaborating with OpenFocus as a terminal agent.",
        "Read the current workspace and resources/ directory, ask the user in this terminal if key context is missing, then create or update resources/draft_summary.md.",
        "The file is the bridge from your custom agent to OpenFocus goal generation: it must be Markdown with one level-1 heading as the goal title, the text under that heading as the goal content, and then one level-2 heading per task with that task's content below it.",
        f"Inspiration title: {title}.",
    ]
    clean_base_url = str(base_url or "").strip()
    if clean_base_url:
        parts.append(f"OpenFocus: {clean_base_url}.")
    parts.append(
        "After saving resources/draft_summary.md, stop and tell the user it is ready to sync in OpenFocus."
    )
    return " ".join(parts)
