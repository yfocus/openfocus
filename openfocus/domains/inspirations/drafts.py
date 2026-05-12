# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from typing import Any

from ...models import InspirationMessage, InspirationResource, InspirationSpace
from .resources import resource_reference


def truncate_text(text: str, n: int = 20) -> str:
    value = (text or "").strip()
    if len(value) <= n:
        return value
    return value[:n].rstrip() + "…"


def context_lines(
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
    *,
    max_messages: int = 18,
) -> str:
    lines = [f"Space title: {space.title}"]
    if resources:
        lines.append("Resources:")
        for res in resources[:20]:
            lines.append(f"- {resource_reference(res).replace(chr(10), ' | ')}")
    if messages:
        lines.append("Conversation:")
        for msg in messages[-max_messages:]:
            role = str(getattr(msg, "role", "assistant") or "assistant")
            body = str(getattr(msg, "content", "") or "").strip()
            if not body:
                continue
            lines.append(f"{role}: {body}")
    return "\n".join(lines)[:16000]


def fallback_reply(space: InspirationSpace, user_text: str) -> str:
    body = str(user_text or "").strip()
    if body.startswith("/draft_goal_tasks"):
        return "I created a fallback draft from the current discussion. Review it and refine in chat if needed."
    if body.startswith("/summary_title"):
        return "I suggested a few title options based on the current discussion."
    return (
        f"I noted your update about '{space.title}'. "
        "What is the most important outcome, constraint, or success signal we should clarify next?"
    )


def fallback_title_suggestions(
    space: InspirationSpace, messages: list[InspirationMessage]
) -> list[str]:
    base = str(space.title or "Inspiration").strip() or "Inspiration"
    latest = ""
    for msg in reversed(messages):
        if str(getattr(msg, "role", "") or "") == "user":
            latest = str(getattr(msg, "content", "") or "").strip()
            if latest:
                break
    suggestions = [base]
    if latest:
        suggestions.append(
            truncate_text(latest.replace("/summary_title", "").strip() or base, 20)
        )
    suggestions.append(truncate_text(base + " / Refined", 20))
    out: list[str] = []
    for item in suggestions:
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in out:
            out.append(cleaned[:80])
    return out[:3] or [base]


def fallback_draft(
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
) -> dict:
    context = context_lines(space, messages, resources)
    desc = context[:1800]
    tasks = [
        {
            "title": f"Clarify the scope of {space.title}",
            "description": "Define the expected outcome, non-goals, and constraints.",
        },
        {
            "title": f"Draft an execution approach for {space.title}",
            "description": "Turn the discussion into an actionable plan with key milestones.",
        },
        {
            "title": f"Review risks and open questions for {space.title}",
            "description": "List unresolved questions and confirm the next decision points.",
        },
    ]
    return {
        "goal_title": space.title,
        "goal_description": desc,
        "tasks": tasks,
        "open_questions": ["Which part should be implemented first?"],
        "rejected_or_deferred_ideas": [],
    }


def llm_reply(
    provider: Any,
    *,
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
) -> str:
    convo = [
        {
            "role": "system",
            "content": (
                "You are OpenFocus Inspiration assistant. "
                "Be a proactive planning partner. Ask one clarifying question or provide one concrete synthesis. "
                'Return strict JSON only: {"message":"..."}.'
            ),
        },
        {"role": "user", "content": context_lines(space, messages, resources)},
    ]
    data = json.loads(
        provider.chat_completions(
            messages=convo,
            temperature=0.2,
            max_tokens=500,
            response_format={"type": "json_object"},
        ).content
    )
    return str(
        data.get("message") or "Please tell me more about the desired outcome."
    ).strip()


def llm_title_suggestions(
    provider: Any,
    *,
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
) -> list[str]:
    convo = [
        {
            "role": "system",
            "content": (
                "You generate concise English or Chinese titles for an inspiration workspace. "
                'Return strict JSON only: {"titles":["...","...","..."]}. '
                "Each title should be <= 80 chars, distinct, and useful as a workspace title."
            ),
        },
        {"role": "user", "content": context_lines(space, messages, resources)},
    ]
    data = json.loads(
        provider.chat_completions(
            messages=convo,
            temperature=0.3,
            max_tokens=300,
            response_format={"type": "json_object"},
        ).content
    )
    out: list[str] = []
    for item in data.get("titles") or []:
        title = str(item or "").strip()
        if title and title not in out:
            out.append(title[:80])
    return out[:5]


def llm_draft(
    provider: Any,
    *,
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
) -> dict:
    convo = [
        {
            "role": "system",
            "content": (
                "You are OpenFocus Inspiration planning assistant. "
                "Generate a publish-ready draft from the discussion. "
                "Return strict JSON only with keys: goal_title, goal_description, tasks, open_questions, rejected_or_deferred_ideas. "
                "tasks must be an array of objects; each task object must include title and description."
            ),
        },
        {"role": "user", "content": context_lines(space, messages, resources)},
    ]
    data = json.loads(
        provider.chat_completions(
            messages=convo,
            temperature=0.1,
            max_tokens=1400,
            response_format={"type": "json_object"},
        ).content
    )
    tasks: list[dict] = []
    for raw in data.get("tasks") or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        tasks.append(
            {
                "title": title[:512],
                "description": str(raw.get("description") or "").strip()[:4000],
            }
        )
    return {
        "goal_title": str(data.get("goal_title") or space.title).strip()[:2000],
        "goal_description": str(data.get("goal_description") or "").strip()[:4000],
        "tasks": tasks,
        "open_questions": [
            str(x).strip()[:500]
            for x in (data.get("open_questions") or [])
            if str(x or "").strip()
        ][:20],
        "rejected_or_deferred_ideas": [
            str(x).strip()[:500]
            for x in (data.get("rejected_or_deferred_ideas") or [])
            if str(x or "").strip()
        ][:20],
    }


def make_phase_summary(
    space: InspirationSpace,
    messages: list[InspirationMessage],
    resources: list[InspirationResource],
) -> str:
    recent_user = [
        str(m.content or "").strip()
        for m in messages[-20:]
        if str(getattr(m, "role", "") or "") == "user" and str(m.content or "").strip()
    ]
    resource_names = [
        str(r.name or f"resource-{r.resource_seq_id}") for r in resources[:8]
    ]
    lines = [f"Space: {space.title}"]
    if recent_user:
        lines.append("Recent user points:")
        lines.extend(f"- {item[:200]}" for item in recent_user[-6:])
    if resource_names:
        lines.append("Resources in use:")
        lines.extend(f"- {name}" for name in resource_names)
    return "\n".join(lines)
