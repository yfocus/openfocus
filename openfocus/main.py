from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from .db import get_engine, session_scope
from .models import Base, Event, Goal, GoalPlanMessage, GoalPlanSession, Task
from .schemas import AgentEventIn, FocusReportIn

from .agent.llm.openai_compat import OpenAICompatibleProvider
from .agent.agents.task_prompt_recommender import TaskPromptRecommenderAgent


APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="OpenFocus", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)

    # 轻量 SQLite 迁移：给 goals 表补齐新增字段（避免引入 alembic 的复杂度）
    with engine.begin() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(goals)").fetchall()]
        if "description" not in cols:
            conn.execute(text("ALTER TABLE goals ADD COLUMN description VARCHAR(4000) NOT NULL DEFAULT ''"))
        if "status" not in cols:
            conn.execute(text("ALTER TABLE goals ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'active'"))
        if "priority" not in cols:
            conn.execute(text("ALTER TABLE goals ADD COLUMN priority VARCHAR(32) NOT NULL DEFAULT 'normal'"))
        if "importance" not in cols:
            conn.execute(text("ALTER TABLE goals ADD COLUMN importance VARCHAR(32) NOT NULL DEFAULT 'normal'"))


def _get_llm_provider_or_error() -> tuple[OpenAICompatibleProvider | None, str | None]:
    try:
        return OpenAICompatibleProvider.from_env(), None
    except Exception as e:
        return None, (
            "缺少 LLM 配置，Plan 模式不可用。\n"
            "请设置环境变量（任选其一）：\n"
            "- OpenAI-compatible：OPENFOCUS_OPENAI_API_KEY（以及可选的 OPENFOCUS_OPENAI_BASE_URL/OPENFOCUS_OPENAI_MODEL）\n"
            "- Ark：OPENFOCUS_ARK_API_KEY（或 ARK_API_KEY），以及 OPENFOCUS_ARK_BASE_URL/OPENFOCUS_ARK_MODEL（或 ARK_BASE_URL/ARK_MODEL）\n"
            f"错误：{e}"
        )


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/goals", status_code=302)


@app.get("/goals", response_class=HTMLResponse)
def goals_list(request: Request) -> HTMLResponse:
    with session_scope() as s:
        goals = s.query(Goal).order_by(Goal.id.desc()).all()
    default_due = dt.date.today() + dt.timedelta(days=1)
    return templates.TemplateResponse(
        request,
        "goals.html",
        {
            "goals": goals,
            "default_due": default_due.isoformat(),
        },
    )


@app.get("/goals/new", response_class=HTMLResponse)
def goals_new(request: Request) -> HTMLResponse:
    # 兼容旧入口：直接跳到目标页
    return RedirectResponse(url="/goals", status_code=302)


@app.post("/goals", include_in_schema=False)
def goals_create(
    content: str = Form(..., min_length=1, max_length=2000),
    description: str = Form("", max_length=4000),
    due_date: str = Form(...),
) -> RedirectResponse:
    parsed_due = dt.date.fromisoformat(due_date)
    with session_scope() as s:
        s.add(Goal(content=content.strip(), description=description.strip(), due_date=parsed_due))
    return RedirectResponse(url="/goals", status_code=303)


@app.get("/goals/{goal_id:int}", response_class=HTMLResponse)
def goals_detail(request: Request, goal_id: int) -> HTMLResponse:
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        tasks = s.query(Task).filter(Task.goal_id == goal_id).order_by(Task.id.asc()).all()
    return templates.TemplateResponse(
        request,
        "goal_detail.html",
        {
            "goal": goal,
            "tasks": tasks,
        },
    )


@app.post("/goals/{goal_id:int}/tasks", include_in_schema=False)
def tasks_create(goal_id: int, title: str = Form(..., min_length=1, max_length=512)) -> RedirectResponse:
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        s.add(Task(goal_id=goal_id, title=title.strip(), status="todo"))
    return RedirectResponse(url=f"/goals/{goal_id}", status_code=303)


