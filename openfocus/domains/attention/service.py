# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ...models import AgentSpace, AttentionItem, Event, Goal, Task
from ..events.service import event_kind_label, event_source_label, event_summary

ACTIVE_STATUS = "active"
DISMISSED_STATUS = "dismissed"
ACTED_STATUS = "acted"

SUCCESS_STATUSES = {"succeeded", "success", "ok", "done", "completed"}
FAILURE_STATUSES = {"failed", "fail", "error", "timeout", "denied", "panic"}
BLOCKED_STATUSES = {"blocked", "waiting", "waiting_on_someone"}
RUNNING_STATUSES = {"running", "in_progress", "progress"}
TERMINAL_ITEM_TYPES = {"completion_reported", "failed", "blocked"}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _normalize_status(value: object) -> str:
    return str(value or "").strip().lower()


def _is_completion(kind: str, payload: dict[str, Any]) -> bool:
    status = _normalize_status(payload.get("status"))
    if kind == "task.completed":
        return True
    return kind == "skill.focus_report" and status in SUCCESS_STATUSES


def _is_failure(_kind: str, payload: dict[str, Any]) -> bool:
    status = _normalize_status(payload.get("status"))
    if status in FAILURE_STATUSES:
        return True
    text = " ".join(
        str(payload.get(k) or "") for k in ("message", "error", "reason", "summary")
    ).lower()
    return any(token in text for token in FAILURE_STATUSES)


def _is_blocked(_kind: str, payload: dict[str, Any]) -> bool:
    status = _normalize_status(payload.get("status"))
    if status in BLOCKED_STATUSES:
        return True
    text = " ".join(
        str(payload.get(k) or "") for k in ("message", "reason", "summary")
    ).lower()
    return "blocked" in text or "waiting on" in text


def _is_running(kind: str, payload: dict[str, Any]) -> bool:
    status = _normalize_status(payload.get("status"))
    if status in RUNNING_STATUSES:
        return True
    return kind in {"task.started", "agent.started"}


