# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...db import session_scope
from ...domains.memory import service as memory_service
from ...models import AgentSpace, Event, Goal, NextMoveFeedback, Task
from ..core.loop import AgentLoopConfig, parse_json_strict, run_tool_loop
from ..core.tooling import SimpleToolRegistry
from ..core.types import EventSink, Json, ToolSpec
from ..llm.types import LLMProvider
from ..tools.goals import build_goal_tools


@dataclass
class AttentionSchedulerAgent:
    """基于 agent loop 推荐用户下一步要做的少量 Task。"""

    provider: LLMProvider
    goal_id: int | None = None
    name: str = "attention_scheduler"

    def instructions(self) -> str:
        return (
            "你是 OpenFocus 的 Next Move 注意力调度 agent。\n"
            "你的任务不是列长清单，而是为用户节省注意力与前额叶执行资源："
            "在所有 open task 中只推荐 2 个现在最值得做的 task，并给出明确、简短、可执行的理由。\n\n"
            "认知科学依据（请用于判断和解释）：\n"
            "- 人类工作记忆容量有限；同时给太多选择会增加认知负荷和决策疲劳。\n"
            "- 任务切换会产生注意残留（attention residue）和重新加载上下文成本；"
            "优先选择能延续近期上下文、降低切换成本的任务。\n"
            "- 明确的下一步能降低启动摩擦；推荐理由应告诉用户为什么现在做它，而不是暴露内部打分。\n"
            "- 截止期、重要性、连续性、用户长期偏好、近期反馈和当前可执行性要一起考虑。\n\n"
            "你会收到：long memory 的全部内容、当前 open goals/tasks、最近一周完成的 goals/tasks、"
            "最近 100 条事件、历史 Not for now/feedback，以及可查看更多 daily memory 和事件的工具。\n"
            "如果需要更多 daily memory，请使用 list_daily_memory_files/read_daily_memory_file；"
            "如果最近 100 条事件不够，请使用 list_recent_events。\n"
            "如果需要核对 goal/task 详情，请使用 list_goals/describe_goal。\n\n"
            "硬性约束：\n"
            "1. 最多推荐 2 个 task；如果候选不足可以少于 2 个，但不能超过 2 个。\n"
            "2. 推荐必须来自输入或工具返回的 open task；不要编造 task_public_id。\n"
            "3. 不要推荐 done/canceled/deleted task，或所属 goal 已完成/归档/暂停的 task。\n"
            "4. 不要推荐 context.recent_not_for_now_task_public_ids 中的 task；用户刚说 Not for now 时必须换一个。\n"
            "5. 如果没有可执行 task，返回 recommendation=null，并说明 no_recommendation_reason。\n"
            "6. 输出必须是严格 JSON，不要 Markdown。\n\n"
            "输出格式：\n"
            "{\n"
            '  "recommendations": [\n'
            "    {\n"
            '      "task_public_id": "...",\n'
            '      "goal_id": 123,\n'
            '      "reason": "一句话说明为什么现在推荐它",\n'
            '      "why": ["理由1", "理由2"],\n'
            '      "confidence": "high|medium|low",\n'
            '      "context_switch_cost": "low|medium|high"\n'
            "    }\n"
            "  ],\n"
            '  "no_recommendation_reason": null\n'
            "}"
        )

    def _fallback(self, *, error: str) -> Json:
        # API 层依然保持稳定，但不再用规则推荐伪造结果。
        return {
            "recommendation": None,
            "recommendations": [],
            "items": [],
            "no_recommendation_reason": f"LLM agent loop failed: {error}",
            "context_summary": {},
        }

    def _task_type_label(self, task_type: str | None) -> str:
        labels = {
            "deep_work": "Deep Work",
            "communication": "Communication",
            "review": "Review",
            "execution": "Execution",
            "admin": "Admin",
        }
        return labels.get(str(task_type or "").strip().lower(), "Execution")

    def _infer_task_type(self, title: str, content: str) -> str:
        text = f"{title}\n{content}".lower()
        if any(k in text for k in ["review", "pr", "diff", "code review", "审查"]):
            return "review"
        if any(k in text for k in ["email", "sync", "meeting", "沟通", "回复"]):
            return "communication"
        if any(k in text for k in ["整理", "报销", "admin", "配置", "清理"]):
            return "admin"
        if any(k in text for k in ["design", "implement", "refactor", "实现", "重构"]):
            return "deep_work"
        return "execution"

    def _infer_estimated_minutes(self, task_type: str, title: str, content: str) -> int:
        text = f"{title}\n{content}".lower()
        if any(k in text for k in ["quick", "small", "minor", "简单", "快速"]):
            return 25
        if task_type == "deep_work":
            return 90
        if task_type in {"communication", "admin"}:
            return 30
        if task_type == "review":
            return 45
        return 60

    def _iso(self, value: object) -> str | None:
        if value is None:
            return None
        if hasattr(value, "isoformat"):
            return value.isoformat()  # type: ignore[no-any-return]
        return str(value)

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _memory_daily_files(self) -> list[dict[str, Any]]:
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

    def _tool_list_daily_memory_files(self, args: Json) -> str:
        limit = max(1, min(int(args.get("limit") or 30), 365))
        return json.dumps(
            {"files": self._memory_daily_files()[:limit], "limit": limit},
            ensure_ascii=False,
        )

    def _tool_read_daily_memory_file(self, args: Json) -> str:
        rel = str(args.get("rel_path") or "").strip()
        if not rel:
            return json.dumps({"error": "rel_path is required"}, ensure_ascii=False)
        try:
            p = memory_service.path_from_rel(rel)
            daily_root = memory_service.daily_root().resolve()
            if p.resolve() != daily_root and daily_root not in p.resolve().parents:
                raise ValueError("not a daily memory file")
            return json.dumps(
                {"rel_path": rel, "content": self._read_text(p)}, ensure_ascii=False
            )
        except Exception as e:
            return json.dumps({"error": str(e), "rel_path": rel}, ensure_ascii=False)

    def _serialize_event(self, ev: Event) -> dict[str, Any]:
        return {
            "id": int(ev.id),
            "kind": ev.kind,
            "agent": ev.agent,
            "task_id": ev.task_id,
            "payload": ev.payload or {},
            "created_at": self._iso(ev.created_at),
        }

    def _tool_list_recent_events(self, args: Json) -> str:
        limit = max(1, min(int(args.get("limit") or 100), 200))
        offset = max(0, int(args.get("offset") or 0))
        with session_scope() as s:
            q = s.query(Event).order_by(Event.id.desc()).offset(offset).limit(limit)
            rows = q.all()
        return json.dumps(
            {
                "events": [self._serialize_event(ev) for ev in rows],
                "limit": limit,
                "offset": offset,
                "next_offset": offset + len(rows),
            },
            ensure_ascii=False,
        )

    def _build_tool_registry(self) -> SimpleToolRegistry:
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
            handler=self._tool_list_daily_memory_files,
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
            handler=self._tool_read_daily_memory_file,
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
            handler=self._tool_list_recent_events,
        )
        return reg

    def _build_context(self) -> dict[str, Any]:
        now = dt.datetime.now(dt.timezone.utc)
        week_ago = now - dt.timedelta(days=7)
        with session_scope() as s:
            open_goals = (
                s.query(Goal)
                .filter(Goal.status.notin_(["done", "archived", "paused", "canceled"]))
                .order_by(Goal.due_date.asc(), Goal.id.asc())
                .all()
            )
            if self.goal_id is not None:
                open_goals = [g for g in open_goals if int(g.id) == int(self.goal_id)]
            open_goal_ids = [int(g.id) for g in open_goals]
            open_tasks = []
            if open_goal_ids:
                open_tasks = (
                    s.query(Task)
                    .filter(Task.goal_id.in_(open_goal_ids))
                    .filter(Task.status.in_(["todo", "in_progress", "blocked"]))
                    .order_by(Task.id.asc())
                    .all()
                )
            task_ids = [t.public_id for t in open_tasks]
            spaces_by_task = {}
            if task_ids:
                for space in (
                    s.query(AgentSpace)
                    .filter(AgentSpace.task_public_id.in_(task_ids))
                    .all()
                ):
                    spaces_by_task[space.task_public_id] = space

            completed_goal_events = (
                s.query(Event)
                .filter(Event.kind == "goal.confirmed_done_by_user")
                .filter(Event.created_at >= week_ago)
                .order_by(Event.id.desc())
                .all()
            )
            completed_goal_ids: list[int] = []
            for ev in completed_goal_events:
                payload = ev.payload or {}
                try:
                    gid = int(payload.get("goal_id") or 0)
                except Exception:
                    gid = 0
                if gid and gid not in completed_goal_ids:
                    completed_goal_ids.append(gid)
            completed_goals = (
                s.query(Goal)
                .filter(Goal.id.in_(completed_goal_ids))
                .order_by(Goal.id.desc())
                .all()
                if completed_goal_ids
                else []
            )
            completed_tasks = (
                s.query(Task)
                .filter(Task.status == "done")
                .filter(Task.completed_at.isnot(None))
                .filter(Task.completed_at >= week_ago)
                .order_by(Task.completed_at.desc())
                .all()
            )
            recent_events = s.query(Event).order_by(Event.id.desc()).limit(100).all()
            feedback_rows = (
                s.query(NextMoveFeedback)
                .order_by(NextMoveFeedback.id.desc())
                .limit(120)
                .all()
            )

        open_goals_payload: list[dict[str, Any]] = []
        goal_by_id: dict[int, Goal] = {
            int(g.id): g for g in [*open_goals, *completed_goals]
        }
        recent_not_for_now_task_public_ids: list[str] = []
        for fb in feedback_rows:
            created_at = getattr(fb, "created_at", None) or now
            if getattr(created_at, "tzinfo", None) is None:
                created_at = created_at.replace(tzinfo=dt.timezone.utc)
            if (now - created_at.astimezone(dt.timezone.utc)) > dt.timedelta(hours=24):
                continue
            if str(getattr(fb, "feedback_type", "") or "").strip() != "dismiss":
                continue
            pid = str(getattr(fb, "task_public_id", "") or "").strip()
            if pid and pid not in recent_not_for_now_task_public_ids:
                recent_not_for_now_task_public_ids.append(pid)
        tasks_by_goal: dict[int, list[dict[str, Any]]] = {}
        for t in open_tasks:
            task_type = str(
                getattr(t, "task_type", "") or ""
            ).strip().lower() or self._infer_task_type(t.title, t.content)
            estimated_minutes = int(
                getattr(t, "estimated_minutes", 0) or 0
            ) or self._infer_estimated_minutes(task_type, t.title, t.content)
            space = spaces_by_task.get(t.public_id)
            tasks_by_goal.setdefault(int(t.goal_id), []).append(
                {
                    "id": int(t.id),
                    "public_id": t.public_id,
                    "title": t.title,
                    "content": t.content,
                    "status": t.status,
                    "task_type": task_type,
                    "task_type_label": self._task_type_label(task_type),
                    "estimated_minutes": estimated_minutes,
                    "context_key": str(getattr(t, "context_key", "") or ""),
                    "agent_space_root_path": getattr(space, "root_path", "")
                    if space is not None
                    else "",
                    "created_at": self._iso(t.created_at),
                }
            )
        for g in open_goals:
            open_goals_payload.append(
                {
                    "id": int(g.id),
                    "title": g.title,
                    "content": g.content,
                    "status": g.status,
                    "priority": g.priority,
                    "importance": g.importance,
                    "due_date": self._iso(g.due_date),
                    "created_at": self._iso(g.created_at),
                    "tasks": tasks_by_goal.get(int(g.id), []),
                }
            )

        return {
            "now": now.isoformat(),
            "long_memory_full_content": self._read_text(
                memory_service.long_term_path()
            ),
            "daily_memory_access": {
                "method": "Use tools list_daily_memory_files(limit) and read_daily_memory_file(rel_path).",
                "available_files_preview": self._memory_daily_files()[:14],
            },
            "event_access": {
                "recent_events_included": 100,
                "method": "Use tool list_recent_events(offset, limit) to read more events beyond the latest 100.",
            },
            "recent_events_latest_100": [
                self._serialize_event(ev) for ev in recent_events
            ],
            "open_goals_and_tasks": open_goals_payload,
            "recent_not_for_now_task_public_ids": recent_not_for_now_task_public_ids,
            "completed_last_7_days": {
                "goals": [
                    {
                        "id": int(g.id),
                        "title": g.title,
                        "content": g.content,
                        "priority": g.priority,
                        "importance": g.importance,
                        "due_date": self._iso(g.due_date),
                        "created_at": self._iso(g.created_at),
                    }
                    for g in completed_goals
                ],
                "tasks": [
                    {
                        "id": int(t.id),
                        "public_id": t.public_id,
                        "goal_id": int(t.goal_id),
                        "goal_title": getattr(
                            goal_by_id.get(int(t.goal_id)), "title", ""
                        ),
                        "title": t.title,
                        "content": t.content,
                        "completed_at": self._iso(t.completed_at),
                    }
                    for t in completed_tasks
                ],
            },
            "recent_next_move_feedback": [
                {
                    "id": int(fb.id),
                    "run_id": fb.run_id,
                    "task_public_id": fb.task_public_id,
                    "feedback_type": fb.feedback_type,
                    "reason_code": fb.reason_code,
                    "reason_text": fb.reason_text,
                    "learned_summary": fb.learned_summary,
                    "created_at": self._iso(fb.created_at),
                }
                for fb in feedback_rows
            ],
        }

    def _normalize_item(
        self, rec: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not isinstance(rec, dict):
            return None
        pid = str(rec.get("task_public_id") or rec.get("public_id") or "").strip()
        if not pid:
            target = rec.get("target") if isinstance(rec.get("target"), dict) else {}
            pid = str(target.get("task_public_id") or "").strip()
        if not pid:
            return None

        task_lookup: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for g in context.get("open_goals_and_tasks") or []:
            if not isinstance(g, dict):
                continue
            for t in g.get("tasks") or []:
                if isinstance(t, dict):
                    task_lookup[str(t.get("public_id") or "")] = (g, t)
        found = task_lookup.get(pid)
        if found is None:
            return None
        if pid in set(context.get("recent_not_for_now_task_public_ids") or []):
            return None
        goal, task = found
        why_raw = rec.get("why")
        why = (
            [str(x).strip() for x in why_raw if str(x).strip()]
            if isinstance(why_raw, list)
            else []
        )
        reason = str(rec.get("reason") or "").strip()
        if reason and reason not in why:
            why.insert(0, reason)
        return {
            "type": "do_task",
            "target": {"goal_id": int(goal.get("id") or 0), "task_public_id": pid},
            "goal_title": str(goal.get("title") or ""),
            "title": str(task.get("title") or rec.get("title") or pid),
            "task_type": str(
                task.get("task_type") or rec.get("task_type") or "execution"
            ),
            "task_type_label": str(
                task.get("task_type_label")
                or self._task_type_label(
                    str(task.get("task_type") or rec.get("task_type") or "")
                )
            ),
            "why": why[:3]
            or [
                "Best next task after considering goals, memory, recent events, and feedback."
            ],
            "reason": reason or (why[0] if why else ""),
            "expected_time_minutes": int(
                task.get("estimated_minutes") or rec.get("expected_time_minutes") or 0
            ),
            "context_switch_cost": str(rec.get("context_switch_cost") or "medium"),
            "confidence": str(rec.get("confidence") or "medium"),
        }

    def _normalize_items(
        self, raw: dict[str, Any], context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        recs: list[Any] = []
        if isinstance(raw.get("recommendations"), list):
            recs.extend(raw.get("recommendations") or [])
        elif isinstance(raw.get("recommendation"), dict):
            recs.append(raw.get("recommendation"))
        elif isinstance(raw.get("item"), dict):
            recs.append(raw.get("item"))

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            item = self._normalize_item(rec, context)
            if item is None:
                continue
            pid = str((item.get("target") or {}).get("task_public_id") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            out.append(item)
            if len(out) >= 2:
                break
        return out

    def run(self, *, sink: EventSink) -> Json:
        context = self._build_context()
        if not any(
            (g.get("tasks") or []) for g in context.get("open_goals_and_tasks") or []
        ):
            return {
                "recommendation": None,
                "recommendations": [],
                "items": [],
                "no_recommendation_reason": "No open executable task.",
                "context_summary": {"candidate_count": 0},
            }

        user_input = json.dumps(
            {
                "task": "Recommend at most two next tasks for the user now. Preserve the user's attention/prefrontal resources while keeping a second fallback option available.",
                "available_tool_methods": {
                    "daily_memory": "list_daily_memory_files(limit), then read_daily_memory_file(rel_path)",
                    "more_events": "list_recent_events(offset, limit)",
                    "goal_details": "list_goals(...), describe_goal(goal_id)",
                },
                "context": context,
            },
            ensure_ascii=False,
            indent=2,
        )

        sink.emit("agent.started", self.name, {"goal_id": self.goal_id})

        try:
            tool_registry = self._build_tool_registry()
            res, _ = run_tool_loop(
                agent_name=self.name,
                system_instructions=self.instructions(),
                user_input=user_input,
                provider=self.provider,
                sink=sink,
                tool_registry=tool_registry,
                response_format={"type": "json_object"},
                config=AgentLoopConfig(
                    max_iterations=5, temperature=0.0, max_tokens=1200
                ),
            )
            raw = parse_json_strict(res.content)
            if not isinstance(raw, dict):
                raise ValueError("invalid recommendation output")
            items = self._normalize_items(raw, context)
            data = {
                "recommendation": items[0] if items else None,
                "recommendations": items,
                "items": items,
                "no_recommendation_reason": raw.get("no_recommendation_reason"),
                "context_summary": {
                    "candidate_count": sum(
                        len(g.get("tasks") or [])
                        for g in context.get("open_goals_and_tasks") or []
                        if isinstance(g, dict)
                    ),
                    "recent_events_included": 100,
                    "daily_memory_tool_available": True,
                    "more_events_tool_available": True,
                    "feedback_count": len(
                        context.get("recent_next_move_feedback") or []
                    ),
                    "latest_event_id": max(
                        [
                            int(ev.get("id") or 0)
                            for ev in context.get("recent_events_latest_100") or []
                            if isinstance(ev, dict)
                        ]
                        or [0]
                    ),
                },
            }
        except Exception as e:
            sink.emit("agent.fallback", self.name, {"error": str(e)})
            data = self._fallback(error=str(e))

        sink.emit(
            "agent.completed", self.name, {"goal_id": self.goal_id, "result": data}
        )
        return data
