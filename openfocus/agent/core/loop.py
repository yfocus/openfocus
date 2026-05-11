# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from ..llm.types import ChatMessage, LLMCallResult, LLMProvider
from .tooling import openai_tools_payload
from .types import EventSink, Json, ToolRegistry


@dataclass
class AgentLoopConfig:
    max_iterations: int = 8
    temperature: float = 0.0
    max_tokens: int = 1200


def _to_openai_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        item: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.name:
            item["name"] = m.name
        if m.tool_call_id:
            item["tool_call_id"] = m.tool_call_id
        if m.tool_calls:
            item["tool_calls"] = m.tool_calls
        out.append(item)
    return out


def run_tool_loop(
    *,
    agent_name: str,
    system_instructions: str,
    user_input: str,
    provider: LLMProvider,
    sink: EventSink,
    tool_registry: ToolRegistry | None = None,
    tools: list[dict[str, Any]] | None = None,
    response_format: dict[str, Any] | None = None,
    config: AgentLoopConfig | None = None,
) -> tuple[LLMCallResult, list[ChatMessage]]:
    """一个最小的 agent 核心循环（参考 honcho 的 tool loop 思路）。

    - 支持 OpenAI-style tool_calls（函数调用）
    - 每轮调用都会通过 sink 记录 event（含 token 用量）
    """
    cfg = config or AgentLoopConfig()
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=system_instructions),
        ChatMessage(role="user", content=user_input),
    ]

    if tool_registry is not None:
        tools = openai_tools_payload(tool_registry.specs())

    last: LLMCallResult | None = None
    for i in range(1, cfg.max_iterations + 1):
        sink.emit(
            kind="agent.llm_call.started",
            agent=agent_name,
            payload={"iteration": i},
        )
        started = time.time()

        last = provider.chat_completions(
            messages=_to_openai_messages(messages),
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            tools=tools,
            response_format=response_format,
        )

        sink.emit(
            kind="agent.llm_call.completed",
            agent=agent_name,
            payload={
                "iteration": i,
                "wall_seconds": round(time.time() - started, 6),
                "usage": last.usage,
                "finish_reason": last.finish_reason,
            },
        )

        messages.append(
            ChatMessage(
                role="assistant", content=last.content, tool_calls=last.tool_calls
            )
        )

        # 无 tool_calls 就结束
        if not last.tool_calls:
            break

        # 有 tool_calls：若未提供 registry，则无法执行，直接终止并让调用方处理
        if tool_registry is None:
            sink.emit(
                kind="agent.tool_calls.detected",
                agent=agent_name,
                payload={"iteration": i, "tool_calls": last.tool_calls},
            )
            break

        # 执行工具，并把结果写回 messages，再进入下一轮
        for tc in last.tool_calls:
            try:
                fn = tc.get("function") or {}
                name = fn.get("name")
                args_raw = fn.get("arguments") or "{}"
                args = (
                    json.loads(args_raw)
                    if isinstance(args_raw, str)
                    else (args_raw or {})
                )
                tool_call_id = tc.get("id")

                sink.emit(
                    kind="agent.tool_call.started",
                    agent=agent_name,
                    payload={
                        "name": name,
                        "arguments": args,
                        "tool_call_id": tool_call_id,
                    },
                )
                out = tool_registry.call(str(name), args)
                sink.emit(
                    kind="agent.tool_call.completed",
                    agent=agent_name,
                    payload={"name": name, "tool_call_id": tool_call_id},
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=str(out),
                        tool_call_id=str(tool_call_id) if tool_call_id else None,
                    )
                )
            except Exception as e:
                sink.emit(
                    kind="agent.tool_call.failed",
                    agent=agent_name,
                    payload={"error": str(e), "tool_call": tc},
                )
                # 失败也写回 tool message，避免 LLM 卡死
                tool_call_id = tc.get("id")
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=f"ERROR: {e}",
                        tool_call_id=str(tool_call_id) if tool_call_id else None,
                    )
                )

    if last is None:
        raise RuntimeError("LLM call not executed")
    return last, messages


def parse_json_strict(text: str) -> Json | list[Any]:
    """尽量严格解析 JSON（用于 LLM 的结构化输出）。"""
    return json.loads(text)
