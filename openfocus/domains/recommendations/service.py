# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy.orm import Session

from ...agent.agents.attention_scheduler import AttentionSchedulerAgent
from ...agent.storage.events import DbEventSink
from ...models import AgentSpace, Event, NextMoveFeedback, NextMoveRun, Task
from ..goals import service as goal_service
from ..memory import service as memory_service


class RecommendationError(ValueError):
    """Raised for invalid recommendation inputs."""


class RecommendationTaskNotFound(LookupError):
    """Raised when feedback references a task that does not exist."""


TASK_TYPE_LABELS = {
    "deep_work": "Deep Work",
    "communication": "Communication",
    "review": "Review",
    "execution": "Execution",
    "admin": "Admin",
}


def _task_type_label(task_type: str | None) -> str:
    return TASK_TYPE_LABELS.get(str(task_type or "").strip().lower(), "Execution")


def _feedback_meta(raw: str | None) -> dict:
    text_value = str(raw or "").strip()
    if not text_value:
        return {}
    try:
        data = json.loads(text_value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _sentence(items: list[dict]) -> str | None:
    if not items:
        return None
    titles = [
        str((it.get("title") or "")).strip()
        for it in items[:3]
        if str((it.get("title") or "")).strip()
    ]
    if not titles:
        return None
    if len(titles) == 1:
        return "Recommended next: " + titles[0] + "."
    return "Recommended next: " + ", ".join(titles[:2]) + "."


def _ts(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return value.isoformat() + "Z"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def response_payload(run: NextMoveRun | None) -> dict:
    if run is None:
        return {
            "generated_at": None,
            "run_id": None,
            "item": None,
            "items": [],
            "context_summary": {},
            "last_event_id": 0,
            "sentence": None,
        }
    recommendations = run.recommendations or {}
    items = recommendations.get("items") if isinstance(recommendations, dict) else []
    if not isinstance(items, list):
        items = []
    items = [it for it in items if isinstance(it, dict)][:2]
    context_summary = run.context_summary or {}
    if not isinstance(context_summary, dict):
        context_summary = {}
    try:
        last_event_id = int(context_summary.get("latest_event_id") or 0)
    except Exception:
        last_event_id = 0
    return {
        "generated_at": _ts(run.generated_at),
        "run_id": int(run.id or 0),
        "item": items[0] if items else None,
        "items": items,
        "context_summary": context_summary,
        "last_event_id": last_event_id,
        "sentence": _sentence(items),
    }


def generate_next_moves(
    s: Session,
    *,
    provider: Any | None,
    provider_error: str | None,
    trigger: str = "manual_refresh",
    limit: int = 2,
) -> dict:
    max_items = max(1, min(int(limit or 2), 3))
    if provider is None:
        items: list[dict] = []
        context_summary = {
            "error": provider_error,
            "agent_loop": "unavailable",
            "recommendation_count": 0,
            "latest_event_id": 0,
        }
        no_recommendation_reason = provider_error
    else:
        agent = AttentionSchedulerAgent(provider=provider)
        data = agent.run(sink=DbEventSink())
        items = (data.get("items") or [])[:max_items] if isinstance(data, dict) else []
        context_summary = (
            dict(data.get("context_summary") or {}) if isinstance(data, dict) else {}
        )
        context_summary["agent_loop"] = "attention_scheduler"
        context_summary["recommendation_count"] = len(items)
        no_recommendation_reason = (
            str(data.get("no_recommendation_reason") or "")
            if isinstance(data, dict)
            else ""
        )

    run = NextMoveRun(
        trigger_kind=str(trigger or "manual_refresh")[:64],
        context_summary=context_summary,
        recommendations={"items": items},
    )
    s.add(run)
    s.flush()
    payload = response_payload(run)
    payload["no_recommendation_reason"] = no_recommendation_reason or None
    return payload


def latest_payload(s: Session) -> dict:
    run = s.query(NextMoveRun).order_by(NextMoveRun.id.desc()).first()
    return response_payload(run)


def _learning_note(
    *,
    task_title: str,
    task_type: str,
    reason_code: str,
    reason_text: str,
    estimated_minutes: int,
) -> str:
    type_label = _task_type_label(task_type)
    reason_map = {
        "not_for_now": "user explicitly said not for now and this task should be avoided in the immediate next recommendation",
        "too_much_context_switch": "user wants less context switching",
        "too_long": "user wants a shorter task block right now",
        "wrong_type": "this work type does not fit the current mode",
        "not_important_now": "this task is not important right now",
        "lacking_context": "user needs more context first",
        "waiting_on_someone": "the task is blocked on someone else",
    }
    reason_label = reason_map.get(reason_code, "the recommendation was dismissed")
    note = f"- Next Move feedback: `{task_title}` ({type_label}, ~{estimated_minutes}m) was dismissed because {reason_label}."
    if reason_text:
        note += f" Note: {reason_text.strip()}"
    return note


def submit_feedback(s: Session, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise RecommendationError("payload must be an object")

    task_public_id = str(payload.get("task_public_id") or "").strip()
    if not task_public_id:
        raise RecommendationError("task_public_id is required")

    feedback_type = (
        str(payload.get("feedback_type") or "dismiss").strip().lower() or "dismiss"
    )
    reason_code = str(payload.get("reason_code") or "not_for_now").strip().lower()
    reason_text = str(payload.get("reason_text") or "").strip()
    try:
        run_id = int(payload.get("run_id") or 0) or None
    except Exception:
        run_id = None

    task = s.query(Task).filter(Task.public_id == task_public_id).one_or_none()
    if task is None:
        raise RecommendationTaskNotFound("Task not found")
    space = (
        s.query(AgentSpace)
        .filter(AgentSpace.task_public_id == task_public_id)
        .one_or_none()
    )

    task_type = str(
        getattr(task, "task_type", "") or ""
    ).strip().lower() or goal_service.infer_task_type(task.title, task.content)
    estimated_minutes = int(
        getattr(task, "estimated_minutes", 0) or 0
    ) or goal_service.infer_estimated_minutes(task_type, task.title, task.content)
    context_key = str(
        getattr(task, "context_key", "") or ""
    ).strip() or goal_service.infer_context_key(
        task.title,
        task.content,
        goal_id=int(task.goal_id),
        root_path=getattr(space, "root_path", None),
    )
    learned_summary = json.dumps(
        {
            "feedback_type": feedback_type,
            "reason_code": reason_code,
            "task_type": task_type,
            "estimated_minutes": estimated_minutes,
            "context_key": context_key,
            "goal_id": int(task.goal_id),
        },
        ensure_ascii=False,
    )
    row = NextMoveFeedback(
        run_id=run_id,
        task_public_id=task_public_id,
        feedback_type=feedback_type,
        reason_code=reason_code,
        reason_text=reason_text[:2000],
        learned_summary=learned_summary,
    )
    s.add(row)
    s.flush()
    feedback_id = int(row.id or 0)
    s.add(
        Event(
            kind="next_move.not_for_now"
            if feedback_type == "dismiss"
            else "next_move.feedback",
            agent="web",
            task_id=task_public_id,
            payload={
                "run_id": run_id,
                "feedback_id": feedback_id,
                "feedback_type": feedback_type,
                "reason_code": reason_code,
                "reason_text": reason_text,
                "task_title": task.title,
                "goal_id": int(task.goal_id),
            },
        )
    )
    similar_rows = (
        s.query(NextMoveFeedback)
        .filter(NextMoveFeedback.feedback_type == feedback_type)
        .filter(NextMoveFeedback.reason_code == reason_code)
        .order_by(NextMoveFeedback.id.desc())
        .limit(50)
        .all()
    )

    daily_note = _learning_note(
        task_title=str(task.title or task_public_id),
        task_type=task_type,
        reason_code=reason_code,
        reason_text=reason_text,
        estimated_minutes=estimated_minutes,
    )
    memory_note = None
    if (
        reason_code
        and sum(
            1
            for row in similar_rows
            if _feedback_meta(getattr(row, "learned_summary", "")).get("task_type")
            == task_type
        )
        >= 2
    ):
        if reason_code == "too_long":
            memory_note = f"- Prefer shorter tasks over ~{estimated_minutes}m when dismissing {_task_type_label(task_type)} work."
        elif reason_code == "too_much_context_switch":
            memory_note = f"- Prefer recommendations that continue the current context before suggesting new {_task_type_label(task_type)} work."
        elif reason_code == "wrong_type":
            memory_note = f"- Avoid prioritizing {_task_type_label(task_type)} tasks when the user says the work type is wrong for now."
        elif reason_code == "not_important_now":
            memory_note = "- When the user dismisses a recommendation as not important now, reduce near-term priority for similar work."
        elif reason_code == "not_for_now":
            memory_note = "- When the user says Not for now, avoid immediately recommending the same task again and use the reason text to infer the next best alternative."
    memory_service.persist_feedback_learning(note=daily_note, memory_note=memory_note)

    memory_service.try_audit_memory(
        kind="next_move.feedback",
        source="web",
        summary=f"Next Move feedback for task: {task_public_id}",
        detail=f"Feedback type: {feedback_type}\nReason code: {reason_code or '-'}\nReason text:\n\n{reason_text or '-'}",
        goal_id=int(task.goal_id),
        task_public_id=task_public_id,
        metadata={
            "run_id": run_id,
            "reason_code": reason_code,
            "learned_summary": learned_summary,
        },
    )
    return {"ok": True, "feedback_id": feedback_id, "task_public_id": task_public_id}
