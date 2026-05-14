# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy.orm import Session

from ...models import Event, Goal, Task
from ..memory import service as memory_service
from .repository import EventRepository

NOISE_EVENT_KINDS = {"companion.connected", "companion.disconnected"}

AuditPayload = dict[str, Any] | bool | None


def _audit_value(audit: dict[str, Any], key: str, default: Any = None) -> Any:
    return audit[key] if key in audit else default


def _audit_metadata(audit: dict[str, Any]) -> dict[str, Any]:
    metadata = audit.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _ts(value: object) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def status_label(status: object) -> str:
    x = str(status or "").strip().lower()
    if not x:
        return ""
    if x in {"succeeded", "success", "ok", "done", "completed"}:
        return "Done"
    if x in {"failed", "fail", "error"}:
        return "Failed"
    if x in {"blocked", "waiting", "waiting_on_someone"}:
        return "Blocked"
    if x in {"running", "in_progress", "progress"}:
        return "In progress"
    return str(status).strip()


def event_source_label(agent: str | None) -> str:
    a = (agent or "").strip()
    if not a:
        return "Source: Unknown"
    if a.lower() in {"ui", "web", "webui"} or a.lower().endswith("/ui"):
        return "Source: Web"
    return f"Source: Agent ({a})"


def event_kind_label(kind: str, payload: object) -> str:
    if kind == "agent.started":
        return "Agent started"
    if kind == "agent.completed":
        return "Agent completed"
    if kind == "agent.fallback":
        return "Agent fallback"
    if kind == "agent.llm_call.started":
        return "LLM call started"
    if kind == "agent.llm_call.completed":
        return "LLM call completed"
    if kind == "agent.tool_calls.detected":
        return "Tool calls detected"
    if kind == "agent.tool_call.started":
        return "Tool call started"
    if kind == "agent.tool_call.completed":
        return "Tool call completed"
    if kind == "agent.tool_call.failed":
        return "Tool call failed"
    if kind == "skill.focus_report":
        return "Execution report"
    if kind == "task.completed":
        return "Completion reported"
    if kind == "task.failed":
        return "Failed"
    if kind == "task.blocked":
        return "Blocked"
    if kind == "task.progress":
        return "Progress reported"
    if kind == "task.started":
        return "Started"
    if kind == "task.reopened":
        return "Reopened"
    if kind == "task.confirmed_done":
        return "Confirmed done"
    if kind in {"next_move.not_for_now", "next_move.feedback"}:
        return "Next Move"
    if kind == "goal.confirmed_done_by_user":
        return "Goal confirmed done"
    if kind == "goal.reopened_by_user":
        return "Goal reopened"
    if kind in {
        "companion.pairing_code.requested",
        "companion.pair.attempted",
        "companion.paired",
    }:
        return "Companion pairing"
    if kind == "companion.disconnected":
        return "Companion connection"
    if kind == "companion.deleted":
        return "Companion management"
    return kind


def event_summary(kind: str, payload: object) -> str:
    if kind == "agent.completed" and isinstance(payload, dict):
        result = (
            payload.get("result") if isinstance(payload.get("result"), dict) else {}
        )
        items = result.get("items") if isinstance(result.get("items"), list) else []
        if items:
            return f"Generated {len(items)} recommendation(s)"
        created = (
            payload.get("created_tasks")
            if isinstance(payload.get("created_tasks"), list)
            else []
        )
        if created:
            return f"Created {len(created)} task(s)"
        return "Agent run completed"
    if kind == "agent.started":
        return "Agent run started"
    if kind == "agent.fallback":
        return "Agent used fallback"
    if kind == "agent.llm_call.started":
        return "LLM call started"
    if kind == "agent.llm_call.completed":
        return "LLM call completed"
    if kind == "agent.tool_call.failed":
        return "Tool call failed"
    if kind == "task.confirmed_done":
        return "Confirmed done by user"
    if kind == "goal.confirmed_done_by_user":
        return "Goal confirmed done by user"
    if kind == "goal.reopened_by_user":
        return "Goal reopened by user"
    if kind == "task.reopened":
        return "Task reopened"
    if kind == "next_move.not_for_now":
        return "Not for now"
    if kind == "next_move.feedback":
        return "Next Move feedback"

    if kind == "companion.pairing_code.requested":
        return "Pairing code requested"
    if kind == "companion.pair.attempted":
        return "Pairing code submitted"
    if kind == "companion.paired":
        return "Companion paired"
    if kind == "companion.disconnected":
        return "Companion disconnected"
    if kind == "companion.deleted":
        return "Companion deleted"

    if isinstance(payload, dict):
        msg = payload.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()

        if kind == "skill.focus_report":
            task_name = payload.get("task_name")
            label = status_label(payload.get("status"))
            if task_name and label:
                return f"{task_name} · {label} (pending confirmation)"
            if label:
                return f"{label} (pending confirmation)"

        if kind in {
            "task.progress",
            "task.started",
            "task.completed",
            "task.failed",
            "task.blocked",
        }:
            if kind == "task.started":
                return "Started"
            if kind == "task.completed":
                return "Completion reported (pending confirmation)"
            if kind == "task.failed":
                return "Failed"
            if kind == "task.blocked":
                return "Blocked"
            return "New progress (pending confirmation)"

        label = status_label(payload.get("status"))
        if label:
            return label

    return event_kind_label(kind, payload)


