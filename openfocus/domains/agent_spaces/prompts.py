# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...db import session_scope
from ...models import AgentSpacePrompt

MAX_PROMPT_TITLE_LENGTH = 160
MAX_PROMPT_CONTENT_LENGTH = 20000


class AgentSpacePromptUseCaseError(RuntimeError):
    pass


class AgentSpacePromptValidationError(AgentSpacePromptUseCaseError):
    pass


class AgentSpacePromptNotFound(AgentSpacePromptUseCaseError):
    pass


@dataclass(frozen=True)
class AgentSpacePromptPayload:
    id: int
    title: str
    content: str
    enabled: bool
    auto_enabled: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "enabled": self.enabled,
            "auto_enabled": self.auto_enabled,
        }


@dataclass(frozen=True)
class AgentSpacePromptListResult:
    items: list[AgentSpacePromptPayload]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": True, "items": [item.to_dict() for item in self.items]}


@dataclass(frozen=True)
class AgentSpacePromptMutationResult:
    item: AgentSpacePromptPayload

    def to_dict(self) -> dict[str, Any]:
        return {"ok": True, "item": self.item.to_dict()}


@dataclass(frozen=True)
class AgentSpacePromptDeleteResult:
    deleted: bool

    def to_dict(self) -> dict[str, Any]:
        return {"ok": True}


def _clean_prompt_id(prompt_id: int) -> int:
    try:
        return int(prompt_id)
    except (TypeError, ValueError) as exc:
        raise AgentSpacePromptValidationError("invalid prompt_id") from exc


def _clean_prompt_text(*, title: str, content: str) -> tuple[str, str]:
    clean_title = str(title or "").strip()
    clean_content = str(content or "").strip()
    if not clean_title or not clean_content:
        raise AgentSpacePromptValidationError("title and content are required")
    if len(clean_title) > MAX_PROMPT_TITLE_LENGTH:
        raise AgentSpacePromptValidationError("title is too long (<=160)")
    if len(clean_content) > MAX_PROMPT_CONTENT_LENGTH:
        raise AgentSpacePromptValidationError("content is too long (<=20000)")
    return clean_title, clean_content


def _prompt_payload(prompt: AgentSpacePrompt) -> AgentSpacePromptPayload:
    return AgentSpacePromptPayload(
        id=int(prompt.id),
        title=str(prompt.title or ""),
        content=str(prompt.content or ""),
        enabled=bool(prompt.enabled),
        auto_enabled=bool(getattr(prompt, "auto_enabled", False)),
    )


def list_prompts(*, enabled_only: bool = True) -> AgentSpacePromptListResult:
    with session_scope() as s:
        query = s.query(AgentSpacePrompt)
        if enabled_only:
            query = query.filter(AgentSpacePrompt.enabled == True)  # noqa: E712
        rows = query.order_by(AgentSpacePrompt.id.desc()).all()
        items = [_prompt_payload(row) for row in rows]

    return AgentSpacePromptListResult(items=items)


def list_prompts_for_management() -> AgentSpacePromptListResult:
    with session_scope() as s:
        rows = (
            s.query(AgentSpacePrompt)
            .order_by(AgentSpacePrompt.enabled.desc(), AgentSpacePrompt.id.desc())
            .all()
        )
        items = [_prompt_payload(row) for row in rows]

    return AgentSpacePromptListResult(items=items)


def create_prompt(
    *,
    title: str,
    content: str,
    enabled: bool,
    auto_enabled: bool,
) -> AgentSpacePromptMutationResult:
    clean_title, clean_content = _clean_prompt_text(title=title, content=content)

    with session_scope() as s:
        prompt = AgentSpacePrompt(
            title=clean_title,
            content=clean_content,
            enabled=bool(enabled),
            auto_enabled=bool(auto_enabled),
        )
        s.add(prompt)
        s.flush()
        item = _prompt_payload(prompt)

    return AgentSpacePromptMutationResult(item=item)


def update_prompt(
    prompt_id: int,
    *,
    title: str,
    content: str,
    enabled: bool,
    auto_enabled: bool,
) -> AgentSpacePromptMutationResult:
    clean_prompt_id = _clean_prompt_id(prompt_id)
    clean_title, clean_content = _clean_prompt_text(title=title, content=content)

    with session_scope() as s:
        prompt = s.get(AgentSpacePrompt, clean_prompt_id)
        if prompt is None:
            raise AgentSpacePromptNotFound("AgentSpace prompt not found")
        prompt.title = clean_title
        prompt.content = clean_content
        prompt.enabled = bool(enabled)
        prompt.auto_enabled = bool(auto_enabled)
        s.add(prompt)
        s.flush()
        item = _prompt_payload(prompt)

    return AgentSpacePromptMutationResult(item=item)


def set_prompt_enabled(
    prompt_id: int,
    *,
    enabled: bool,
) -> AgentSpacePromptMutationResult:
    clean_prompt_id = _clean_prompt_id(prompt_id)

    with session_scope() as s:
        prompt = s.get(AgentSpacePrompt, clean_prompt_id)
        if prompt is None:
            raise AgentSpacePromptNotFound("AgentSpace prompt not found")
        prompt.enabled = bool(enabled)
        s.add(prompt)
        s.flush()
        item = _prompt_payload(prompt)

    return AgentSpacePromptMutationResult(item=item)


def set_prompt_auto_enabled(
    prompt_id: int,
    *,
    auto_enabled: bool,
) -> AgentSpacePromptMutationResult:
    clean_prompt_id = _clean_prompt_id(prompt_id)

    with session_scope() as s:
        prompt = s.get(AgentSpacePrompt, clean_prompt_id)
        if prompt is None:
            raise AgentSpacePromptNotFound("AgentSpace prompt not found")
        prompt.auto_enabled = bool(auto_enabled)
        s.add(prompt)
        s.flush()
        item = _prompt_payload(prompt)

    return AgentSpacePromptMutationResult(item=item)


def delete_prompt(prompt_id: int) -> AgentSpacePromptDeleteResult:
    clean_prompt_id = _clean_prompt_id(prompt_id)

    with session_scope() as s:
        prompt = s.get(AgentSpacePrompt, clean_prompt_id)
        if prompt is None:
            return AgentSpacePromptDeleteResult(deleted=False)
        s.delete(prompt)

    return AgentSpacePromptDeleteResult(deleted=True)