@app.post("/tasks/{task_id:int}/done", include_in_schema=False)
def tasks_mark_done(task_id: int) -> RedirectResponse:
    with session_scope() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if t.status != "done":
            t.status = "done"
            t.completed_at = dt.datetime.now(dt.timezone.utc)
        goal_id = t.goal_id
    return RedirectResponse(url=f"/goals/{goal_id}", status_code=303)


@app.post("/tasks/{task_id:int}/delete", include_in_schema=False)
def tasks_delete(task_id: int) -> RedirectResponse:
    with session_scope() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise HTTPException(status_code=404, detail="Task not found")
        goal_id = t.goal_id
        s.delete(t)
    return RedirectResponse(url=f"/goals/{goal_id}", status_code=303)


@app.get("/goals/{goal_id:int}/edit", response_class=HTMLResponse)
def goals_edit(request: Request, goal_id: int) -> HTMLResponse:
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
    return templates.TemplateResponse(
        request,
        "goal_edit.html",
        {
            "goal": goal,
        },
    )


@app.post("/goals/{goal_id:int}/edit", include_in_schema=False)
def goals_update(
    goal_id: int,
    content: str = Form(..., min_length=1, max_length=2000),
    description: str = Form("", max_length=4000),
    due_date: str = Form(...),
    status: str = Form("active", max_length=32),
    priority: str = Form("normal", max_length=32),
    importance: str = Form("normal", max_length=32),
) -> RedirectResponse:
    parsed_due = dt.date.fromisoformat(due_date)
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        goal.content = content.strip()
        goal.description = description.strip()
        goal.due_date = parsed_due
        goal.status = status.strip() or "active"
        goal.priority = priority.strip() or "normal"
        goal.importance = importance.strip() or "normal"
    return RedirectResponse(url="/goals", status_code=303)


@app.post("/goals/{goal_id:int}/delete", include_in_schema=False)
def goals_delete(goal_id: int) -> RedirectResponse:
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        # 清理关联 tasks（MVP 先做简单级联）
        s.query(Task).filter(Task.goal_id == goal_id).delete()
        s.delete(goal)
    return RedirectResponse(url="/goals", status_code=303)


@app.get("/goals/plan", response_class=HTMLResponse)
def goal_plan_start(request: Request) -> HTMLResponse:
    default_due = dt.date.today() + dt.timedelta(days=7)
    _provider, err = _get_llm_provider_or_error()
    return templates.TemplateResponse(
        request,
        "goal_plan.html",
        {
            "default_due": default_due.isoformat(),
            "error": err,
        },
    )


def _plan_system_prompt(*, remaining_turns: int) -> str:
    return (
        "你是一个 Goal 规划助手（Plan 模式）。\n"
        "目标：通过与用户对话，逐步澄清 goal 的真实目的、识别潜在 goal 冲突、识别 goal 与 goal 的关系，并输出可执行 tasks。\n"
        "你必须严格输出 JSON（不要 Markdown）。\n"
        "你每次只能做两种之一：\n"
        "1) 继续提问：输出 {\"type\":\"question\", \"question\":\"...\"}\n"
        "2) 给出最终方案：输出 {\"type\":\"final\", \"goal\":{...}, \"tasks\":[...], \"conflicts\":[...], \"relations\":[...]}\n"
        f"剩余可提问次数：{remaining_turns}。当 remaining_turns<=0 时必须输出 final。\n"
        "final.goal 需要字段：content, description, status, priority, importance。\n"
        "tasks 每项至少包含 title（字符串）。"
    )


def _plan_llm_step(
    *, provider: OpenAICompatibleProvider, session: GoalPlanSession, messages: list[GoalPlanMessage]
) -> dict:
    remaining = max(0, 3 - session.turns)
    sys = _plan_system_prompt(remaining_turns=remaining)
    convo: list[dict] = [{"role": "system", "content": sys}]
    convo.append(
        {
            "role": "user",
            "content": f"草稿 goal：{session.draft_content}\n完成时间：{session.due_date.isoformat()}\n",
        }
    )
    for m in messages:
        convo.append({"role": m.role, "content": m.content})

    res = provider.chat_completions(
        messages=convo,
        temperature=0.0,
        max_tokens=900,
        response_format={"type": "json_object"},
    )
    import json as _json

    return _json.loads(res.content)