def record_event(
    s: Session,
    *,
    kind: str,
    agent: str,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
    create_attention: bool = True,
    audit: AuditPayload = None,
) -> Event:
    """Persist one OpenFocus event and run shared event-side effects.

    EventRepository remains the only DB write path for Event rows; this service
    owns product-level side effects such as Attention derivation and optional
    audit-memory mirroring.
    """

    event = Event(
        kind=str(kind or ""),
        agent=str(agent or ""),
        task_id=task_id,
        payload=payload or {},
    )
    EventRepository(s).add(event)
    if create_attention:
        from ..attention import service as attention_service

        attention_service.maybe_create_from_event(s, event)

    if audit:
        audit_payload: dict[str, Any]
        if isinstance(audit, dict):
            audit_payload = audit
        else:
            audit_payload = {}
        memory_service.try_audit_memory(
            kind=str(_audit_value(audit_payload, "kind", f"event.{event.kind}")),
            source=str(_audit_value(audit_payload, "source", f"agent:{event.agent}")),
            summary=str(
                _audit_value(
                    audit_payload,
                    "summary",
                    f"Event `{event.kind}` was recorded.",
                )
            ),
            detail=str(
                _audit_value(
                    audit_payload,
                    "detail",
                    json.dumps(event.payload or {}, ensure_ascii=False, indent=2),
                )
            ),
            goal_id=_audit_value(audit_payload, "goal_id"),
            task_public_id=_audit_value(audit_payload, "task_public_id", event.task_id),
            metadata={
                "event_id": int(event.id or 0),
                "event_kind": event.kind,
                "created_at": memory_service.iso(event.created_at),
                **_audit_metadata(audit_payload),
            },
            occurred_at=_audit_value(audit_payload, "occurred_at", event.created_at),
        )
    return event


def report_agent_event(
    s: Session,
    *,
    kind: str,
    agent: str,
    task_id: str | None,
    payload: dict[str, Any],
) -> dict:
    event = record_event(
        s,
        kind=kind,
        agent=agent,
        task_id=task_id,
        payload=payload,
        audit={
            "kind": f"event.{kind}",
            "source": f"agent:{agent}",
            "summary": f"Agent reported event `{kind}`.",
            "detail": json.dumps(payload or {}, ensure_ascii=False, indent=2),
            "task_public_id": task_id,
        },
    )
    return {"id": int(event.id or 0), "created_at": event.created_at}


def report_focus_result(s: Session, report: Any) -> dict:
    payload = {
        "task_name": report.task_name,
        "status": report.status,
        "goal_id": report.goal_id,
        "task_public_id": report.task_public_id,
        "user_prompt": report.user_prompt,
        "assistant_response": report.assistant_response,
        "metadata": report.metadata,
    }
    record_event(
        s,
        kind="skill.focus_report",
        agent=report.agent,
        task_id=report.task_public_id,
        payload=payload,
        audit={
            "kind": "skill.focus_report",
            "source": f"agent:{report.agent}",
            "summary": f"Focus report for task `{report.task_name}` with status `{report.status}`.",
            "detail": json.dumps(payload, ensure_ascii=False, indent=2),
            "goal_id": report.goal_id,
            "task_public_id": report.task_public_id,
            "metadata": {"status": report.status},
        },
    )
    return {"ok": True, "task_updated": None}


