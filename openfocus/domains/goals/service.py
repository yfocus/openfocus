# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ...models import AgentMessage, AgentSession, AgentSpace, Event, Goal, Task
from ..memory import service as memory_service


class GoalTaskNotFound(LookupError):
    """Raised when a requested Goal or Task does not exist."""


@dataclass(frozen=True)
class GoalResult:
    goal_id: int
    title: str


@dataclass(frozen=True)
class TaskResult:
    task_id: int
    task_public_id: str
    goal_id: int
    title: str


def infer_task_type(title: str, description: str) -> str:
    text = f"{title}\n{description}".lower()
    if any(
        k in text
        for k in [
            "review",
            "approve",
            "comment",
            "code review",
            "qa",
            "test report",
            "验收",
            "评审",
            "reviewer",
            " pr",
            " mr",
        ]
    ):
        return "review"
    if any(
        k in text
        for k in [
            "sync",
            "meeting",
            "reply",
            "email",
            "message",
            "call",
            "沟通",
            "对齐",
            "联系",
            "回复",
            "会议",
        ]
    ):
        return "communication"
    if any(
        k in text
        for k in [
            "admin",
            "ops",
            "cleanup",
            "organize",
            "docs",
            "document",
            "整理",
            "记录",
            "文档",
            "行政",
        ]
    ):
        return "admin"
    if any(
        k in text
        for k in [
            "design",
            "investigate",
            "analysis",
            "analyze",
            "refactor",
            "architecture",
            "research",
            "规划",
            "设计",
            "排查",
            "分析",
            "重构",
        ]
    ):
        return "deep_work"
    return "execution"


def infer_estimated_minutes(task_type: str, title: str, description: str) -> int:
    text = f"{title}\n{description}".lower()
    m = re.search(
        r"(\d{1,3})\s*(minutes?|mins?|min|小时|小時|hour|hours|hr|hrs|h|分钟|分鐘)",
        text,
    )
    if m:
        try:
            num = max(5, min(240, int(m.group(1))))
            unit = m.group(2)
            if unit in {"小时", "小時", "hour", "hours", "hr", "hrs", "h"}:
                return min(240, num * 60)
            return num
        except Exception:
            pass
    if re.search(
        r"\b(quick|small|tiny|minor|trivial|fast|马上|快速|小改|顺手)\b", text
    ):
        return 20
    if task_type == "review":
        return 25
    if task_type == "communication":
        return 20
    if task_type == "admin":
        return 15
    if task_type == "deep_work":
        return 90
    return 45


def infer_context_key(
    title: str, description: str, *, goal_id: int, root_path: str | None = None
) -> str:
    rp = str(root_path or "").strip()
    if rp:
        try:
            from pathlib import Path

            name = Path(rp).name.strip().lower()
            if name:
                return f"space:{name[:80]}"
        except Exception:
            pass
    text = f"{title}\n{description}".lower()
    m = re.search(r"([a-z0-9_.-]+/[a-z0-9_.-]+)", text)
    if m:
        return f"topic:{m.group(1)[:80]}"
    tokens = [
        x for x in re.split(r"[^a-zA-Z0-9\u4e00-\u9fff]+", text) if len(x.strip()) >= 2
    ]
    seed = (tokens[0] if tokens else "")[:32].strip().lower()
    if seed:
        return f"goal:{goal_id}:{seed}"
    return f"goal:{goal_id}"


def _add_goal_created_event(s: Session, goal: Goal, *, agent: str = "ui") -> None:
    s.add(
        Event(
            kind="goal.created",
            agent=agent,
            task_id=None,
            payload={"goal_id": int(goal.id), "title": str(goal.title or "")},
        )
    )


def _add_task_created_event(s: Session, task: Task, *, agent: str = "ui") -> None:
    s.add(
        Event(
            kind="task.created",
            agent=agent,
            task_id=str(task.public_id or ""),
            payload={
                "goal_id": int(task.goal_id),
                "task_public_id": str(task.public_id or ""),
                "title": str(task.title or ""),
            },
        )
    )


def create_goal(
    s: Session,
    *,
    title: str,
    content: str,
    due_date: dt.date,
    agent: str = "ui",
    source: str = "web",
    priority: str = "normal",
    importance: str = "normal",
    status: str = "active",
    source_inspiration_space_id: int | None = None,
    source_inspiration_draft_id: int | None = None,
    audit: bool = True,
) -> Goal:
    title_text = str(title or "").strip()
    content_text = str(content or "").strip()
    goal = Goal(
        title=title_text,
        content=content_text,
        due_date=due_date,
        status=str(status or "active").strip() or "active",
        priority=str(priority or "normal").strip() or "normal",
        importance=str(importance or "normal").strip() or "normal",
        source_inspiration_space_id=source_inspiration_space_id,
        source_inspiration_draft_id=source_inspiration_draft_id,
    )
    s.add(goal)
    s.flush()
    _add_goal_created_event(s, goal, agent=agent)
    if audit:
        memory_service.try_audit_memory(
            kind="goal.created",
            source=source,
            summary=f"Created goal: {title_text}",
            detail=f"Goal title:\n\n{title_text}\n\nContent:\n\n{content_text}",
            goal_id=int(goal.id) or None,
            metadata={"due_date": due_date.isoformat()},
        )
    return goal