def _next_move_item_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else None
    if result is None:
        return None
    items = result.get("items") if isinstance(result.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        task_public_id = str(target.get("task_public_id") or "").strip()
        if task_public_id:
            return item
    return None


def _is_next_move(kind: str, payload: dict[str, Any]) -> bool:
    return (
        kind == "agent.completed" and _next_move_item_from_payload(payload) is not None
    )


def _task_public_id_for_event(event: Event) -> str:
    task_public_id = str(event.task_id or "").strip()
    if task_public_id:
        return task_public_id
    payload = event.payload if isinstance(event.payload, dict) else {}
    task_public_id = str(payload.get("task_public_id") or "").strip()
    if task_public_id:
        return task_public_id
    next_move_item = _next_move_item_from_payload(payload)
    if next_move_item is not None:
        target = (
            next_move_item.get("target")
            if isinstance(next_move_item.get("target"), dict)
            else {}
        )
        return str(target.get("task_public_id") or "").strip()
    return ""


def _attention_title(kind: str, payload: dict[str, Any], task: Task) -> str:
    if _is_next_move(kind, payload):
        next_move_item = _next_move_item_from_payload(payload) or {}
        return str(next_move_item.get("title") or task.title or "Next Move ready")
    return str(
        task.title or payload.get("task_name") or event_kind_label(kind, payload)
    )


def _attention_summary(kind: str, payload: dict[str, Any], event: Event) -> str:
    if _is_next_move(kind, payload):
        next_move_item = _next_move_item_from_payload(payload) or {}
        why = (
            next_move_item.get("why")
            if isinstance(next_move_item.get("why"), list)
            else []
        )
        reason = str(next_move_item.get("reason") or "").strip()
        summary = "Next Move recommendation is ready."
        first_why = str(why[0] or "").strip() if why else ""
        if first_why:
            summary = first_why
        elif reason:
            summary = reason
        source = event_source_label(event.agent)
        return f"{summary} · {source}" if source else summary
    summary = event_summary(kind, payload)
    source = event_source_label(event.agent)
    return f"{summary} · {source}" if source and summary else (source or summary)


def _task_for_event(s: Session, event: Event) -> Task | None:
    task_public_id = _task_public_id_for_event(event)
    if not task_public_id:
        return None
    return s.query(Task).filter(Task.public_id == task_public_id).one_or_none()


def _apply_state_transition(s: Session, *, task_public_id: str, new_type: str) -> None:
    """Keep only the current agent-reported execution state per task.

    Running starts a new state and clears stale terminal reports. Terminal reports
    (completed/failed/blocked) clear running. Next Move is orthogonal and remains
    visible independently.
    """

    task_public_id = str(task_public_id or "").strip()
    if not task_public_id:
        return
    if new_type == "running":
        stale_types = TERMINAL_ITEM_TYPES
    elif new_type in TERMINAL_ITEM_TYPES:
        stale_types = {"running"}
    else:
        return
    now = _utcnow()
    rows = (
        s.query(AttentionItem)
        .filter(AttentionItem.task_public_id == task_public_id)
        .filter(AttentionItem.status == ACTIVE_STATUS)
        .filter(AttentionItem.item_type.in_(stale_types))
        .all()
    )
    for row in rows:
        row.status = ACTED_STATUS
        row.acted_at = now


def maybe_create_from_event(s: Session, event: Event) -> AttentionItem | None:
    """Create a persistent attention item for high-value events only."""

    if not event.id:
        s.flush()
    existing = (
        s.query(AttentionItem)
        .filter(AttentionItem.source_event_id == int(event.id or 0))
        .one_or_none()
    )
    if existing is not None:
        return existing

    payload = event.payload if isinstance(event.payload, dict) else {}
    kind = str(event.kind or "")
    if _is_completion(kind, payload):
        item_type = "completion_reported"
        severity = "success"
    elif _is_next_move(kind, payload):
        item_type = "next_move"
        severity = "info"
    elif _is_failure(kind, payload):
        item_type = "failed"
        severity = "error"
    elif _is_blocked(kind, payload):
        item_type = "blocked"
        severity = "warning"
    elif _is_running(kind, payload):
        item_type = "running"
        severity = "info"
    else:
        return None

    task = _task_for_event(s, event)
    if task is None:
        return None

    # Avoid training the user to dismiss duplicate cards when the same task emits
    # multiple success/failure reports. Keep the latest source and summary visible.
    duplicate = (
        s.query(AttentionItem)
        .filter(AttentionItem.task_public_id == task.public_id)
        .filter(AttentionItem.item_type == item_type)
        .filter(AttentionItem.status == ACTIVE_STATUS)
        .order_by(AttentionItem.id.desc())
        .first()
    )
    goal = s.query(Goal).filter(Goal.id == int(task.goal_id)).one_or_none()
    title = _attention_title(kind, payload, task)
    summary = _attention_summary(kind, payload, event)
    _apply_state_transition(
        s, task_public_id=str(task.public_id or ""), new_type=item_type
    )
    if duplicate is not None:
        duplicate.source_event_id = int(event.id or 0)
        duplicate.goal_id = int(goal.id) if goal is not None else int(task.goal_id)
        duplicate.severity = severity
        duplicate.title = str(title or "")[:512]
        duplicate.summary = str(summary or "")[:2000]
        duplicate.payload = {
            "event_kind": kind,
            "event_payload": payload,
            "task_title": task.title,
            "goal_title": goal.title if goal is not None else "",
            "last_event_at": _ts(event.created_at or _utcnow()),
        }
        s.flush()
        return duplicate

    item = AttentionItem(
        source_event_id=int(event.id or 0),
        task_public_id=task.public_id,
        goal_id=int(goal.id) if goal is not None else int(task.goal_id),
        item_type=item_type,
        severity=severity,
        title=str(title or "")[:512],
        summary=str(summary or "")[:2000],
        status=ACTIVE_STATUS,
        payload={
            "event_kind": kind,
            "event_payload": payload,
            "task_title": task.title,
            "goal_title": goal.title if goal is not None else "",
            "last_event_at": _ts(event.created_at or _utcnow()),
        },
        created_at=event.created_at or _utcnow(),
    )
    s.add(item)
    s.flush()
    return item


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


def _age_seconds(value: object, *, now: dt.datetime | None = None) -> int | None:
    if not isinstance(value, dt.datetime):
        return None
    current = now or _utcnow()
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return max(0, int((current - value).total_seconds()))


def _bucket_for_item_type(item_type: str) -> str:
    if item_type == "running":
        return "running"
    if item_type == "next_move":
        return "next_move"
    return "completed"


def action_for_task(task_public_id: str, has_space: bool) -> dict[str, Any]:
    if has_space:
        return {
            "primary_target": "agent_space",
            "primary_label": "Go to AgentSpace",
            "primary_url": f"/tasks/{task_public_id}/agent_space",
            "fallback_label": "Go to Task",
            "fallback_url": f"/goals?task={task_public_id}",
        }
    return {
        "primary_target": "task",
        "primary_label": "Go to Task",
        "primary_url": f"/goals?task={task_public_id}",
        "fallback_label": None,
        "fallback_url": None,
    }


def action_for_goal(goal_id: int | str) -> dict[str, Any]:
    return {
        "primary_target": "goal",
        "primary_label": "Open Goal",
        "primary_url": f"/goals?goal={goal_id}",
        "fallback_label": None,
        "fallback_url": None,
    }


def _serialize_item(
    item: AttentionItem,
    *,
    task: Task | None,
    goal: Goal | None,
    has_space: bool,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    task_public_id = str(item.task_public_id or (task.public_id if task else ""))
    action = action_for_task(task_public_id, has_space) if task_public_id else {}
    payload = item.payload if isinstance(item.payload, dict) else {}
    return {
        "id": int(item.id or 0),
        "source_event_id": int(item.source_event_id or 0),
        "type": item.item_type,
        "bucket": _bucket_for_item_type(str(item.item_type or "")),
        "severity": item.severity,
        "status": item.status,
        "title": item.title or (task.title if task else ""),
        "summary": item.summary or "",
        "task_public_id": task_public_id,
        "task_title": task.title if task else "",
        "goal_id": int(goal.id) if goal else item.goal_id,
        "goal_title": goal.title if goal else "",
        "has_agent_space": bool(has_space),
        "action": action,
        "created_at": _ts(item.created_at),
        "state_since": _ts(item.created_at),
        "state_age_seconds": _age_seconds(item.created_at, now=now),
        "last_event_at": payload.get("last_event_at") if payload else None,
        "dismissed_at": _ts(item.dismissed_at),
        "acted_at": _ts(item.acted_at),
    }


def active_items_payload(s: Session, *, limit: int = 10) -> dict:
    limit = max(1, min(int(limit or 10), 50))
    base_query = s.query(AttentionItem).filter(AttentionItem.status == ACTIVE_STATUS)
    total = int(base_query.with_entities(func.count(AttentionItem.id)).scalar() or 0)
    items = (
        base_query.order_by(AttentionItem.created_at.desc(), AttentionItem.id.desc())
        .limit(limit)
        .all()
    )
    task_ids = [str(it.task_public_id or "") for it in items if it.task_public_id]
    tasks = (
        {
            t.public_id: t
            for t in s.query(Task).filter(Task.public_id.in_(task_ids)).all()
        }
        if task_ids
        else {}
    )
    goal_ids = {int(t.goal_id) for t in tasks.values() if t.goal_id}
    goal_ids.update(int(it.goal_id) for it in items if it.goal_id)
    goals = (
        {int(g.id): g for g in s.query(Goal).filter(Goal.id.in_(goal_ids)).all()}
        if goal_ids
        else {}
    )
    spaces = (
        {
            sp.task_public_id
            for sp in s.query(AgentSpace)
            .filter(AgentSpace.task_public_id.in_(task_ids))
            .all()
        }
        if task_ids
        else set()
    )
    serialized = []
    now = _utcnow()
    for item in items:
        task = tasks.get(str(item.task_public_id or ""))
        goal = (
            goals.get(int(task.goal_id))
            if task is not None
            else goals.get(int(item.goal_id or 0))
        )
        serialized.append(
            _serialize_item(
                item,
                task=task,
                goal=goal,
                has_space=str(item.task_public_id or "") in spaces,
                now=now,
            )
        )
    buckets = {
        "running": [x for x in serialized if x.get("bucket") == "running"],
        "completed": [x for x in serialized if x.get("bucket") == "completed"],
        "next_move": [x for x in serialized if x.get("bucket") == "next_move"],
    }
    return {"items": serialized, "count": total, "buckets": buckets}


def set_item_status(s: Session, item_id: int, status: str) -> dict:
    item = s.query(AttentionItem).filter(AttentionItem.id == int(item_id)).one_or_none()
    if item is None:
        raise LookupError("attention item not found")
    if item.status != ACTIVE_STATUS:
        return {"ok": True, "id": int(item.id or 0), "status": item.status}
    now = _utcnow()
    if status == DISMISSED_STATUS:
        item.status = DISMISSED_STATUS
        item.dismissed_at = now
    elif status == ACTED_STATUS:
        item.status = ACTED_STATUS
        item.acted_at = now
    else:
        raise ValueError("unsupported attention item status")
    s.flush()
    return {"ok": True, "id": int(item.id or 0), "status": item.status}


def mark_matching_task_items_acted(s: Session, task_public_id: str) -> None:
    task_public_id = str(task_public_id or "").strip()
    if not task_public_id:
        return
    now = _utcnow()
    rows = (
        s.query(AttentionItem)
        .filter(AttentionItem.task_public_id == task_public_id)
        .filter(AttentionItem.status == ACTIVE_STATUS)
        .filter(
            or_(
                AttentionItem.item_type == "completion_reported",
                AttentionItem.item_type == "failed",
                AttentionItem.item_type == "blocked",
                AttentionItem.item_type == "running",
            )
        )
        .all()
    )
    for item in rows:
        item.status = ACTED_STATUS
        item.acted_at = now
    if rows:
        s.flush()
