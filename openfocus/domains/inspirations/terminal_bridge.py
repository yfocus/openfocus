# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from collections.abc import Callable

from ...models import InspirationSpace, RemoteTerminalSession
from ..terminals import gateway as terminal_gateway


def terminal_payload(
    space_id: int,
    terminal: RemoteTerminalSession,
    *,
    embed_path: Callable[[int, str], str],
) -> dict:
    out = terminal_gateway.terminal_payload(
        int(space_id), terminal, route_prefix="/api/inspirations"
    )
    terminal_id = str(terminal.terminal_id or "")
    if "embed_url" in out:
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