def update_goal(
    s: Session,
    *,
    goal_id: int,
    title: str,
    content: str,
    due_date: dt.date,
    status: str = "active",
    priority: str = "normal",
    importance: str = "normal",
) -> GoalResult:
    goal = s.get(Goal, int(goal_id))
    if goal is None:
        raise GoalTaskNotFound("Goal not found")
    old_title = str(goal.title or "")
    old_content = str(goal.content or "")
    title_text = str(title or "").strip()
    content_text = str(content or "").strip()
    status_text = str(status or "active").strip() or "active"
    priority_text = str(priority or "normal").strip() or "normal"
    importance_text = str(importance or "normal").strip() or "normal"

    goal.title = title_text
    goal.content = content_text
    goal.due_date = due_date
    goal.status = status_text
    goal.priority = priority_text
    goal.importance = importance_text
    memory_service.try_audit_memory(
        kind="goal.edited",
        source="web",
        summary=f"Edited goal: {title_text}",
        detail=(
            f"Previous title: {old_title}\n\n"
            f"Previous content:\n\n{old_content}\n\n"
            f"Updated title: {title_text}\n\n"
            f"Updated content:\n\n{content_text}"
        ),
        goal_id=int(goal_id),
        metadata={
            "due_date": due_date.isoformat(),
            "status": status_text,
            "priority": priority_text,
            "importance": importance_text,
        },
    )
    return GoalResult(goal_id=int(goal.id), title=title_text)


def mark_goal_done(s: Session, *, goal_id: int) -> GoalResult:
    goal = s.get(Goal, int(goal_id))
    if goal is None:
        raise GoalTaskNotFound("Goal not found")
    if (goal.status or "").strip() != "done":
        old = (goal.status or "").strip() or "active"
        goal.status = "done"
        s.add(
            Event(
                kind="goal.confirmed_done_by_user",
                agent="ui",
                task_id=None,
                payload={"goal_id": int(goal_id), "from": old},
            )
        )
        memory_service.try_audit_memory(
            kind="goal.finished",
            source="web",
            summary=f"Finished goal: {goal.title}",
            detail=f"Goal moved from `{old}` to `done`.",
            goal_id=int(goal_id),
            metadata={"from": old, "to": "done"},
        )
    return GoalResult(goal_id=int(goal.id), title=str(goal.title or ""))


def reopen_goal(s: Session, *, goal_id: int) -> GoalResult:
    goal = s.get(Goal, int(goal_id))
    if goal is None:
        raise GoalTaskNotFound("Goal not found")
    if (goal.status or "").strip() == "done":
        goal.status = "active"
        s.add(
            Event(
                kind="goal.reopened_by_user",
                agent="ui",
                task_id=None,
                payload={"goal_id": int(goal_id)},
            )
        )
        memory_service.try_audit_memory(
            kind="goal.reopened",
            source="web",
            summary=f"Reopened goal: {goal.title}",
            detail="Goal moved from `done` back to `active`.",
            goal_id=int(goal_id),
            metadata={"to": "active"},
        )
    return GoalResult(goal_id=int(goal.id), title=str(goal.title or ""))


def delete_goal(s: Session, *, goal_id: int) -> GoalResult:
    goal = s.get(Goal, int(goal_id))
    if goal is None:
        raise GoalTaskNotFound("Goal not found")
    deleted_title = str(goal.title or "")
    s.query(Task).filter(Task.goal_id == int(goal_id)).delete()
    s.delete(goal)
    memory_service.try_audit_memory(
        kind="goal.deleted",
        source="web",
        summary=f"Deleted goal: {deleted_title}",
        detail="Goal and its tasks were deleted.",
        goal_id=int(goal_id),
        metadata={},
    )
    return GoalResult(goal_id=int(goal_id), title=deleted_title)


def create_task(
    s: Session,
    *,
    goal_id: int,
    title: str,
    content: str,
    agent: str = "ui",
    source: str = "web",
    source_inspiration_space_id: int | None = None,
    source_inspiration_draft_id: int | None = None,
    audit: bool = True,
) -> Task:
    goal = s.get(Goal, int(goal_id))
    if goal is None:
        raise GoalTaskNotFound("Goal not found")
    title_text = str(title or "").strip()
    content_text = str(content or "").strip()
    task_type = infer_task_type(title_text, content_text)
    estimated_minutes = infer_estimated_minutes(task_type, title_text, content_text)
    context_key = infer_context_key(title_text, content_text, goal_id=int(goal_id))
    task = Task(
        goal_id=int(goal_id),
        title=title_text,
        content=content_text,
        status="todo",
        task_type=task_type,
        estimated_minutes=estimated_minutes,
        context_key=context_key,
        source_inspiration_space_id=source_inspiration_space_id,
        source_inspiration_draft_id=source_inspiration_draft_id,
    )
    s.add(task)
    s.flush()
    _add_task_created_event(s, task, agent=agent)
    if audit:
        memory_service.try_audit_memory(
            kind="task.created",
            source=source,
            summary=f"Created task: {title_text}",
            detail=f"Task title:\n\n{title_text}\n\nContent:\n\n{content_text}",
            goal_id=int(goal_id),
            task_public_id=str(task.public_id or "") or None,
            metadata={
                "task_type": task_type,
                "estimated_minutes": estimated_minutes,
                "context_key": context_key,
            },
        )
    return task