@app.post("/goals/plan/start", include_in_schema=False)
def goal_plan_create_session(
    draft_content: str = Form(..., min_length=1, max_length=2000),
    due_date: str = Form(...),
) -> RedirectResponse:
    provider, err = _get_llm_provider_or_error()
    if provider is None:
        return RedirectResponse(url="/goals/plan", status_code=303)

    parsed_due = dt.date.fromisoformat(due_date)
    with session_scope() as s:
        sess = GoalPlanSession(draft_content=draft_content.strip(), due_date=parsed_due)
        s.add(sess)
        s.flush()
        sid = sess.id
        # 先写一条 assistant 引导语
        s.add(GoalPlanMessage(session_id=sid, role="assistant", content="我会先问你几个问题来澄清目标。请尽量具体。"))

    with session_scope() as s:
        sess = s.get(GoalPlanSession, sid)
        msgs = s.query(GoalPlanMessage).filter(GoalPlanMessage.session_id == sid).order_by(GoalPlanMessage.id.asc()).all()
        data = _plan_llm_step(provider=provider, session=sess, messages=msgs)
        if data.get("type") == "question":
            s.add(GoalPlanMessage(session_id=sid, role="assistant", content=str(data.get("question") or "")))
        else:
            # 直接 final（极少见），也写进去
            s.add(GoalPlanMessage(session_id=sid, role="assistant", content=str(data)))
            sess.result_json = data

    return RedirectResponse(url=f"/goals/plan/{sid}", status_code=303)


@app.get("/goals/plan/{session_id}", response_class=HTMLResponse)
def goal_plan_view(request: Request, session_id: int) -> HTMLResponse:
    with session_scope() as s:
        sess = s.get(GoalPlanSession, session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")
        msgs = s.query(GoalPlanMessage).filter(GoalPlanMessage.session_id == session_id).order_by(GoalPlanMessage.id.asc()).all()
    return templates.TemplateResponse(
        request,
        "goal_plan_session.html",
        {
            "session": sess,
            "messages": msgs,
            "created_goal_id": sess.created_goal_id,
        },
    )


@app.post("/goals/plan/{session_id}/reply", include_in_schema=False)
def goal_plan_reply(session_id: int, answer: str = Form(..., min_length=1, max_length=20000)) -> RedirectResponse:
    provider, err = _get_llm_provider_or_error()
    if provider is None:
        return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)

    with session_scope() as s:
        sess = s.get(GoalPlanSession, session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if sess.status != "in_progress":
            return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)
        s.add(GoalPlanMessage(session_id=session_id, role="user", content=answer.strip()))
        sess.turns += 1

    with session_scope() as s:
        sess = s.get(GoalPlanSession, session_id)
        msgs = s.query(GoalPlanMessage).filter(GoalPlanMessage.session_id == session_id).order_by(GoalPlanMessage.id.asc()).all()
        data = _plan_llm_step(provider=provider, session=sess, messages=msgs)

        if data.get("type") == "question":
            q = str(data.get("question") or "").strip() or "请补充更多细节。"
            s.add(GoalPlanMessage(session_id=session_id, role="assistant", content=q))
            return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)

        # final：落库 goal+tasks
        goal_obj = data.get("goal") or {}
        tasks = data.get("tasks") or []
        content = str(goal_obj.get("content") or sess.draft_content).strip()
        description = str(goal_obj.get("description") or "").strip()
        status = str(goal_obj.get("status") or "active").strip() or "active"
        priority = str(goal_obj.get("priority") or "normal").strip() or "normal"
        importance = str(goal_obj.get("importance") or "normal").strip() or "normal"

        g = Goal(
            content=content,
            description=description,
            due_date=sess.due_date,
            status=status,
            priority=priority,
            importance=importance,
        )
        s.add(g)
        s.flush()

        for t in tasks:
            if not isinstance(t, dict):
                continue
            title = str(t.get("title") or "").strip()
            if not title:
                continue
            s.add(Task(goal_id=g.id, title=title, status="todo"))

        sess.status = "completed"
        sess.created_goal_id = g.id
        sess.result_json = data
        s.add(GoalPlanMessage(session_id=session_id, role="assistant", content="已生成目标与任务清单。"))

        return RedirectResponse(url=f"/goals/{g.id}", status_code=303)


