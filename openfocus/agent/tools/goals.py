from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import asc, desc

from ...db import session_scope
from ...models import Goal, Task
from ..core.tooling import SimpleToolRegistry
from ..core.types import Json, ToolSpec


def _parse_date_or_datetime(v: str) -> dt.datetime:
    # 支持 YYYY-MM-DD 或 RFC3339/ISO8601 datetime
    try:
        if len(v) == 10:
            d = dt.date.fromisoformat(v)
            return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
        x = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        if x.tzinfo is None:
            x = x.replace(tzinfo=dt.timezone.utc)
        return x
    except Exception as e:
        raise ValueError(f"invalid date/datetime: {v}") from e


def build_goal_tools() -> SimpleToolRegistry:
    reg = SimpleToolRegistry.empty()

    reg.register(
        ToolSpec(
            name="list_goals",
            description="列出所有 goals，可按状态/优先级/重要程度/时间范围过滤，并支持排序与 limit。",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "only_unfinished": {"type": "boolean", "description": "仅看未完成 goals（status!=done）"},
                    "status": {"type": "string", "description": "按 status 过滤，如 active/done/paused"},
                    "priority": {"type": "string", "description": "按 priority 过滤，如 urgent/normal"},
                    "importance": {"type": "string", "description": "按 importance 过滤，如 very_important/normal"},
                    "created_after": {"type": "string", "description": "创建时间下限，YYYY-MM-DD 或 ISO8601"},
                    "created_before": {"type": "string", "description": "创建时间上限，YYYY-MM-DD 或 ISO8601"},
                    "due_before": {"type": "string", "description": "完成时间上限，YYYY-MM-DD"},
                    "due_after": {"type": "string", "description": "完成时间下限，YYYY-MM-DD"},
                    "order_by": {"type": "string", "description": "排序字段：created_at/due_date", "enum": ["created_at", "due_date"]},
                    "order": {"type": "string", "description": "排序方向：asc/desc", "enum": ["asc", "desc"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "additionalProperties": False,
            },
        ),
        handler=_tool_list_goals,
    )

    reg.register(
        ToolSpec(
            name="describe_goal",
            description="查看 goal 的详细信息：描述、时间信息、以及其下 tasks 列表。",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "goal_id": {"type": "integer"},
                    "include_tasks": {"type": "boolean", "default": True},
                },
                "required": ["goal_id"],
                "additionalProperties": False,
            },
        ),
        handler=_tool_describe_goal,
    )

    # 兼容拼写（用户输入里写的是 describe_gloal）
    reg.register(
        ToolSpec(
            name="describe_gloal",
            description="describe_goal 的别名（兼容拼写）。",
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "goal_id": {"type": "integer"},
                    "include_tasks": {"type": "boolean", "default": True},
                },
                "required": ["goal_id"],
                "additionalProperties": False,
            },
        ),
        handler=_tool_describe_goal,
    )

    return reg


def _tool_list_goals(args: Json) -> str:
    only_unfinished = bool(args.get("only_unfinished")) if "only_unfinished" in args else False
    status = (args.get("status") or None)
    priority = (args.get("priority") or None)
    importance = (args.get("importance") or None)
    created_after = args.get("created_after")
    created_before = args.get("created_before")
    due_before = args.get("due_before")
    due_after = args.get("due_after")
    order_by = args.get("order_by") or "created_at"
    order = args.get("order") or "desc"
    limit = int(args.get("limit") or 50)

    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    with session_scope() as s:
        q = s.query(Goal)
        if only_unfinished:
            q = q.filter(Goal.status != "done")
        if status:
            q = q.filter(Goal.status == str(status))
        if priority:
            q = q.filter(Goal.priority == str(priority))
        if importance:
            q = q.filter(Goal.importance == str(importance))

        if created_after:
            q = q.filter(Goal.created_at >= _parse_date_or_datetime(str(created_after)))
        if created_before:
            q = q.filter(Goal.created_at <= _parse_date_or_datetime(str(created_before)))

        if due_after:
            q = q.filter(Goal.due_date >= dt.date.fromisoformat(str(due_after)))
        if due_before:
            q = q.filter(Goal.due_date <= dt.date.fromisoformat(str(due_before)))

        col = Goal.created_at if order_by == "created_at" else Goal.due_date
        q = q.order_by(asc(col) if order == "asc" else desc(col))
        goals = q.limit(limit).all()

        # 附带 tasks 统计（减少 describe_goal 调用次数）
        goal_ids = [g.id for g in goals]
        tasks = []
        if goal_ids:
            tasks = s.query(Task.goal_id, Task.status).filter(Task.goal_id.in_(goal_ids)).all()
        counts: dict[int, dict[str, int]] = {}
        for gid, st in tasks:
            counts.setdefault(gid, {})
            counts[gid][st] = counts[gid].get(st, 0) + 1

        out: list[dict[str, Any]] = []
        for g in goals:
            out.append(
                {
                    "goal_id": g.id,
                    "content": g.content,
                    "description": g.description,
                    "status": g.status,
                    "priority": g.priority,
                    "importance": g.importance,
                    "due_date": g.due_date.isoformat(),
                    "created_at": g.created_at.isoformat(),
                    "task_counts": counts.get(g.id, {}),
                }
            )

    return json.dumps({"goals": out, "limit": limit}, ensure_ascii=False)


def _tool_describe_goal(args: Json) -> str:
    goal_id = int(args["goal_id"])
    include_tasks = bool(args.get("include_tasks", True))

    with session_scope() as s:
        g = s.get(Goal, goal_id)
        if g is None:
            return json.dumps({"error": "goal not found", "goal_id": goal_id}, ensure_ascii=False)

        tasks_out: list[dict[str, Any]] = []
        if include_tasks:
            tasks = s.query(Task).filter(Task.goal_id == goal_id).order_by(Task.id.asc()).all()
            for t in tasks:
                tasks_out.append(
                    {
                        "id": t.id,
                        "public_id": t.public_id,
                        "title": t.title,
                        "status": t.status,
                        "created_at": t.created_at.isoformat(),
                        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                    }
                )

        out = {
            "goal": {
                "goal_id": g.id,
                "content": g.content,
                "description": g.description,
                "status": g.status,
                "priority": g.priority,
                "importance": g.importance,
                "due_date": g.due_date.isoformat(),
                "created_at": g.created_at.isoformat(),
            },
            "tasks": tasks_out,
        }
    return json.dumps(out, ensure_ascii=False)
