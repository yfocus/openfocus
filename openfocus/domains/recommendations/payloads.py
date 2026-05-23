# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from ...models import Event
from ..memory import service as memory_service


def iso(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[no-any-return]
    return str(value)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def memory_daily_files() -> list[dict[str, Any]]:
    root = memory_service.daily_root().resolve()
    files = sorted(root.glob("*.md"), reverse=True)
    out: list[dict[str, Any]] = []
    for p in files:
        try:
            stat = p.stat()
            rel = (
                p.resolve()
                .relative_to(memory_service.memory_dir().resolve())
                .as_posix()
            )
            out.append(
                {
                    "rel_path": rel,
                    "name": p.name,
                    "bytes": stat.st_size,
                    "modified_at": dt.datetime.fromtimestamp(
                        stat.st_mtime, tz=dt.timezone.utc
                    ).isoformat(),
                }
            )
        except Exception:
            continue
    return out


def serialize_event(ev: Event) -> dict[str, Any]:
    return {
        "id": int(ev.id),
        "kind": ev.kind,
        "agent": ev.agent,
        "task_id": ev.task_id,
        "payload": ev.payload or {},
        "created_at": iso(ev.created_at),
    }
