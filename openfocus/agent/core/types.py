# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

Json = dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    # OpenAI tools schema: {"type":"object","properties":...,"required":...}
    parameters_json_schema: Json


ToolFn = Callable[[Json], str]


class ToolRegistry(Protocol):
    def specs(self) -> list[ToolSpec]: ...

    def call(self, name: str, arguments: Json) -> str: ...


class EventSink(Protocol):
    def emit(
        self,
        kind: str,
        agent: str,
        payload: Json | None = None,
        task_id: str | None = None,
    ) -> None: ...


class Agent(Protocol):
    name: str

    def instructions(self) -> str:
        """系统级指令（system prompt）。"""

    def run(self, *, sink: EventSink) -> Json:
        """执行一次 agent 任务，返回结构化结果。"""
