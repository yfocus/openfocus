# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...db import session_scope
from ...models import AgentSpace, Event, Goal, NextMoveFeedback, Task
from ..goals.classification import (
    infer_context_key,
    infer_estimated_minutes,
    infer_task_type,
)
from ..memory import service as memory_service

TASK_TYPE_LABELS = {
    "deep_work": "Deep Work",
    "communication": "Communication",
    "review": "Review",
    "execution": "Execution",
    "admin": "Admin",
}

OPEN_GOAL_STATUSES_EXCLUDED = frozenset({"done", "archived", "paused", "canceled"})
EXECUTABLE_TASK_STATUSES = frozenset({"todo", "in_progress", "blocked"})


def task_type_label(task_type: str | None) -> str:
    return TASK_TYPE_LABELS.get(str(task_type or "").strip().lower(), "Execution")


def iso(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[no-any-return]
    return str(value)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def memory_daily_files() -> list[dict[str, Any]]:
    root = memory_service.daily_root().resolve()
    files = sorted(root.glob("*.md"), reverse=True)
    out: list[dict[str, Any]] = []
    for p in files:
        try:
            stat = p.stat()
            rel = (
                p.resolve()
                .relative_to(memory_service.memory_dir().resolve())
                .as_posix()
            )
            out.append(
                {
                    "rel_path": rel,
                    "name": p.name,
                    "bytes": stat.st_size,
                    "modified_at": dt.datetime.fromtimestamp(
                        stat.st_mtime, tz=dt.timezone.utc
                    ).isoformat(),
                }
            )
        except Exception:
            continue
    return out


def serialize_event(ev: Event) -> dict[str, Any]:
    return {
        "id": int(ev.id),
        "kind": ev.kind,
        "agent": ev.agent,
        "task_id": ev.task_id,
        "payload": ev.payload or {},
        "created_at": iso(ev.created_at),
    }


@dataclass(frozen=True)
class RecommendationContextBuilder:
    """Build the JSON context snapshot used by Next Move recommendation agents."""

    goal_id: int | None = None

    def build(self) -> dict[str, Any]:
        now = dt.datetime.now(dt.timezone.utc)
        week_ago = now - dt.timedelta(days=7)
        with session_scope() as s:
            open_goals = (
                s.query(Goal)
                .filter(Goal.status.notin_(OPEN_GOAL_STATUSES_EXCLUDED))
                .order_by(Goal.due_date.asc(), Goal.id.asc())
                .all()
            )
            if self.goal_id is not None:
                open_goals = [g for g in open_goals if int(g.id) == int(self.goal_id)]
            open_goal_ids = [int(g.id) for g in open_goals]

            open_tasks = []
            if open_goal_ids:
                open_tasks = (
                    s.query(Task)
                    .filter(Task.goal_id.in_(open_goal_ids))
                    .filter(Task.status.in_(EXECUTABLE_TASK_STATUSES))
                    .order_by(Task.id.asc())
                    .all()
                )

            task_ids = [t.public_id for t in open_tasks]
            spaces_by_task = {}
            if task_ids:
                for space in (
                    s.query(AgentSpace)
                    .filter(AgentSpace.task_public_id.in_(task_ids))
                    .all()
                ):
                    spaces_by_task[space.task_public_id] = space

            completed_goal_events = (
                s.query(Event)
                .filter(Event.kind == "goal.confirmed_done_by_user")
                .filter(Event.created_at >= week_ago)
                .order_by(Event.id.desc())
                .all()
            )
            completed_goal_ids: list[int] = []
            for ev in completed_goal_events:
                payload = ev.payload or {}
                try:
                    gid = int(payload.get("goal_id") or 0)
                except Exception:
                    gid = 0
                if gid and gid not in completed_goal_ids:
                    completed_goal_ids.append(gid)
            completed_goals = (
                s.query(Goal)
                .filter(Goal.id.in_(completed_goal_ids))
                .order_by(Goal.id.desc())
                .all()
                if completed_goal_ids
                else []
            )
            completed_tasks = (
                s.query(Task)
                .filter(Task.status == "done")
                .filter(Task.completed_at.isnot(None))
                .filter(Task.completed_at >= week_ago)
                .order_by(Task.completed_at.desc())
                .all()
            )
            recent_events = s.query(Event).order_by(Event.id.desc()).limit(100).all()
            feedback_rows = (
                s.query(NextMoveFeedback)
                .order_by(NextMoveFeedback.id.desc())
                .limit(120)
                .all()
            )

        open_goals_payload: list[dict[str, Any]] = []
        goal_by_id: dict[int, Goal] = {
            int(g.id): g for g in [*open_goals, *completed_goals]
        }
        tasks_by_goal: dict[int, list[dict[str, Any]]] = {}
        for t in open_tasks:
            space = spaces_by_task.get(t.public_id)
            root_path = getattr(space, "root_path", "") if space is not None else ""
            task_type = str(
                getattr(t, "task_type", "") or ""
            ).strip().lower() or infer_task_type(t.title, t.content)
            estimated_minutes = int(
                getattr(t, "estimated_minutes", 0) or 0
            ) or infer_estimated_minutes(task_type, t.title, t.content)
            context_key = str(getattr(t, "context_key", "") or "").strip()
            if not context_key:
                context_key = infer_context_key(
                    t.title,
                    t.content,
                    goal_id=int(t.goal_id),
                    root_path=root_path,
                )
            tasks_by_goal.setdefault(int(t.goal_id), []).append(
                {
                    "id": int(t.id),
                    "public_id": t.public_id,
                    "title": t.title,
                    "content": t.content,
                    "status": t.status,
                    "task_type": task_type,
                    "task_type_label": task_type_label(task_type),
                    "estimated_minutes": estimated_minutes,
                    "context_key": context_key,
                    "agent_space_root_path": root_path,
                    "created_at": iso(t.created_at),
                }
            )

        for g in open_goals:
            open_goals_payload.append(
                {
                    "id": int(g.id),
                    "title": g.title,
                    "content": g.content,
                    "status": g.status,
                    "priority": g.priority,
                    "importance": g.importance,
                    "due_date": iso(g.due_date),
                    "created_at": iso(g.created_at),
                    "tasks": tasks_by_goal.get(int(g.id), []),
                }
            )

        return {
            "now": now.isoformat(),
            "long_memory_full_content": read_text(memory_service.long_term_path()),
            "daily_memory_access": {
                "method": "Use tools list_daily_memory_files(limit) and read_daily_memory_file(rel_path).",
                "available_files_preview": memory_daily_files()[:14],
            },
            "event_access": {
                "recent_events_included": 100,
                "method": "Use tool list_recent_events(offset, limit) to read more events beyond the latest 100.",
            },
            "recent_events_latest_100": [serialize_event(ev) for ev in recent_events],
            "open_goals_and_tasks": open_goals_payload,
            "recent_not_for_now_task_public_ids": self._recent_not_for_now_ids(
                feedback_rows, now=now
            ),
            "completed_last_7_days": {
                "goals": [
                    {
                        "id": int(g.id),
                        "title": g.title,
                        "content": g.content,
                        "priority": g.priority,
                        "importance": g.importance,
                        "due_date": iso(g.due_date),
                        "created_at": iso(g.created_at),
                    }
                    for g in completed_goals
                ],
                "tasks": [
                    {
                        "id": int(t.id),
                        "public_id": t.public_id,
                        "goal_id": int(t.goal_id),
                        "goal_title": getattr(
                            goal_by_id.get(int(t.goal_id)), "title", ""
                        ),
                        "title": t.title,
                        "content": t.content,
                        "completed_at": iso(t.completed_at),
                    }
                    for t in completed_tasks
                ],
            },
            "recent_next_move_feedback": [
                {
                    "id": int(fb.id),
                    "run_id": fb.run_id,
                    "task_public_id": fb.task_public_id,
                    "feedback_type": fb.feedback_type,
                    "reason_code": fb.reason_code,
                    "reason_text": fb.reason_text,
                    "learned_summary": fb.learned_summary,
                    "created_at": iso(fb.created_at),
                }
                for fb in feedback_rows
            ],
        }

    def _recent_not_for_now_ids(
        self, feedback_rows: list[NextMoveFeedback], *, now: dt.datetime
    ) -> list[str]:
        out: list[str] = []
        for fb in feedback_rows:
            created_at = getattr(fb, "created_at", None) or now
            if getattr(created_at, "tzinfo", None) is None:
                created_at = created_at.replace(tzinfo=dt.timezone.utc)
            if (now - created_at.astimezone(dt.timezone.utc)) > dt.timedelta(hours=24):
                continue
            if str(getattr(fb, "feedback_type", "") or "").strip() != "dismiss":
                continue
            pid = str(getattr(fb, "task_public_id", "") or "").strip()
            if pid and pid not in out:
                out.append(pid)
        return out