@app.post("/api/agent/events")
def agent_report_event(payload: AgentEventIn) -> dict:
    """Agent 上报任务进度/状态。

    每次调用都会落一条 event 到数据库，便于后续做历史、指标与推荐。
    """
    with session_scope() as s:
        ev = Event(
            kind=payload.kind,
            agent=payload.agent,
            task_id=payload.task_id,
            payload=payload.payload,
        )
        s.add(ev)

        # 轻量联动：若上报的 task_id 命中 Task.public_id，并且看起来完成了，则标记 done。
        if payload.task_id:
            t = s.query(Task).filter(Task.public_id == payload.task_id).one_or_none()
            if t is not None:
                done = False
                if payload.kind in {"task.completed", "task.done", "completed", "done"}:
                    done = True
                if payload.payload.get("done") is True:
                    done = True
                percent = payload.payload.get("percent")
                if isinstance(percent, (int, float)) and percent >= 100:
                    done = True
                if done and t.status != "done":
                    t.status = "done"
                    t.completed_at = dt.datetime.now(dt.timezone.utc)

        s.flush()  # 获取自增 id
        event_id = ev.id
        created_at = ev.created_at
    return {"id": event_id, "created_at": created_at}


@app.post("/api/skills/focus_report")
def focus_report(report: FocusReportIn) -> dict:
    """Skill: focus_report

    用于外部 agent 上报任务执行情况。
    - 每次上报都会作为 Event 持久化（kind=skill.focus_report）
    - 会尝试结合当前 tasks 状态自动勾选完成（标记 Task.status=done）
    """
    payload = {
        "task_name": report.task_name,
        "status": report.status,
        "goal_id": report.goal_id,
        "task_public_id": report.task_public_id,
        "user_prompt": report.user_prompt,
        "assistant_response": report.assistant_response,
        "metadata": report.metadata,
    }

    normalized = report.status.strip().lower()
    is_done = normalized in {"done", "completed", "succeeded", "success", "ok"}

    with session_scope() as s:
        s.add(
            Event(
                kind="skill.focus_report",
                agent=report.agent,
                task_id=report.task_public_id,
                payload=payload,
            )
        )

        updated_task: dict | None = None
        if is_done:
            t = None
            if report.task_public_id:
                t = s.query(Task).filter(Task.public_id == report.task_public_id).one_or_none()
            if t is None:
                q = s.query(Task).filter(Task.title == report.task_name)
                if report.goal_id is not None:
                    q = q.filter(Task.goal_id == report.goal_id)
                t = q.order_by(Task.id.desc()).first()
            if t is not None and t.status != "done":
                t.status = "done"
                t.completed_at = dt.datetime.now(dt.timezone.utc)
                updated_task = {"id": t.id, "public_id": t.public_id, "status": t.status}

        s.flush()
    return {"ok": True, "task_updated": updated_task}


class _NoopEventSink:
    def emit(self, kind: str, agent: str, payload: dict | None = None, task_id: str | None = None) -> None:
        return None


@app.get("/api/tasks/{task_public_id}/recommended_prompt")
def task_recommended_prompt(task_public_id: str) -> dict:
    """按需生成任务推荐提示词（不落库）。"""

    provider, err = _get_llm_provider_or_error()
    if provider is None:
        raise HTTPException(status_code=400, detail=err or "LLM provider not configured")

    agent = TaskPromptRecommenderAgent(task_public_id=task_public_id, provider=provider)
    try:
        out = agent.run(sink=_NoopEventSink())
    except ValueError as e:
        msg = str(e)
        if "Task not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=500, detail=msg)
    except Exception as e:
        # LLM 调用失败/网关错误等：向前端返回可读错误信息（不包含密钥）。
        raise HTTPException(status_code=502, detail=str(e))

    return {"task_public_id": task_public_id, "prompt": out["prompt"]}
