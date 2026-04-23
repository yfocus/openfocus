from __future__ import annotations

from dataclasses import dataclass

from ...db import session_scope
from ...models import Goal, Task
from ..core.loop import AgentLoopConfig, parse_json_strict, run_tool_loop
from ..core.types import EventSink, Json
from ..llm.types import LLMProvider
from ..tools.goals import build_goal_tools


@dataclass
class TaskPromptRecommenderAgent:
    """为某个 Task 生成可复制粘贴给外部 Agent 的推荐提示词（按需生成，不落库）。"""

    task_public_id: str
    provider: LLMProvider
    name: str = "task_prompt_recommender"

    def instructions(self) -> str:
        return (
            "你是一个提示词工程助手。\n"
            "目标：为用户生成一段可直接复制给外部执行型 Agent 的中文提示词，用于完成一个具体 Task。\n"
            "重要：这段提示词必须内嵌 taskId，并要求外部 Agent 在执行过程中定期向 OpenFocus 上报进度与 token 用量。\n"
            "你必须严格输出 JSON（不要 Markdown），格式如下：\n"
            "{\n"
            '  \"prompt\": \"...\"\n'
            "}\n"
            "约束：\n"
            "- prompt 必须包含：taskId=<uuid>（原样保留），并要求所有上报携带该 taskId。\n"
            "- prompt 必须包含进度上报频率建议：开始 + 每个里程碑或每 10~15 分钟 + 结束。\n"
            "- prompt 必须要求上报 token 用量 usage（prompt_tokens/completion_tokens/total_tokens），无法获得时允许 total_tokens=null 并说明原因。\n"
            "- prompt 必须给出可执行的上报示例（至少包含 POST /api/agent/events；可选包含 POST /api/skills/focus_report 作为最终结果上报）。\n"
            "- prompt 中必须包含任务标题与（若提供）Goal 摘要；输出应可直接粘贴使用。"
        )

    def run(self, *, sink: EventSink) -> Json:
        with session_scope() as s:
            t = s.query(Task).filter(Task.public_id == self.task_public_id).one_or_none()
            if t is None:
                raise ValueError(f"Task not found: public_id={self.task_public_id}")
            g = s.get(Goal, t.goal_id)

        goal_summary = ""
        if g is not None:
            goal_summary = (
                f"Goal: {g.content}\n"
                f"Goal description: {g.description}\n"
                f"Goal due_date: {g.due_date}\n"
            )

        user_input = (
            "请基于以下信息生成推荐提示词（中文）。\n"
            f"task_title: {t.title}\n"
            f"taskId: {t.public_id}\n"
            + (goal_summary + "\n" if goal_summary else "")
            + "OpenFocus server: 默认 http://127.0.0.1:8001（如不一致请在提示词中提醒用户替换）。\n"
            "上报接口：POST /api/agent/events 与 POST /api/skills/focus_report\n"
            "请输出 JSON。"
        )

        tool_registry = build_goal_tools()
        res, _messages = run_tool_loop(
            agent_name=self.name,
            system_instructions=self.instructions(),
            user_input=user_input,
            provider=self.provider,
            sink=sink,
            tool_registry=tool_registry,
            response_format={"type": "json_object"},
            config=AgentLoopConfig(max_iterations=2, temperature=0.2, max_tokens=900),
        )

        data = parse_json_strict(res.content)
        if not isinstance(data, dict) or not isinstance(data.get("prompt"), str):
            raise ValueError(f"Invalid LLM JSON output: {data!r}")

        prompt = str(data["prompt"]).strip()
        if self.task_public_id not in prompt:
            raise ValueError("Generated prompt missing taskId")
        if "/api/agent/events" not in prompt:
            raise ValueError("Generated prompt missing /api/agent/events")
        if "/api/skills/focus_report" not in prompt:
            raise ValueError("Generated prompt missing /api/skills/focus_report")

        return {"prompt": prompt, "usage": res.usage}
