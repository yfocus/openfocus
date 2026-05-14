# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...db import session_scope
from ...domains.events import service as event_service
from ...models import Goal, Task
from ..core.loop import AgentLoopConfig, parse_json_strict, run_tool_loop
from ..core.types import EventSink, Json
from ..llm.types import LLMProvider
from ..tools.goals import build_goal_tools


@dataclass
class TaskDecomposerAgent:
    """把 Goal 拆解为可执行 Task（MVP：生成 title 列表 + 写入 tasks 表）。"""

    goal_id: int
    provider: LLMProvider
    name: str = "task_decomposer"

    def instructions(self) -> str:
        return (
            "你是一个严谨的任务拆解助手。\n"
            "目标：把用户的 Goal 拆解为 5~15 个可执行的 Task。\n"
            "如果你需要获取当前系统中的目标列表或某个目标的详细信息，优先使用工具：\n"
            "- list_goals(...)\n"
            "- describe_goal(goal_id=...) / describe_gloal(goal_id=...)\n"
            "不要凭空假设数据库里的 goal/task 状态。\n"
            "输出必须是严格 JSON（不要 Markdown），格式如下：\n"
            "{\n"
            '  "tasks": [\n'
            '    {"title": "...", "rationale": "...", "estimate_minutes": 30},\n'
            "    ...\n"
            "  ]\n"
            "}\n"
            "约束：title 简短明确；estimate_minutes 为 5~480 的整数；rationale 1 句话。"
        )

    def run(self, *, sink: EventSink) -> Json:
        with session_scope() as s:
            goal = s.get(Goal, self.goal_id)
            if goal is None:
                raise ValueError(f"Goal not found: {self.goal_id}")

        user_input = (
            f"Goal title：{goal.title}\n"
            f"Goal content：{goal.content}\n"
            f"完成时间：{goal.due_date.isoformat()}\n"
            "请输出 JSON。"
        )

        sink.emit("agent.started", self.name, {"goal_id": self.goal_id})

        tool_registry = build_goal_tools()
        res, _messages = run_tool_loop(
            agent_name=self.name,
            system_instructions=self.instructions(),
            user_input=user_input,
            provider=self.provider,
            sink=sink,
            tool_registry=tool_registry,
            response_format={"type": "json_object"},
            config=AgentLoopConfig(max_iterations=3, temperature=0.0, max_tokens=1200),
        )

        data = parse_json_strict(res.content)
        if not isinstance(data, dict) or "tasks" not in data:
            raise ValueError(f"Invalid LLM JSON output: {data!r}")
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("Invalid tasks field")

        created: list[dict[str, Any]] = []
        with session_scope() as s:
            for t in tasks:
                if not isinstance(t, dict):
                    continue
                title = str(t.get("title") or "").strip()
                if not title:
                    continue
                content = str(t.get("rationale") or "").strip()
                obj = Task(
                    goal_id=self.goal_id,
                    title=title,
                    content=content,
                    status="todo",
                )
                s.add(obj)
                s.flush()
                event_service.record_event(
                    s,
                    kind="task.created",
                    agent=self.name,
                    task_id=str(obj.public_id or ""),
                    payload={
                        "goal_id": int(obj.goal_id),
                        "task_public_id": str(obj.public_id or ""),
                        "title": str(obj.title or ""),
                    },
                    audit=False,
                )
                created.append(
                    {
                        "id": obj.id,
                        "public_id": obj.public_id,
                        "title": obj.title,
                    }
                )

        sink.emit(
            "agent.completed",
            self.name,
            {
                "goal_id": self.goal_id,
                "created_tasks": created,
                "usage": res.usage,
            },
        )

        return {"goal_id": self.goal_id, "created_tasks": created}
