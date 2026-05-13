# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ...db import session_scope
from ...domains.events import service as event_service
from ...domains.goals import service as goal_service
from ...domains.memory import service as memory_service
from ...models import AgentSpace, Event, Goal, Task


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


def create_router(
    *, templates: Jinja2Templates, release_agent_space: Callable[[str], Awaitable[dict]]
) -> APIRouter:
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse(url="/goals", status_code=302)

    @router.get("/goals", response_class=HTMLResponse)
    def goals_list(request: Request) -> HTMLResponse:
        with session_scope() as s:
            # Dashboard 左侧目标列表：支持筛选/排序
            goal_filter = (request.query_params.get("gfilter") or "ALL").strip().upper()
            goal_sort = (request.query_params.get("gsort") or "DDL").strip().upper()

            goals_all = s.query(Goal).order_by(Goal.id.desc()).all()
            today = dt.date.today()

            # 仅对当前页面所需的 goals 做聚合
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

            # AgentSpace：用于 Task 详情页展示“创建/进入工作区”
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

            # 尽量用已有 events 推断“进行中/进度百分比/最近更新时间”
            public_ids = [t.public_id for t in tasks]
            latest_event_by_task: dict[str, Event] = {}
            if public_ids:
                evs = (
                    s.query(Event)
                    .filter(Event.task_id.in_(public_ids))
                    .order_by(Event.id.desc())
                    .all()
                )
                for ev in evs:
                    if ev.task_id and ev.task_id not in latest_event_by_task:
                        latest_event_by_task[ev.task_id] = ev

            # 任务详情栏需要展示“与该任务相关的事件”（只展示最近 N 条，避免页面过重）。
            # 注意：事件展示面向人，不直接暴露内部 kind/status 码。
            task_events: dict[str, list[dict]] = {pid: [] for pid in public_ids}

            # Goal 的事件：聚合该 Goal 下各 Task 的事件（用于 Dashboard 中间栏 Goal->Event）。
            # 先初始化，保证即使没有 task 也不会出现未定义。
            task_goal_by_pid: dict[str, int] = {t.public_id: t.goal_id for t in tasks}
            goal_events: dict[int, list[dict]] = {g.id: [] for g in goals_all}
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
                    # 额外带上 task_public_id，未来可在 UI 里做“打开该任务”。
                    goal_events[gid].append({**it, "task_public_id": pid})

            # Goal 级事件：用于 Goal 详情页的 Event 区块（例如“confirm done by user”）。
            # 同时记录“完成时间”，用于 Dashboard 左侧排序。
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

            task_meta: dict[str, dict] = {}
            now = memory_service.utcnow()
            for t in tasks:
                ev = latest_event_by_task.get(t.public_id)
                last_at = None
                kind = None
                if ev is not None:
                    kind = ev.kind
                    last_at = ev.created_at

                ui_status = "todo"
                if t.status == "done":
                    ui_status = "done"
                else:
                    if kind in {"task.started", "task.progress"}:
                        ui_status = "in_progress"

                task_meta[t.public_id] = {
                    "ui_status": ui_status,
                    # 产品约束：进度仅二元（完成/未完成），不展示 80% 等百分比。
                    "percent": (100 if t.status == "done" else None),
                    "last_event_at": last_at,
                    "elapsed": _human_since(last_at or t.created_at, now=now),
                }

            def _task_sort_key(t: Task):
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

            for gid, grouped_tasks in tasks_by_goal.items():
                grouped_tasks.sort(key=_task_sort_key)

            def _goal_group(g: Goal) -> int:
                # 0: in_progress, 1: expired, 2: completed
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

            def _sort_key(g: Goal):
                grp = _goal_group(g)
                created_at = getattr(g, "created_at", None) or memory_service.utcnow()
                # 只对已完成的 goal 使用 done_at；否则为空
                done_at = goal_done_at.get(int(g.id)) if grp == 2 else None

                # 统一把“已完成”放到下面（grp 参与排序），满足默认要求
                if goal_sort in {"CREATED", "CREATED_AT", "CREATED_EVENT"}:
                    # 新建优先（倒序）
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
                    # 完成时间优先（倒序）；未完成放在各自组里按创建时间兜底
                    ts_done = (
                        done_at.timestamp()
                        if (done_at and hasattr(done_at, "timestamp"))
                        else -1
                    )
                    ts_created = (
                        created_at.timestamp()
                        if hasattr(created_at, "timestamp")
                        else 0
                    )
                    return (grp, -ts_done if grp == 2 else -ts_created, -int(g.id))
                # 默认 DDL（due_date 升序；同 DDL 以创建时间倒序）
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

            # Dashboard 左栏显示用标题截断（不再维护独立 summary 字段）。
            goal_display: dict[int, str] = {}
            for g in goals:
                goal_display[g.id] = _truncate_zh(str(g.title or "").strip(), 20)

            task_display: dict[str, str] = {}
            for t in tasks:
                task_display[t.public_id] = _truncate_zh(str(t.title or "").strip(), 20)

            # 选中态（用于右侧详情栏默认展示）
            sel_goal_id = request.query_params.get("goal")
            sel_task_pid = request.query_params.get("task")
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

        default_due = dt.date.today() + dt.timedelta(days=1)
        return templates.TemplateResponse(
            request,
            "goals.html",
            {
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
            },
        )

    @router.get("/goals/new", response_class=HTMLResponse)
    def goals_new(request: Request) -> HTMLResponse:
        # 兼容旧入口：直接跳到目标页
        return RedirectResponse(url="/goals", status_code=302)

    @router.post("/goals", include_in_schema=False)
    async def goals_create(
        title: str = Form(..., min_length=1, max_length=2000),
        content: str = Form(..., min_length=1, max_length=4000),
        due_date: str = Form(...),
    ) -> RedirectResponse:
        parsed_due = dt.date.fromisoformat(due_date)
        with session_scope() as s:
            goal = goal_service.create_goal(
                s,
                title=title,
                content=content,
                due_date=parsed_due,
                agent="ui",
                source="web",
            )
            created_goal_id = int(goal.id or 0)
        return RedirectResponse(
            url=f"/goals?goal={created_goal_id}&tab=tasks", status_code=303
        )

    @router.post("/goals/{goal_id:int}/tasks", include_in_schema=False)
    def tasks_create(
        goal_id: int,
        title: str = Form(..., min_length=1, max_length=512),
        content: str = Form(..., min_length=1, max_length=4000),
    ) -> RedirectResponse:
        with session_scope() as s:
            try:
                goal_service.create_task(
                    s,
                    goal_id=int(goal_id),
                    title=title,
                    content=content,
                    agent="ui",
                    source="web",
                )
            except goal_service.GoalTaskNotFound:
                raise HTTPException(status_code=404, detail="Goal not found")
        return RedirectResponse(url=f"/goals?goal={goal_id}&tab=tasks", status_code=303)

    @router.post("/goals/{goal_id:int}/done", include_in_schema=False)
    def goals_mark_done(goal_id: int) -> RedirectResponse:
        """将 Goal 标记为已完成（人工行为）。"""

        with session_scope() as s:
            try:
                goal_service.mark_goal_done(s, goal_id=int(goal_id))
            except goal_service.GoalTaskNotFound:
                raise HTTPException(status_code=404, detail="Goal not found")
        return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)

    @router.post("/goals/{goal_id:int}/reopen", include_in_schema=False)
    def goals_reopen(goal_id: int) -> RedirectResponse:
        """将已完成的 Goal 重新打开（人工行为）。"""

        with session_scope() as s:
            try:
                goal_service.reopen_goal(s, goal_id=int(goal_id))
            except goal_service.GoalTaskNotFound:
                raise HTTPException(status_code=404, detail="Goal not found")
        return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)

    @router.post("/tasks/{task_id:int}/done", include_in_schema=False)
    def tasks_mark_done(task_id: int) -> RedirectResponse:
        with session_scope() as s:
            try:
                result = goal_service.mark_task_done(s, task_id=int(task_id))
            except goal_service.GoalTaskNotFound:
                raise HTTPException(status_code=404, detail="Task not found")
            goal_id = result.goal_id
            task_public_id = result.task_public_id

        # 完成任务时自动释放 AgentSpace（若存在）。
        # 注意：这里是 best-effort；释放失败不应阻断“完成”本身。
        try:
            asyncio.run(release_agent_space(task_public_id))
        except RuntimeError:
            # 兼容：极少数情况下当前线程已有 event loop。
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(release_agent_space(task_public_id))
            finally:
                try:
                    loop.close()
                except Exception:
                    pass
        except Exception:
            pass
        return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)

    @router.post("/tasks/{task_id:int}/reopen", include_in_schema=False)
    def tasks_reopen(task_id: int) -> RedirectResponse:
        """将已完成任务重新打开（人工行为）。"""

        with session_scope() as s:
            try:
                result = goal_service.reopen_task(s, task_id=int(task_id))
            except goal_service.GoalTaskNotFound:
                raise HTTPException(status_code=404, detail="Task not found")
            goal_id = result.goal_id
        return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)

    @router.post("/tasks/{task_id:int}/edit", include_in_schema=False)
    def tasks_update(
        task_id: int,
        title: str = Form(..., min_length=1, max_length=512),
        content: str = Form(..., min_length=1, max_length=4000),
    ) -> RedirectResponse:
        with session_scope() as s:
            try:
                result = goal_service.update_task(
                    s, task_id=int(task_id), title=title, content=content
                )
            except goal_service.GoalTaskNotFound:
                raise HTTPException(status_code=404, detail="Task not found")
        # 保持 Dashboard 选中态
        return RedirectResponse(
            url=f"/goals?task={result.task_public_id}&goal={result.goal_id}",
            status_code=303,
        )

    @router.post("/tasks/{task_id:int}/delete", include_in_schema=False)
    def tasks_delete(task_id: int) -> RedirectResponse:
        with session_scope() as s:
            try:
                result = goal_service.delete_task(s, task_id=int(task_id))
            except goal_service.GoalTaskNotFound:
                raise HTTPException(status_code=404, detail="Task not found")
        return RedirectResponse(url=f"/goals?goal={result.goal_id}", status_code=303)

    @router.post("/goals/{goal_id:int}/edit", include_in_schema=False)
    def goals_update(
        goal_id: int,
        title: str = Form(..., min_length=1, max_length=2000),
        content: str = Form(..., min_length=1, max_length=4000),
        due_date: str = Form(...),
        status: str = Form("active", max_length=32),
        priority: str = Form("normal", max_length=32),
        importance: str = Form("normal", max_length=32),
    ) -> RedirectResponse:
        parsed_due = dt.date.fromisoformat(due_date)
        with session_scope() as s:
            try:
                goal_service.update_goal(
                    s,
                    goal_id=int(goal_id),
                    title=title,
                    content=content,
                    due_date=parsed_due,
                    status=status,
                    priority=priority,
                    importance=importance,
                )
            except goal_service.GoalTaskNotFound:
                raise HTTPException(status_code=404, detail="Goal not found")
        return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)

    @router.post("/goals/{goal_id:int}/delete", include_in_schema=False)
    def goals_delete(goal_id: int) -> RedirectResponse:
        with session_scope() as s:
            try:
                goal_service.delete_goal(s, goal_id=int(goal_id))
            except goal_service.GoalTaskNotFound:
                raise HTTPException(status_code=404, detail="Goal not found")
        return RedirectResponse(url="/goals", status_code=303)

    return router