def update_task(s: Session, *, task_id: int, title: str, content: str) -> TaskResult:
    task = s.get(Task, int(task_id))
    if task is None:
        raise GoalTaskNotFound("Task not found")
    old_title = str(task.title or "")
    old_content = str(task.content or "")
    title_text = str(title or "").strip()
    content_text = str(content or "").strip()
    task_type = infer_task_type(title_text, content_text)
    estimated_minutes = infer_estimated_minutes(task_type, title_text, content_text)
    context_key = infer_context_key(title_text, content_text, goal_id=int(task.goal_id))
    task.title = title_text
    task.content = content_text
    task.task_type = task_type
    task.estimated_minutes = estimated_minutes
    task.context_key = context_key
    memory_service.try_audit_memory(
        kind="task.edited",
        source="web",
        summary=f"Edited task: {title_text}",
        detail=(
            f"Previous title: {old_title}\n\n"
            f"Previous content:\n\n{old_content}\n\n"
            f"Updated title: {title_text}\n\n"
            f"Updated content:\n\n{content_text}"
        ),
        goal_id=int(task.goal_id),
        task_public_id=str(task.public_id or "") or None,
        metadata={
            "task_type": task_type,
            "estimated_minutes": estimated_minutes,
            "context_key": context_key,
        },
    )
    return TaskResult(
        task_id=int(task.id),
        task_public_id=str(task.public_id or ""),
        goal_id=int(task.goal_id),
        title=title_text,
    )


def mark_task_done(
    s: Session, *, task_id: int, now: dt.datetime | None = None
) -> TaskResult:
    task = s.get(Task, int(task_id))
    if task is None:
        raise GoalTaskNotFound("Task not found")
    if task.status != "done":
        old = task.status
        task.status = "done"
        task.completed_at = now or memory_service.utcnow()
        s.add(
            Event(
                kind="task.confirmed_done",
                agent="ui",
                task_id=task.public_id,
                payload={"from": old},
            )
        )
        memory_service.try_audit_memory(
            kind="task.finished",
            source="web",
            summary=f"Finished task: {task.title}",
            detail=f"Task moved from `{old}` to `done`.",
            goal_id=int(task.goal_id),
            task_public_id=task.public_id,
            metadata={"from": old, "to": "done"},
        )
    return TaskResult(
        task_id=int(task.id),
        task_public_id=str(task.public_id or ""),
        goal_id=int(task.goal_id),
        title=str(task.title or ""),
    )


def reopen_task(s: Session, *, task_id: int) -> TaskResult:
    task = s.get(Task, int(task_id))
    if task is None:
        raise GoalTaskNotFound("Task not found")
    if task.status == "done":
        task.status = "todo"
        task.completed_at = None
        s.add(
            Event(
                kind="task.reopened",
                agent="ui",
                task_id=task.public_id,
                payload={},
            )
        )
        memory_service.try_audit_memory(
            kind="task.reopened",
            source="web",
            summary=f"Reopened task: {task.title}",
            detail="Task moved from `done` back to `todo`.",
            goal_id=int(task.goal_id),
            task_public_id=task.public_id,
            metadata={"to": "todo"},
        )
    return TaskResult(
        task_id=int(task.id),
        task_public_id=str(task.public_id or ""),
        goal_id=int(task.goal_id),
        title=str(task.title or ""),
    )


def delete_task(s: Session, *, task_id: int) -> TaskResult:
    task = s.get(Task, int(task_id))
    if task is None:
        raise GoalTaskNotFound("Task not found")
    goal_id = int(task.goal_id)
    deleted_title = str(task.title or "")
    deleted_public_id = str(task.public_id or "")
    space = (
        s.query(AgentSpace)
        .filter(AgentSpace.task_public_id == task.public_id)
        .one_or_none()
    )
    if space is not None:
        sessions = s.query(AgentSession).filter(AgentSession.space_id == space.id).all()
        sess_ids = [ss.session_id for ss in sessions]
        if sess_ids:
            s.query(AgentMessage).filter(AgentMessage.session_id.in_(sess_ids)).delete(
                synchronize_session=False
            )
            s.query(AgentSession).filter(AgentSession.session_id.in_(sess_ids)).delete(
                synchronize_session=False
            )
        s.delete(space)
    s.delete(task)
    memory_service.try_audit_memory(
        kind="task.deleted",
        source="web",
        summary=f"Deleted task: {deleted_title}",
        detail="Task and related AgentSpace resources were deleted.",
        goal_id=goal_id,
        task_public_id=deleted_public_id or None,
        metadata={},
    )
    return TaskResult(
        task_id=int(task_id),
        task_public_id=deleted_public_id,
        goal_id=goal_id,
        title=deleted_title,
    )
