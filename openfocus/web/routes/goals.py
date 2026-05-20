# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ...db import session_scope
from ...domains.dashboard import read_model as dashboard_read_model
from ...domains.goals import service as goal_service


def create_router(
    *, templates: Jinja2Templates, release_agent_space: Callable[[str], Awaitable[dict]]
) -> APIRouter:
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse(url="/goals", status_code=302)

    @router.get("/goals", response_class=HTMLResponse)
    def goals_list(request: Request) -> HTMLResponse:
        context = dashboard_read_model.load_dashboard_context(request.query_params)
        return templates.TemplateResponse(
            request,
            "goals.html",
            context,
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
        return RedirectResponse(url=f"/goals?goal={created_goal_id}", status_code=303)

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
        return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)

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
