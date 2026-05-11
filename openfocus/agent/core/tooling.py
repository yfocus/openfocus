# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .types import Json, ToolFn, ToolSpec


@dataclass
class SimpleToolRegistry:
    _specs: dict[str, ToolSpec]
    _handlers: dict[str, ToolFn]

    @classmethod
    def empty(cls) -> "SimpleToolRegistry":
        return cls(_specs={}, _handlers={})

    def register(self, spec: ToolSpec, handler: ToolFn) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def call(self, name: str, arguments: Json) -> str:
        if name not in self._handlers:
            raise KeyError(f"Unknown tool: {name}")
        return self._handlers[name](arguments)


def openai_tools_payload(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters_json_schema,
            },
        }
        for s in specs
    ]