def recent_events_payload(s: Session, *, limit: int = 30) -> dict:
    limit = max(1, min(int(limit or 30), 200))
    events = [
        ev
        for ev in EventRepository(s).list_recent(limit=limit * 3)
        if (ev.kind or "") not in NOISE_EVENT_KINDS
    ][:limit]

    candidate_task_ids = [ev.task_id for ev in events if ev.task_id]
    existing_task_ids: set[str] = set()
    if candidate_task_ids:
        existing_task_ids = {
            row[0]
            for row in s.query(Task.public_id)
            .filter(Task.public_id.in_(candidate_task_ids))
            .all()
        }

    items: list[dict] = []
    for ev in events:
        payload = ev.payload or {}
        task_public_id = (
            ev.task_id if (ev.task_id and ev.task_id in existing_task_ids) else None
        )
        items.append(
            {
                "id": ev.id,
                "kind": ev.kind,
                "kind_label": event_kind_label(ev.kind, payload),
                "source_label": event_source_label(ev.agent),
                "task_id": ev.task_id,
                "task_public_id": task_public_id,
                "created_at": _ts(ev.created_at),
                "summary": event_summary(ev.kind, payload),
            }
        )
    return {"items": items}


def calendar_month_payload(s: Session, *, ym: str | None = None) -> dict:
    today = dt.date.today()
    raw = str(ym or "").strip()
    if raw:
        parts = raw.split("-")
        if len(parts) != 2:
            raise ValueError("ym must be YYYY-MM")
        year = int(parts[0])
        month = int(parts[1])
    else:
        year, month = int(today.year), int(today.month)
    if not (1 <= month <= 12):
        raise ValueError("month out of range")
    if year < 1970 or year > 2100:
        raise ValueError("year out of range")

    month_start = dt.date(year, month, 1)
    month_end = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    start_dt = dt.datetime(
        month_start.year, month_start.month, month_start.day, tzinfo=dt.timezone.utc
    )
    end_dt = dt.datetime(
        month_end.year, month_end.month, month_end.day, tzinfo=dt.timezone.utc
    )

    done_tasks = (
        s.query(Task)
        .filter(Task.completed_at.isnot(None))
        .filter(Task.completed_at >= start_dt)
        .filter(Task.completed_at < end_dt)
        .all()
    )
    goals = s.query(Goal).order_by(Goal.id.asc()).all()
    all_tasks = s.query(Task).order_by(Task.id.asc()).all()

    goal_by_id = {int(g.id): g for g in goals}
    tasks_by_goal: dict[int, list[Task]] = {}
    for task in all_tasks:
        tasks_by_goal.setdefault(int(task.goal_id), []).append(task)

    days: dict[str, list[dict]] = {}
    for task in done_tasks:
        if not task.completed_at:
            continue
        day = task.completed_at.astimezone(dt.timezone.utc).date().isoformat()
        goal = goal_by_id.get(int(task.goal_id))
        days.setdefault(day, []).append(
            {
                "task_public_id": task.public_id,
                "task_title": task.title,
                "goal_id": int(task.goal_id),
                "goal_title": (goal.title if goal is not None else ""),
                "completed_at": _ts(task.completed_at),
            }
        )

    goals_out: list[dict] = []
    for goal in goals:
        goal_id = int(goal.id)
        tasks = tasks_by_goal.get(goal_id, [])
        done_count = sum(1 for task in tasks if (task.status or "").strip() == "done")
        goals_out.append(
            {
                "id": goal_id,
                "title": goal.title,
                "status": goal.status,
                "created_at": _ts(goal.created_at),
                "due_date": _ts(goal.due_date),
                "total_tasks": len(tasks),
                "done_tasks": done_count,
                "tasks": [
                    {
                        "id": int(task.id),
                        "public_id": task.public_id,
                        "title": task.title,
                        "status": task.status,
                        "completed_at": _ts(task.completed_at)
                        if task.completed_at
                        else None,
                    }
                    for task in tasks
                ],
            }
        )

    return {
        "ok": True,
        "ym": f"{year:04d}-{month:02d}",
        "month_start": month_start.isoformat(),
        "month_end": month_end.isoformat(),
        "days": days,
        "goals": goals_out,
    }
