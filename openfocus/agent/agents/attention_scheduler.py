from __future__ import annotations

from dataclasses import dataclass

from ...db import session_scope
from ...models import Goal, Task
from ..core.loop import AgentLoopConfig, parse_json_strict, run_tool_loop
from ..core.types import EventSink, Json
from ..llm.types import LLMProvider
from ..tools.goals import build_goal_tools


@dataclass
class AttentionSchedulerAgent:
    """基于目标/任务/完成情况，推荐用户下一步要做的 Task。"""

    provider: LLMProvider
    goal_id: int | None = None
    name: str = "attention_scheduler"

    def instructions(self) -> str:
        return (
            "你是注意力调度助手。\n"
            "输入包含 Goals 与 Tasks，请推荐用户下一步最应该做的 1~3 个 Task。\n"
            "如果你需要获取最新的目标/任务信息，请优先使用工具而不是猜测：\n"
            "- list_goals(...)：按未完成/紧急/非常重要/时间范围过滤\n"
            "- describe_goal(goal_id=...) / describe_gloal(goal_id=...)：查看某个 goal 及其 tasks\n"
            "输出必须是严格 JSON（不要 Markdown），格式如下：\n"
            "{\n"
            '  "recommendations": [\n'
            '    {"task_public_id": "...", "reason": "..."},\n'
            "    ...\n"
            "  ]\n"
            "}\n"
            "约束：reason 1 句话，可解释（考虑截止期/阻塞/连续性/收益）。"
        )

    def _fallback(self) -> Json:
        with session_scope() as s:
            q = s.query(Task).filter(Task.status == "todo")
            if self.goal_id is not None:
                q = q.filter(Task.goal_id == self.goal_id)
            t = q.order_by(Task.id.asc()).first()
        if t is None:
            return {"recommendations": []}
        return {
            "recommendations": [
                {
                    "task_public_id": t.public_id,
                    "reason": "按顺序推进未完成任务（fallback 规则）。",
                }
            ]
        }

    def run(self, *, sink: EventSink) -> Json:
        with session_scope() as s:
            goals_q = s.query(Goal).order_by(Goal.due_date.asc()).all()
            tasks_q = s.query(Task).order_by(Task.id.asc()).all()

        if not tasks_q:
            return {"recommendations": []}

        # 构建给 LLM 的上下文（压缩版）
        goals_text = "\n".join(
            [f"- goal_id={g.id} due={g.due_date} content={g.content}" for g in goals_q]
        )
        tasks_text = "\n".join(
            [
                f"- task_public_id={t.public_id} goal_id={t.goal_id} status={t.status} title={t.title}"
                for t in tasks_q
                if self.goal_id is None or t.goal_id == self.goal_id
            ]
        )

        user_input = (
            "Goals:\n" + goals_text + "\n\n" + "Tasks:\n" + tasks_text + "\n\n" + "请输出 JSON。"
        )

        sink.emit("agent.started", self.name, {"goal_id": self.goal_id})

        try:
            tool_registry = build_goal_tools()
            res, _ = run_tool_loop(
                agent_name=self.name,
                system_instructions=self.instructions(),
                user_input=user_input,
                provider=self.provider,
                sink=sink,
                tool_registry=tool_registry,
                response_format={"type": "json_object"},
                config=AgentLoopConfig(max_iterations=3, temperature=0.0, max_tokens=900),
            )
            data = parse_json_strict(res.content)
            if not isinstance(data, dict) or "recommendations" not in data:
                raise ValueError("invalid recommendation output")
        except Exception as e:
            sink.emit("agent.fallback", self.name, {"error": str(e)})
            data = self._fallback()

        sink.emit("agent.completed", self.name, {"goal_id": self.goal_id, "result": data})
        return data
