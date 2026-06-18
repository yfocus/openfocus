# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

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
            "You are OpenFocus Prompt Master.\n"
            "Rewrite the user's draft into one clear, fluent prompt while preserving the user's original meaning.\n"
            "Language policy:\n"
            "- If the draft is mainly Chinese or broken Chinese, return fluent natural Chinese.\n"
            "- If the draft is mainly English or broken English, return fluent natural English.\n"
            "- If the draft mixes languages, use the language that best preserves the user's apparent intent.\n"
            "Strict preservation rules:\n"
            "- Do not add task, goal, repository, product, user, or AgentSpace context unless that information is explicitly present in the draft.\n"
            "- Do not change the requested subject, audience, tone, constraints, or deliverable.\n"
            "- Do not turn a general writing/translation request into a coding or implementation prompt.\n"
            "- Do not invent numbered requirements, context blocks, IDs, file names, or project details.\n"
            "Style rules:\n"
            "- Make fragmented text grammatically complete and polite when appropriate.\n"
            "- Remove filler and ambiguity only when it does not change meaning.\n"
            "- Return only the rewritten prompt text. Do not use Markdown fences, labels, explanations, or commentary.\n"
            "Examples:\n"
            "Draft: 餐厅推荐，好吃的地方，尊贵的客人需要，中文我不好\n"
            "Output: 请推荐一些适合招待尊贵客人的好吃餐厅。我的中文不太好，请帮我把表达写得自然、得体一些。\n"
            "Draft: restaurant recommendation good place important guest my chinese not good\n"
            "Output: Please recommend good restaurants suitable for hosting an important guest. My Chinese is not very good, so please help me phrase the request naturally and politely."
        )

    def optimize(self, prompt: str) -> PromptMasterResult:
        clean_prompt = clean_prompt_master_input(prompt)

        try:
            result = self.provider.chat_completions(
                messages=[
                    {"role": "system", "content": self.instructions()},
                    {
                        "role": "user",
                        "content": (
                            "Rewrite this draft prompt. Preserve only the user's intent from inside the delimiters.\n"
                            'Draft prompt:\n"""\n'
                            f"{clean_prompt}\n"
                            '"""'
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

        optimized = _strip_wrapping_code_fence(
            str(getattr(result, "content", "") or "")
        )
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


def _strip_wrapping_code_fence(text: str) -> str:
    clean = str(text or "").strip()
    if not clean.startswith("```") or not clean.endswith("```"):
        return clean
    lines = clean.splitlines()
    if len(lines) < 2:
        return clean
    if not lines[0].startswith("```") or lines[-1].strip() != "```":
        return clean
    return "\n".join(lines[1:-1]).strip()
