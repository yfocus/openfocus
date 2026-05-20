# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from ...db import session_scope
from ...models import AgentSpace, Event, Goal, Task, TaskAgentActivity
from ..events import service as event_service
from ..memory import service as memory_service

HOOK_ACTIVE_TASK_STATES = {
    "running",
    "waiting",
    "review_ready",
    "failed",
    "stale",
    "canceled",
}


def _query_param(query_params: Mapping[str, Any] | None, key: str) -> str:
    if query_params is None:
        return ""
    value = query_params.get(key, "")
    return str(value or "")


def _truncate_zh(text: str, n: int = 20) -> str:
    s = (text or "").strip()
    if len(s) <= n:
        return s
    return s[:n].rstrip() + "…"


def _human_duration_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {s}s" if s else f"{minutes}m"
    hours, m = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {m}m" if m else f"{hours}h"
    days, h = divmod(hours, 24)
    return f"{days}d {h}h" if h else f"{days}d"


def _human_since(ts: dt.datetime | None, *, now: dt.datetime | None = None) -> str:
    if ts is None:
        return "-"
    now = now or memory_service.utcnow()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return _human_duration_seconds(int((now - ts).total_seconds()))


def load_dashboard_context(
    query_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the template context for the Goals Dashboard.

    The route owns HTTP and template rendering; this read model owns the
    cross-domain projection needed by the Dashboard surface.
    """

    with session_scope() as s:
        goal_filter = (_query_param(query_params, "gfilter") or "ALL").strip().upper()
        goal_sort = (_query_param(query_params, "gsort") or "DDL").strip().upper()

        goals_all = s.query(Goal).order_by(Goal.id.desc()).all()
        today = dt.date.today()

        goal_ids = [g.id for g in goals_all]
        tasks = []
        if goal_ids:
            tasks = (
                s.query(Task)
                .filter(Task.goal_id.in_(goal_ids))
                .order_by(Task.id.asc())
                .all()
            )

        tasks_by_goal: dict[int, list[Task]] = {}
        for t in tasks:
            tasks_by_goal.setdefault(t.goal_id, []).append(t)

        public_ids = [t.public_id for t in tasks]
        agent_spaces_by_task: dict[str, AgentSpace] = {}
        if public_ids:
            spaces = (
                s.query(AgentSpace)
                .filter(AgentSpace.task_public_id.in_(public_ids))
                .all()
            )
            for sp in spaces:
                agent_spaces_by_task[sp.task_public_id] = sp

        activity_by_task: dict[str, TaskAgentActivity] = {}
        if public_ids:
            activities = (
                s.query(TaskAgentActivity)
                .filter(TaskAgentActivity.task_public_id.in_(public_ids))
                .order_by(
                    TaskAgentActivity.updated_at.desc(),
                    TaskAgentActivity.id.desc(),
                )
                .all()
            )
            for activity in activities:
                if (
                    activity.task_public_id
                    and activity.task_public_id not in activity_by_task
                ):
                    activity_by_task[activity.task_public_id] = activity

        task_events: dict[str, list[dict[str, Any]]] = {pid: [] for pid in public_ids}
        task_goal_by_pid: dict[str, int] = {t.public_id: t.goal_id for t in tasks}
        goal_events: dict[int, list[dict[str, Any]]] = {g.id: [] for g in goals_all}
        if public_ids:
            per_task_limit = 12
            evs = (
                s.query(Event)
                .filter(Event.task_id.in_(public_ids))
                .order_by(Event.id.desc())
                .all()
            )
            for ev in evs:
                pid = ev.task_id
                if not pid or pid not in task_events:
                    continue
                if len(task_events[pid]) >= per_task_limit:
                    continue
                task_events[pid].append(
                    {
                        "id": ev.id,
                        "kind": ev.kind,
                        "kind_label": event_service.event_kind_label(
                            ev.kind, ev.payload or {}
                        ),
                        "source_label": event_service.event_source_label(ev.agent),
                        "created_at": ev.created_at,
                        "summary": event_service.event_summary(
                            ev.kind, ev.payload or {}
                        ),
                    }
                )

        for pid, evs in task_events.items():
            gid = task_goal_by_pid.get(pid)
            if gid is None or gid not in goal_events:
                continue
            for it in evs:
                goal_events[gid].append({**it, "task_public_id": pid})

        goal_done_at: dict[int, dt.datetime] = {}
        goal_level_evs = (
            s.query(Event)
            .filter(Event.kind.like("goal.%"))
            .order_by(Event.id.desc())
            .limit(200)
            .all()
        )
        for ev in goal_level_evs:
            payload = ev.payload or {}
            try:
                gid = int((payload or {}).get("goal_id") or 0)
            except Exception:
                gid = 0
            if not gid or gid not in goal_events:
                continue

            if ev.kind == "goal.confirmed_done_by_user":
                prev = goal_done_at.get(gid)
                if prev is None or (
                    hasattr(ev.created_at, "timestamp")
                    and hasattr(prev, "timestamp")
                    and ev.created_at > prev
                ):
                    goal_done_at[gid] = ev.created_at

            goal_events[gid].append(
                {
                    "id": ev.id,
                    "kind": ev.kind,
                    "kind_label": event_service.event_kind_label(ev.kind, payload),
                    "source_label": event_service.event_source_label(ev.agent),
                    "created_at": ev.created_at,
                    "summary": event_service.event_summary(ev.kind, payload),
                    "task_public_id": None,
                }
            )
        for gid, evs in goal_events.items():
            evs.sort(
                key=lambda x: x.get("created_at") or memory_service.utcnow(),
                reverse=True,
            )
            goal_events[gid] = evs[:30]

        task_meta: dict[str, dict[str, Any]] = {}
        now = memory_service.utcnow()
        for t in tasks:
            activity = activity_by_task.get(t.public_id)
            last_at = None
            activity_state = ""
            if activity is not None:
                activity_state = str(activity.state or "").strip().lower()
                last_at = activity.last_activity_at or activity.state_started_at

            ui_status = "todo"
            if t.status == "done":
                ui_status = "done"
            elif activity_state in HOOK_ACTIVE_TASK_STATES:
                ui_status = "in_progress"

            task_meta[t.public_id] = {
                "ui_status": ui_status,
                "percent": (100 if t.status == "done" else None),
                "last_event_at": last_at,
                "elapsed": _human_since(last_at or t.created_at, now=now),
            }

        def _task_sort_key(t: Task) -> tuple[int, float, int]:
            meta = task_meta.get(t.public_id, {}) or {}
            ui_status = (
                str(meta.get("ui_status") or getattr(t, "status", "") or "todo")
                .strip()
                .lower()
            )
            status_rank = {
                "in_progress": 0,
                "todo": 1,
                "blocked": 2,
                "done": 9,
            }.get(ui_status, 3)
            created_at = getattr(t, "created_at", None) or memory_service.utcnow()
            created_ts = (
                created_at.timestamp() if hasattr(created_at, "timestamp") else 0
            )
            return (status_rank, -created_ts, -int(getattr(t, "id", 0) or 0))

        for grouped_tasks in tasks_by_goal.values():
            grouped_tasks.sort(key=_task_sort_key)

        def _goal_group(g: Goal) -> int:
            if (g.status or "").strip() == "done":
                return 2
            if getattr(g, "due_date", None) and g.due_date < today:
                return 1
            return 0

        def _accept_goal(g: Goal) -> bool:
            x = goal_filter
            if x == "ALL":
                return True
            grp = _goal_group(g)
            if x in {"IN_PROGRESS", "INPROGRESS", "IN-PROGRESS"}:
                return grp == 0
            if x == "EXPIRED":
                return grp == 1
            if x == "COMPLETED":
                return grp == 2
            return True

        def _sort_key(g: Goal) -> tuple[Any, ...]:
            grp = _goal_group(g)
            created_at = getattr(g, "created_at", None) or memory_service.utcnow()
            done_at = goal_done_at.get(int(g.id)) if grp == 2 else None

            if goal_sort in {"CREATED", "CREATED_AT", "CREATED_EVENT"}:
                return (
                    grp,
                    -(
                        created_at.timestamp()
                        if hasattr(created_at, "timestamp")
                        else 0
                    ),
                    -int(g.id),
                )
            if goal_sort in {"COMPLETED", "COMPLETED_AT", "DONE", "DONE_AT"}:
                ts_done = (
                    done_at.timestamp()
                    if (done_at and hasattr(done_at, "timestamp"))
                    else -1
                )
                ts_created = (
                    created_at.timestamp() if hasattr(created_at, "timestamp") else 0
                )
                return (grp, -ts_done if grp == 2 else -ts_created, -int(g.id))

            due = getattr(g, "due_date", None) or today
            ts_created = (
                created_at.timestamp() if hasattr(created_at, "timestamp") else 0
            )
            return (
                grp,
                int(due.toordinal()) if hasattr(due, "toordinal") else 0,
                -ts_created,
                -int(g.id),
            )

        goals = [g for g in goals_all if _accept_goal(g)]
        goals.sort(key=_sort_key)

        goal_display: dict[int, str] = {
            g.id: _truncate_zh(str(g.title or "").strip(), 20) for g in goals
        }

        task_display: dict[str, str] = {
            t.public_id: _truncate_zh(str(t.title or "").strip(), 20) for t in tasks
        }

        sel_goal_id = _query_param(query_params, "goal")
        sel_task_pid = _query_param(query_params, "task")
        selected_goal = None
        selected_task = None
        if sel_goal_id:
            try:
                selected_goal = s.get(Goal, int(sel_goal_id))
            except Exception:
                selected_goal = None
        if sel_task_pid:
            selected_task = (
                s.query(Task).filter(Task.public_id == sel_task_pid).one_or_none()
            )

        last_start_agent_command = ""
        last_agent_space = (
            s.query(AgentSpace)
            .filter(AgentSpace.start_agent_command != "")
            .order_by(AgentSpace.id.desc())
            .first()
        )
        if last_agent_space is not None:
            last_start_agent_command = str(
                getattr(last_agent_space, "start_agent_command", "") or ""
            )

    default_due = dt.date.today() + dt.timedelta(days=1)
    return {
        "goals": goals,
        "tasks_by_goal": tasks_by_goal,
        "agent_spaces_by_task": agent_spaces_by_task,
        "task_meta": task_meta,
        "goal_display": goal_display,
        "task_display": task_display,
        "task_events": task_events,
        "goal_events": goal_events,
        "now": memory_service.utcnow(),
        "today": today,
        "selected_goal": selected_goal,
        "selected_task": selected_task,
        "default_due": default_due.isoformat(),
        "goal_filter": goal_filter,
        "goal_sort": goal_sort,
        "last_start_agent_command": last_start_agent_command,
    }
