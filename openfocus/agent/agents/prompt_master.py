# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ...db import session_scope
from ...models import AgentSpace, Goal, Task
from ..llm.types import LLMProvider

MAX_PROMPT_MASTER_INPUT_LENGTH = 20000


class PromptMasterError(RuntimeError):
    pass


class PromptMasterValidationError(PromptMasterError):
    pass


class PromptMasterSpaceNotFound(PromptMasterError):
    pass


class PromptMasterLLMError(PromptMasterError):
    pass


@dataclass(frozen=True)
class PromptMasterTaskContext:
    task_public_id: str
    task_title: str
    task_content: str
    goal_title: str = ""
    goal_content: str = ""


@dataclass(frozen=True)
class PromptMasterResult:
    prompt: str
    usage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": True, "prompt": self.prompt}
        if self.usage:
            payload["usage"] = self.usage
        return payload


def clean_prompt_master_input(prompt: str) -> str:
    clean = str(prompt or "").strip()
    if not clean:
        raise PromptMasterValidationError("prompt is required")
    if len(clean) > MAX_PROMPT_MASTER_INPUT_LENGTH:
        raise PromptMasterValidationError("prompt is too long (<=20000)")
    return clean


def load_agent_space_task_context(space_id: int) -> PromptMasterTaskContext:
    with session_scope() as s:
        space = s.get(AgentSpace, int(space_id))
        if space is None:
            raise PromptMasterSpaceNotFound("AgentSpace not found")

        task_public_id = str(space.task_public_id or "").strip()
        task = s.query(Task).filter(Task.public_id == task_public_id).one_or_none()
        if task is None:
            raise PromptMasterSpaceNotFound("Task not found")

        goal = s.get(Goal, int(task.goal_id)) if task.goal_id else None
        return PromptMasterTaskContext(
            task_public_id=task_public_id,
            task_title=str(task.title or ""),
            task_content=str(task.content or ""),
            goal_title=str(getattr(goal, "title", "") or "") if goal else "",
            goal_content=str(getattr(goal, "content", "") or "") if goal else "",
        )


@dataclass
class PromptMasterAgent:
    provider: LLMProvider
    task_context: PromptMasterTaskContext
    name: str = "prompt_master"

    def instructions(self) -> str:
        return (
            "You are OpenFocus Prompt Master inside AgentSpace.\n"
            "Optimize the user's draft prompt for a command-line coding agent that will work in the current task workspace.\n"
            "Preserve the user's intent, constraints, and requested output. Make vague requests concrete, add relevant task context, and remove filler.\n"
            "Do not create a prompt catalog item. Do not ask to send anything to a terminal. Do not include commentary.\n"
            "Return only the optimized prompt text."
        )

    def optimize(self, prompt: str) -> PromptMasterResult:
        clean_prompt = clean_prompt_master_input(prompt)
        context_payload = {
            "task_public_id": self.task_context.task_public_id,
            "task_title": self.task_context.task_title,
            "task_content": self.task_context.task_content,
            "goal_title": self.task_context.goal_title,
            "goal_content": self.task_context.goal_content,
            "draft_prompt": clean_prompt,
        }

        try:
            result = self.provider.chat_completions(
                messages=[
                    {"role": "system", "content": self.instructions()},
                    {
                        "role": "user",
                        "content": json.dumps(
                            context_payload, ensure_ascii=False, indent=2
                        ),
                    },
                ],
                temperature=0.2,
                max_tokens=1600,
                tools=None,
                response_format=None,
            )
        except Exception as exc:
            raise PromptMasterLLMError(str(exc) or "LLM call failed") from exc

        optimized = str(getattr(result, "content", "") or "").strip()
        if not optimized:
            raise PromptMasterLLMError("LLM returned an empty prompt")

        return PromptMasterResult(
            prompt=optimized,
            usage=dict(getattr(result, "usage", {}) or {}),
        )


def optimize_agent_space_prompt(
    *,
    space_id: int,
    prompt: str,
    provider: LLMProvider,
) -> PromptMasterResult:
    clean_prompt = clean_prompt_master_input(prompt)
    task_context = load_agent_space_task_context(space_id)
    return PromptMasterAgent(provider=provider, task_context=task_context).optimize(
        clean_prompt
    )
