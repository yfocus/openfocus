# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json

from ...agent.core.tooling import SimpleToolRegistry
from ...agent.core.types import Json, ToolSpec
from ...agent.tools.goals import build_goal_tools
from ...db import session_scope
from ...models import Event
from ..memory import service as memory_service
from .payloads import memory_daily_files, read_text, serialize_event


def build_recommendation_tool_registry() -> SimpleToolRegistry:
    """Build the tool registry exposed to Next Move recommendation agents."""

    reg = build_goal_tools()
    reg.register(
        ToolSpec(
            name="list_daily_memory_files",
            description="列出可读取的 daily memory 文件。先用该工具找到 rel_path，再调用 read_daily_memory_file。",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 365}
                },
                "additionalProperties": False,
            },
        ),
        handler=_tool_list_daily_memory_files,
    )
    reg.register(
        ToolSpec(
            name="read_daily_memory_file",
            description="读取某个 daily memory 文件的完整内容。rel_path 必须来自 list_daily_memory_files。",
            parameters_json_schema={
                "type": "object",
                "properties": {"rel_path": {"type": "string"}},
                "required": ["rel_path"],
                "additionalProperties": False,
            },
        ),
        handler=_tool_read_daily_memory_file,
    )
    reg.register(
        ToolSpec(
            name="list_recent_events",
            description="查看更多事件。支持 offset/limit 翻页；返回最新事件优先。",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "additionalProperties": False,
            },
        ),
        handler=_tool_list_recent_events,
    )
    return reg


def _tool_list_daily_memory_files(args: Json) -> str:
    limit = max(1, min(int(args.get("limit") or 30), 365))
    return json.dumps(
        {"files": memory_daily_files()[:limit], "limit": limit},
        ensure_ascii=False,
    )


def _tool_read_daily_memory_file(args: Json) -> str:
    rel = str(args.get("rel_path") or "").strip()
    if not rel:
        return json.dumps({"error": "rel_path is required"}, ensure_ascii=False)
    try:
        p = memory_service.path_from_rel(rel)
        daily_root = memory_service.daily_root().resolve()
        if p.resolve() != daily_root and daily_root not in p.resolve().parents:
            raise ValueError("not a daily memory file")
        return json.dumps(
            {"rel_path": rel, "content": read_text(p)}, ensure_ascii=False
        )
    except Exception as e:
        return json.dumps({"error": str(e), "rel_path": rel}, ensure_ascii=False)


def _tool_list_recent_events(args: Json) -> str:
    limit = max(1, min(int(args.get("limit") or 100), 200))
    offset = max(0, int(args.get("offset") or 0))
    with session_scope() as s:
        rows = (
            s.query(Event).order_by(Event.id.desc()).offset(offset).limit(limit).all()
        )
    return json.dumps(
        {
            "events": [serialize_event(ev) for ev in rows],
            "limit": limit,
            "offset": offset,
            "next_offset": offset + len(rows),
        },
        ensure_ascii=False,
    )
