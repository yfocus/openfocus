# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ...domains.recommendations.context_builder import (
    RecommendationContextBuilder,
    task_type_label,
)
from ...domains.recommendations.tool_adapter import build_recommendation_tool_registry
from ..core.loop import AgentLoopConfig, parse_json_strict, run_tool_loop
from ..core.tooling import SimpleToolRegistry
from ..core.types import EventSink, Json
from ..llm.types import LLMProvider


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
        return task_type_label(task_type)

    def _build_tool_registry(self) -> SimpleToolRegistry:
        return build_recommendation_tool_registry()

    def _build_context(self) -> dict[str, Any]:
        return RecommendationContextBuilder(goal_id=self.goal_id).build()

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
