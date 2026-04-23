from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class ChatMessage:
    role: str
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    # OpenAI tool_calls payload (list)
    tool_calls: list[dict[str, Any]] | None = None


@dataclass
class LLMCallResult:
    content: str
    finish_reason: str | None
    usage: dict[str, Any]
    tool_calls: list[dict[str, Any]] | None = None


class LLMProvider(Protocol):
    def chat_completions(
        self,
        *,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMCallResult: ...

