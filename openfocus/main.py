from __future__ import annotations

import datetime as dt
import json
import os
import mimetypes
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from .db import get_engine, session_scope
from .models import AgentSpace, Base, Companion, Event, Goal, GoalPlanMessage, GoalPlanSession, Task
from .schemas import (
    AgentEventIn,
    AgentSpaceCreateIn,
    FocusReportIn,
)

from .agent.llm.openai_compat import OpenAICompatibleProvider
from .agent.agents.task_prompt_recommender import TaskPromptRecommenderAgent

from .companion_grpc import CompanionGrpcError, CompanionGrpcServer


APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="OpenFocus", version="0.1.0")


# OpenFocus(Control Plane) 内置 gRPC server：Companion(Data Plane) 以客户端方式连接进来。
COMPANION_GRPC = CompanionGrpcServer()


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


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
    now = now or _utcnow()

    # SQLite/SQLAlchemy 在某些配置下会返回 naive datetime；这里统一按 UTC 处理。
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)

    return _human_duration_seconds(int((now - ts).total_seconds()))


templates.env.filters["human_since"] = _human_since


def _map_companion_files_error(e: CompanionGrpcError) -> HTTPException:
    msg = str(e or "").strip()
    low = msg.lower()
    if ("not found" in low) or ("no such file" in low):
        return HTTPException(status_code=404, detail=msg or "not found")
    if ("too large" in low) or ("file too large" in low):
        return HTTPException(status_code=413, detail=msg or "file too large")
    if (
        ("traversal" in low)
        or ("invalid path" in low)
        or ("must be absolute" in low)
        or ("not a directory" in low)
        or ("root_path" in low)
    ):
        return HTTPException(status_code=400, detail=msg or "bad request")
    return HTTPException(status_code=502, detail=f"Companion 文件服务错误：{msg}")



@app.on_event("startup")
def _startup() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)

    # 轻量 SQLite 迁移：给 goals 表补齐新增字段（避免引入 alembic 的复杂度）
    with engine.begin() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(goals)").fetchall()]
        if "summary" not in cols:
            conn.execute(text("ALTER TABLE goals ADD COLUMN summary VARCHAR(64) NOT NULL DEFAULT ''"))
        if "description" not in cols:
            conn.execute(text("ALTER TABLE goals ADD COLUMN description VARCHAR(4000) NOT NULL DEFAULT ''"))
        if "status" not in cols:
            conn.execute(text("ALTER TABLE goals ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'active'"))
        if "priority" not in cols:
            conn.execute(text("ALTER TABLE goals ADD COLUMN priority VARCHAR(32) NOT NULL DEFAULT 'normal'"))
        if "importance" not in cols:
            conn.execute(text("ALTER TABLE goals ADD COLUMN importance VARCHAR(32) NOT NULL DEFAULT 'normal'"))

        task_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(tasks)").fetchall()]
        if "summary" not in task_cols:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN summary VARCHAR(64) NOT NULL DEFAULT ''"))

        if "description" not in task_cols:
            conn.execute(text("ALTER TABLE tasks ADD COLUMN description VARCHAR(4000) NOT NULL DEFAULT ''"))

        # goal_plan_sessions 补字段（用于“已有 goal 进入 plan”）
        sess_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(goal_plan_sessions)").fetchall()]
        if "source_goal_id" not in sess_cols:
            conn.execute(text("ALTER TABLE goal_plan_sessions ADD COLUMN source_goal_id INTEGER"))

        # agent_spaces 补字段（Companion 架构升级）
        space_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(agent_spaces)").fetchall()]
        if "companion_id" not in space_cols:
            conn.execute(text("ALTER TABLE agent_spaces ADD COLUMN companion_id INTEGER"))


@app.on_event("startup")
async def _startup_companion_grpc() -> None:
    # 测试里可能希望手动控制启动/端口
    if os.environ.get("OPENFOCUS_GRPC_AUTOSTART", "1") == "0":
        return
    await COMPANION_GRPC.start()


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


def _truncate_zh(text: str, n: int = 20) -> str:
    s = (text or "").strip()
    if len(s) <= n:
        return s
    return s[:n].rstrip() + "…"


def _summarize_items(provider: OpenAICompatibleProvider | None, texts: list[str]) -> list[str]:
    """生成 <=20 字摘要（尽量走 LLM；不可用则截断兜底）。"""

    cleaned = [(t or "").strip() for t in texts]
    needs = [i for i, t in enumerate(cleaned) if len(t) > 20]
    out: list[str] = [t if len(t) <= 20 else "" for t in cleaned]
    if not needs:
        return out

    if provider is None:
        for i in needs:
            out[i] = _truncate_zh(cleaned[i], 20)
        return out

    # 单次批量生成，避免对每条都发请求。
    import json as _json

    payload = {
        "items": [{"i": i, "text": cleaned[i]} for i in needs],
        "rules": "每条输出一个不超过20个中文字符的摘要；不要标点堆叠；不要引号；不要换行。",
    }

    sys = "你是一个中文摘要生成器。你必须严格输出 JSON（不要 Markdown）。"
    user = (
        "为这些文本生成摘要。\n"
        "输出格式：{\"items\":[{\"i\":0,\"summary\":\"...\"}, ...]}\n"
        + _json.dumps(payload, ensure_ascii=False)
    )

    try:
        res = provider.chat_completions(
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.2,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        data = _json.loads(res.content)
        items = data.get("items") or []
        mapping: dict[int, str] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            ii = it.get("i")
            ss = it.get("summary")
            if isinstance(ii, int) and isinstance(ss, str):
                mapping[ii] = ss.strip().replace("\n", " ")
        for i in needs:
            s = mapping.get(i) or _truncate_zh(cleaned[i], 20)
            if len(s) > 20:
                s = _truncate_zh(s, 20)
            out[i] = s
        return out
    except Exception:
        for i in needs:
            out[i] = _truncate_zh(cleaned[i], 20)
        return out


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/goals", status_code=302)


@app.get("/goals", response_class=HTMLResponse)
def goals_list(request: Request) -> HTMLResponse:
    with session_scope() as s:
        goals = s.query(Goal).order_by(Goal.id.desc()).all()

        goal_ids = [g.id for g in goals]
        tasks = []
        if goal_ids:
            tasks = s.query(Task).filter(Task.goal_id.in_(goal_ids)).order_by(Task.id.asc()).all()

        tasks_by_goal: dict[int, list[Task]] = {}
        for t in tasks:
            tasks_by_goal.setdefault(t.goal_id, []).append(t)

        # AgentSpace：用于 Task 详情页展示“创建/进入工作区”
        public_ids = [t.public_id for t in tasks]
        agent_spaces_by_task: dict[str, AgentSpace] = {}
        if public_ids:
            spaces = s.query(AgentSpace).filter(AgentSpace.task_public_id.in_(public_ids)).all()
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
                        "kind_label": _event_kind_label(ev.kind, ev.payload or {}),
                        "source_label": _event_source_label(ev.agent),
                        "created_at": ev.created_at,
                        "summary": _event_summary(ev.kind, ev.payload or {}),
                    }
                )

        task_meta: dict[str, dict] = {}
        now = _utcnow()
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

        # Dashboard 左栏显示用摘要（不触发 LLM；空摘要时做截断兜底）
        goal_display: dict[int, str] = {}
        for g in goals:
            gs = (getattr(g, "summary", "") or "").strip()
            goal_display[g.id] = gs if gs else _truncate_zh(g.content, 20)

        task_display: dict[str, str] = {}
        for t in tasks:
            ts = (getattr(t, "summary", "") or "").strip()
            task_display[t.public_id] = ts if ts else _truncate_zh(t.title, 20)

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
            selected_task = s.query(Task).filter(Task.public_id == sel_task_pid).one_or_none()

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
            "now": _utcnow(),
            "selected_goal": selected_goal,
            "selected_task": selected_task,
            "default_due": default_due.isoformat(),
        },
    )


def _score_text_to_weight(v: str | None) -> int:
    x = (v or "").strip().lower()
    if x in {"p0", "urgent", "highest", "high"}:
        return 3
    if x in {"p1", "medium", "normal"}:
        return 2
    if x in {"p2", "low"}:
        return 1
    return 2


@app.get("/api/recommendations/next")
def recommendations_next(limit: int = 5) -> dict:
    """推荐下一步（MVP 规则版）。

    触发：前端在状态变更、30 分钟轮询、手动刷新时调用。
    """

    # 产品交互：一次只给一个“下一步”，避免让用户在推荐列表里再做决策。
    limit = 1
    now = _utcnow()
    today = now.date()

    with session_scope() as s:
        goals = s.query(Goal).order_by(Goal.due_date.asc(), Goal.id.desc()).all()
        goal_by_id = {g.id: g for g in goals}
        tasks = s.query(Task).filter(Task.status != "done").order_by(Task.id.asc()).all()

        # 最近事件：用于识别 in_progress / percent
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

        scored: list[tuple[float, dict]] = []
        for t in tasks:
            g = goal_by_id.get(t.goal_id)
            if g is None:
                continue

            days_left = (g.due_date - today).days
            urgency = 0.0
            if days_left <= 0:
                urgency = 6.0
            elif days_left <= 1:
                urgency = 5.0
            elif days_left <= 3:
                urgency = 4.0
            elif days_left <= 7:
                urgency = 3.0
            else:
                urgency = 1.0

            pri = _score_text_to_weight(g.priority)
            imp = _score_text_to_weight(g.importance)

            ev = latest_event_by_task.get(t.public_id)
            in_progress = ev is not None and ev.kind in {"task.started", "task.progress"}
            progress_bonus = 1.0 if in_progress else 0.0

            score = urgency * 3 + pri * 2 + imp * 2 + progress_bonus

            why: list[str] = []
            if days_left <= 0:
                why.append("已超期/今日到期，优先处理")
            elif days_left <= 3:
                why.append(f"DDL 临近（{days_left} 天内）")
            else:
                why.append(f"目标 DDL：{g.due_date.isoformat()}")
            why.append(f"重要度：{g.importance} · 优先级：{g.priority}")
            if in_progress:
                why.append("最近有进度上报，继续推进可降低切换成本")

            scored.append(
                (
                    score,
                    {
                        "type": "do_task",
                        "target": {"goal_id": g.id, "task_public_id": t.public_id},
                        "title": t.title,
                        "why": why[:3],
                        "expected_time_minutes": 30 if in_progress else 60,
                        "debug": {"score": score},
                    },
                )
            )

        scored.sort(key=lambda x: x[0], reverse=True)
        best = [it for _s, it in scored[:1]]
        item = (best[0] if best else None)

    sentence = None
    if item is not None:
        because = (item.get("why") or [])
        because_text = because[0] if because else ""
        sentence = f"建议下一步去完成「{item.get('title') or ''}」，因为{because_text}。" if because_text else f"建议下一步去完成「{item.get('title') or ''}」。"

    return {"generated_at": now.isoformat(), "item": item, "items": ([item] if item else []), "sentence": sentence}


@app.get("/goals/new", response_class=HTMLResponse)
def goals_new(request: Request) -> HTMLResponse:
    # 兼容旧入口：直接跳到目标页
    return RedirectResponse(url="/goals", status_code=302)


@app.post("/goals", include_in_schema=False)
def goals_create(
    content: str = Form(..., min_length=1, max_length=2000),
    description: str = Form(..., min_length=1, max_length=4000),
    due_date: str = Form(...),
) -> RedirectResponse:
    parsed_due = dt.date.fromisoformat(due_date)
    provider, _err = _get_llm_provider_or_error()
    summary = _summarize_items(provider, [content.strip()])[0]
    with session_scope() as s:
        s.add(
            Goal(
                content=content.strip(),
                summary=summary,
                description=description.strip(),
                due_date=parsed_due,
            )
        )
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
def tasks_create(
    goal_id: int,
    title: str = Form(..., min_length=1, max_length=512),
    description: str = Form(..., min_length=1, max_length=4000),
) -> RedirectResponse:
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="Goal not found")
        provider, _err = _get_llm_provider_or_error()
        summary = _summarize_items(provider, [title.strip()])[0]
        s.add(
            Task(
                goal_id=goal_id,
                title=title.strip(),
                summary=summary,
                description=description.strip(),
                status="todo",
            )
        )
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/tasks/{task_id:int}/done", include_in_schema=False)
def tasks_mark_done(task_id: int) -> RedirectResponse:
    with session_scope() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if t.status != "done":
            old = t.status
            t.status = "done"
            t.completed_at = dt.datetime.now(dt.timezone.utc)
            s.add(
                Event(
                    kind="task.confirmed_done",
                    agent="ui",
                    task_id=t.public_id,
                    payload={"from": old},
                )
            )
        goal_id = t.goal_id
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/tasks/{task_id:int}/reopen", include_in_schema=False)
def tasks_reopen(task_id: int) -> RedirectResponse:
    """将已完成任务重新打开（人工行为）。"""

    with session_scope() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if t.status == "done":
            t.status = "todo"
            t.completed_at = None
            s.add(
                Event(
                    kind="task.reopened",
                    agent="ui",
                    task_id=t.public_id,
                    payload={},
                )
            )
        goal_id = t.goal_id
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/tasks/{task_id:int}/delete", include_in_schema=False)
def tasks_delete(task_id: int) -> RedirectResponse:
    with session_scope() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise HTTPException(status_code=404, detail="Task not found")
        goal_id = t.goal_id
        # 清理该 task 绑定的 AgentSpace（若存在）
        space = s.query(AgentSpace).filter(AgentSpace.task_public_id == t.public_id).one_or_none()
        if space is not None:
            s.delete(space)
        s.delete(t)
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


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
    description: str = Form(..., min_length=1, max_length=4000),
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
    return RedirectResponse(url=f"/goals?goal={goal_id}", status_code=303)


@app.post("/api/goals/extract_content_from_description")
def api_extract_goal_from_description(payload: dict) -> dict:
    """从详细描述提炼 goal 内容（用于 New Goal 对话框的“从详细描述生成”）。"""

    desc = (payload.get("description") if isinstance(payload, dict) else "")
    desc = (str(desc or "").strip())
    if not desc:
        raise HTTPException(status_code=400, detail="description is required")

    provider, err = _get_llm_provider_or_error()
    if provider is None:
        raise HTTPException(status_code=400, detail=err or "LLM provider not configured")

    import json as _json

    sys = "你是一个中文目标提炼器。你必须严格输出 JSON（不要 Markdown）。"
    user = (
        "从下面的详细描述中提炼一个清晰、可执行的 goal 内容（不要超过 2000 字）。\n"
        "要求：只输出 goal 内容，不要多余解释，不要引号，不要换行。\n"
        + _json.dumps({"description": desc}, ensure_ascii=False)
    )

    trace = [
        "读取用户详细描述",
        "提炼为一句可执行目标",
        "校验长度限制（<=2000 字）",
    ]

    res = provider.chat_completions(
        messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
        temperature=0.2,
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    data = _json.loads(res.content)
    content = (data.get("content") or data.get("goal") or "")
    content = str(content).strip().replace("\n", " ")
    if not content:
        raise HTTPException(status_code=502, detail="LLM 返回为空")
    if len(content) > 2000:
        content = content[:2000]
    return {"ok": True, "content": content, "trace": trace}


@app.post("/api/tasks/extract_title_from_description")
def api_extract_task_title_from_description(payload: dict) -> dict:
    """从详细描述提炼 task 标题（用于 New Task 对话框的“从详细描述生成”）。"""

    desc = (payload.get("description") if isinstance(payload, dict) else "")
    desc = (str(desc or "").strip())
    if not desc:
        raise HTTPException(status_code=400, detail="description is required")

    provider, err = _get_llm_provider_or_error()
    if provider is None:
        raise HTTPException(status_code=400, detail=err or "LLM provider not configured")

    import json as _json

    sys = "你是一个中文任务标题提炼器。你必须严格输出 JSON（不要 Markdown）。"
    user = (
        "从下面的详细描述中提炼一个 task 标题（<=512 字），要求：短、清晰、可执行。\n"
        "只输出标题，不要多余解释，不要引号，不要换行。\n"
        + _json.dumps({"description": desc}, ensure_ascii=False)
    )
    trace = [
        "读取用户详细描述",
        "提炼为一句可执行任务标题",
        "校验长度限制（<=512 字）",
    ]

    res = provider.chat_completions(
        messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
        temperature=0.2,
        max_tokens=200,
        response_format={"type": "json_object"},
    )
    data = _json.loads(res.content)
    title = (data.get("title") or data.get("task") or "")
    title = str(title).strip().replace("\n", " ")
    if not title:
        raise HTTPException(status_code=502, detail="LLM 返回为空")
    if len(title) > 512:
        title = title[:512]
    return {"ok": True, "title": title, "trace": trace}


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
    *,
    provider: OpenAICompatibleProvider,
    session: GoalPlanSession,
    messages: list[GoalPlanMessage],
    source_goal: Goal | None = None,
    existing_tasks: list[Task] | None = None,
) -> dict:
    remaining = max(0, 3 - session.turns)
    sys = _plan_system_prompt(remaining_turns=remaining)
    convo: list[dict] = [{"role": "system", "content": sys}]
    extra = ""
    if source_goal is not None:
        extra += f"\n当前 goal：{source_goal.content}\n"
        if source_goal.description:
            extra += f"goal 描述：{source_goal.description}\n"
        extra += f"goal 状态：{source_goal.status} · priority={source_goal.priority} · importance={source_goal.importance}\n"
    if existing_tasks:
        extra += "\n当前已存在的 tasks：\n"
        for t in existing_tasks[:50]:
            extra += f"- [{t.status}] {t.title} (taskId={t.public_id})\n"

    convo.append(
        {
            "role": "user",
            "content": (
                f"草稿 goal：{session.draft_content}\n完成时间：{session.due_date.isoformat()}\n"
                + extra
            ),
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
        s.add(GoalPlanMessage(session_id=sid, role="assistant", content="我会先问你几个问题来澄清目标，然后给出一份可执行的任务拆解草案。"))

    with session_scope() as s:
        sess = s.get(GoalPlanSession, sid)
        msgs = s.query(GoalPlanMessage).filter(GoalPlanMessage.session_id == sid).order_by(GoalPlanMessage.id.asc()).all()
        data = _plan_llm_step(provider=provider, session=sess, messages=msgs)
        if data.get("type") == "question":
            s.add(GoalPlanMessage(session_id=sid, role="assistant", content=str(data.get("question") or "")))
        else:
            # final：保存草案，等待用户确认（不直接落库 goal/tasks）
            sess.result_json = data
            sess.status = "awaiting_confirm"
            s.add(GoalPlanMessage(session_id=sid, role="assistant", content="我已经生成了任务拆解草案。请在下方确认后再创建。"))

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
        source_goal = None
        existing_tasks = None
        if getattr(sess, "source_goal_id", None):
            source_goal = s.get(Goal, sess.source_goal_id)
            existing_tasks = (
                s.query(Task).filter(Task.goal_id == sess.source_goal_id).order_by(Task.id.asc()).all()
            )
        data = _plan_llm_step(
            provider=provider,
            session=sess,
            messages=msgs,
            source_goal=source_goal,
            existing_tasks=existing_tasks,
        )

        if data.get("type") == "question":
            q = str(data.get("question") or "").strip() or "请补充更多细节。"
            s.add(GoalPlanMessage(session_id=session_id, role="assistant", content=q))
            return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)

        # final：保存草案，等待用户确认
        sess.result_json = data
        sess.status = "awaiting_confirm"
        s.add(GoalPlanMessage(session_id=session_id, role="assistant", content="我已经生成了任务拆解草案。请在下方确认后再应用。"))
        return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)


@app.post("/goals/{goal_id:int}/plan/start", include_in_schema=False)
def goal_plan_create_session_from_goal(goal_id: int) -> RedirectResponse:
    """从已有 goal 进入 Plan：输出 tasks 草案，用户确认后再写入。"""

    provider, _err = _get_llm_provider_or_error()
    if provider is None:
        return RedirectResponse(url="/goals/plan", status_code=303)

    with session_scope() as s:
        g = s.get(Goal, goal_id)
        if g is None:
            raise HTTPException(status_code=404, detail="Goal not found")

        sess = GoalPlanSession(draft_content=g.content.strip(), due_date=g.due_date, source_goal_id=goal_id)
        s.add(sess)
        s.flush()
        sid = sess.id
        s.add(
            GoalPlanMessage(
                session_id=sid,
                role="assistant",
                content="我会基于该目标与现有任务，与你确认意图后给出新的任务拆解草案。",
            )
        )

    with session_scope() as s:
        sess = s.get(GoalPlanSession, sid)
        msgs = s.query(GoalPlanMessage).filter(GoalPlanMessage.session_id == sid).order_by(GoalPlanMessage.id.asc()).all()
        g = s.get(Goal, goal_id)
        existing_tasks = s.query(Task).filter(Task.goal_id == goal_id).order_by(Task.id.asc()).all()
        data = _plan_llm_step(provider=provider, session=sess, messages=msgs, source_goal=g, existing_tasks=existing_tasks)
        if data.get("type") == "question":
            s.add(GoalPlanMessage(session_id=sid, role="assistant", content=str(data.get("question") or "")))
        else:
            sess.result_json = data
            sess.status = "awaiting_confirm"
            s.add(GoalPlanMessage(session_id=sid, role="assistant", content="我已经生成了任务拆解草案。请在下方确认后再应用。"))

    return RedirectResponse(url=f"/goals/plan/{sid}", status_code=303)


@app.post("/goals/plan/{session_id}/confirm", include_in_schema=False)
def goal_plan_confirm(session_id: int, selected_task: list[str] = Form(default=[])) -> RedirectResponse:
    with session_scope() as s:
        sess = s.get(GoalPlanSession, session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if sess.status != "awaiting_confirm":
            return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)

        data = sess.result_json or {}
        tasks = data.get("tasks") or []

        # 选中项：用 index 选择，避免 title 重复
        selected_idx: set[int] = set()
        for x in selected_task:
            try:
                selected_idx.add(int(x))
            except Exception:
                continue

        picked: list[str] = []
        for i, t in enumerate(tasks):
            if selected_idx and i not in selected_idx:
                continue
            if not isinstance(t, dict):
                continue
            title = str(t.get("title") or "").strip()
            if title:
                picked.append(title)

        # 没有勾选就直接回到会话
        if not picked:
            s.add(GoalPlanMessage(session_id=session_id, role="assistant", content="未选择任何任务，未做变更。"))
            return RedirectResponse(url=f"/goals/plan/{session_id}", status_code=303)

        target_goal_id: int
        if getattr(sess, "source_goal_id", None):
            target_goal_id = int(sess.source_goal_id)
        else:
            goal_obj = data.get("goal") or {}
            content = str(goal_obj.get("content") or sess.draft_content).strip()
            description = str(goal_obj.get("description") or "").strip()
            status = str(goal_obj.get("status") or "active").strip() or "active"
            priority = str(goal_obj.get("priority") or "normal").strip() or "normal"
            importance = str(goal_obj.get("importance") or "normal").strip() or "normal"

            provider, _err = _get_llm_provider_or_error()
            summary = _summarize_items(provider, [content])[0]
            g = Goal(
                content=content,
                summary=summary,
                description=description,
                due_date=sess.due_date,
                status=status,
                priority=priority,
                importance=importance,
            )
            s.add(g)
            s.flush()
            target_goal_id = g.id
            sess.created_goal_id = g.id

        # 应用 tasks
        provider, _err = _get_llm_provider_or_error()
        summaries = _summarize_items(provider, picked)
        for i, title in enumerate(picked):
            s.add(Task(goal_id=target_goal_id, title=title, summary=summaries[i], status="todo"))

        sess.status = "completed"
        if sess.created_goal_id is None:
            sess.created_goal_id = target_goal_id
        s.add(GoalPlanMessage(session_id=session_id, role="assistant", content="已应用到目标。"))

    return RedirectResponse(url=f"/goals?goal={target_goal_id}", status_code=303)


def _memory_dir() -> Path:
    env = os.environ.get("OPENFOCUS_MEMORY_DIR")
    if env:
        p = Path(env).expanduser().resolve()
    else:
        p = (Path(__file__).resolve().parent.parent / ".data" / "memory").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


@app.get("/memory", response_class=HTMLResponse)
def memory_view(request: Request) -> HTMLResponse:
    mem_dir = _memory_dir()
    user_card = _read_text(mem_dir / "user_card.md")
    user_memory = _read_text(mem_dir / "user_memory.md")
    return templates.TemplateResponse(
        request,
        "memory.html",
        {
            "user_card": user_card,
            "user_memory": user_memory,
        },
    )


@app.get("/companions", response_class=HTMLResponse)
def companions_view(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "companions.html", {})


def _companion_display_status(c: Companion, *, now: dt.datetime | None = None) -> str:
    now = now or _utcnow()
    if (c.status or "").strip() == "pending_certification":
        return "pending_certification"
    last = c.last_seen_at
    if last is None:
        return "offline"
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    # 30 秒无心跳即认为 offline（MVP 简化）
    return "active" if (now - last).total_seconds() <= 30 else "offline"


@app.post("/api/companions/register")
def companion_register(payload: dict) -> dict:
    """Companion -> OpenFocus 注册/心跳。

    Companion 进程启动后应定期调用该接口刷新 last_seen。
    """

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    device_id = str(payload.get("device_id") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not device_id or len(device_id) > 64:
        raise HTTPException(status_code=400, detail="device_id is required")
    if not base_url or len(base_url) > 1024:
        raise HTTPException(status_code=400, detail="base_url is required")

    now = _utcnow()
    with session_scope() as s:
        c = s.query(Companion).filter(Companion.device_id == device_id).one_or_none()
        if c is None:
            c = Companion(device_id=device_id, base_url=base_url, name=name)
            s.add(c)
            s.flush()
        else:
            c.base_url = base_url
            if name:
                c.name = name
        c.last_seen_at = now
        # 若已配对成功则保持 active
        if (c.auth_token or "").strip():
            c.status = "active"
        else:
            c.status = "pending_certification"
        s.add(c)
        cid = c.id
        status_out = c.status

    return {"ok": True, "id": cid, "status": status_out}


@app.get("/api/companions")
def companions_list(limit: int = 50) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    now = _utcnow()
    with session_scope() as s:
        comps = s.query(Companion).order_by(Companion.id.desc()).limit(limit).all()
        ids = [c.id for c in comps]
        spaces_by_comp: dict[int, list[dict]] = {cid: [] for cid in ids}
        if ids:
            spaces = s.query(AgentSpace).filter(AgentSpace.companion_id.in_(ids)).order_by(AgentSpace.id.desc()).all()
            for sp in spaces:
                cid = int(getattr(sp, "companion_id", 0) or 0)
                if cid in spaces_by_comp:
                    spaces_by_comp[cid].append({"id": sp.id, "task_public_id": sp.task_public_id})

    items: list[dict] = []
    for c in comps:
        items.append(
            {
                "id": c.id,
                "device_id": c.device_id,
                "name": c.name,
                "base_url": c.base_url,
                "status": _companion_display_status(c, now=now),
                "last_seen_at": (c.last_seen_at.isoformat() if c.last_seen_at else None),
                "agent_spaces": spaces_by_comp.get(c.id, []),
            }
        )
    return {"ok": True, "items": items}


@app.post("/api/companions/{companion_id:int}/pair")
async def companion_pair(companion_id: int, payload: dict) -> dict:
    code = str((payload.get("code") if isinstance(payload, dict) else "") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    if len(code) != 10:
        raise HTTPException(status_code=400, detail="认证码必须为 10 位")

    now = _utcnow()
    minute_start = now.replace(second=0, microsecond=0)

    with session_scope() as s:
        c = s.get(Companion, companion_id)
        if c is None:
            raise HTTPException(status_code=404, detail="Companion not found")

        # 每分钟最多 10 次尝试
        ws = c.pair_attempt_window_start
        if ws is None or (ws.replace(tzinfo=dt.timezone.utc) if ws.tzinfo is None else ws) != minute_start:
            c.pair_attempt_window_start = minute_start
            c.pair_attempt_count = 0
        if c.pair_attempt_count >= 10:
            raise HTTPException(status_code=429, detail="本分钟认证尝试次数已达上限（10 次）")
        c.pair_attempt_count += 1
        s.add(c)

        device_id = c.device_id

    # 通过 gRPC 长连接下发配对确认
    conn = COMPANION_GRPC.registry.get(companion_id)
    if conn is None:
        raise HTTPException(status_code=502, detail="Companion 未在线（无可用 gRPC 长连接）")
    try:
        token = await conn.request_pair(code, timeout_seconds=10.0)
    except CompanionGrpcError as e:
        raise HTTPException(status_code=502, detail=f"Companion 配对失败：{e}")

    with session_scope() as s:
        c3 = s.get(Companion, companion_id)
        if c3 is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        c3.auth_token = token
        c3.status = "active"
        c3.last_seen_at = now
        s.add(c3)

        s.add(
            Event(
                kind="companion.paired",
                agent="openfocus/ui",
                task_id=None,
                payload={"companion_id": companion_id, "device_id": device_id},
            )
        )
    return {"ok": True}


@app.post("/api/companions/{companion_id:int}/pairing_code")
async def companion_pairing_code(companion_id: int) -> dict:
    """用户点击“认证”时获取（并刷新）当前配对码。

    设计约束：每次用户点击认证都生成一个新的 code，有效期 10 分钟。
    """

    with session_scope() as s:
        c = s.get(Companion, companion_id)
        if c is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        if _companion_display_status(c) == "offline":
            raise HTTPException(status_code=400, detail="Companion offline")

    conn = COMPANION_GRPC.registry.get(companion_id)
    if conn is None:
        raise HTTPException(status_code=502, detail="Companion 未在线（无可用 gRPC 长连接）")

    try:
        _code, expires_at = await conn.request_pairing_code(force_new=True, timeout_seconds=10.0)
    except CompanionGrpcError as e:
        raise HTTPException(status_code=502, detail=f"Companion 获取配对码失败：{e}")

    # 安全要求：配对码只在 Companion 终端/本机侧展示；Web 侧不回传 code，避免“自动填充”绕过人工确认。
    return {"ok": True, "expires_at": expires_at}


@app.post("/api/companions/{companion_id:int}/choose_directory")
async def companion_choose_directory_proxy(companion_id: int) -> dict:
    with session_scope() as s:
        c = s.get(Companion, companion_id)
        if c is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        if _companion_display_status(c) != "active" or not (c.auth_token or "").strip():
            raise HTTPException(status_code=400, detail="Companion 未认证/不可用")
        device_id = c.device_id

    conn = COMPANION_GRPC.registry.get(companion_id)
    if conn is None:
        raise HTTPException(status_code=502, detail="Companion 未在线（无可用 gRPC 长连接）")
    try:
        path = await conn.request_choose_directory(timeout_seconds=30.0)
    except CompanionGrpcError as e:
        raise HTTPException(status_code=502, detail=f"Companion 目录选择失败：{e}")
    return {"ok": True, "path": path}


@app.post("/memory/save", include_in_schema=False)
def memory_save(user_card: str = Form(""), user_memory: str = Form("")) -> RedirectResponse:
    mem_dir = _memory_dir()
    (mem_dir / "user_card.md").write_text(user_card or "", encoding="utf-8")
    (mem_dir / "user_memory.md").write_text(user_memory or "", encoding="utf-8")
    return RedirectResponse(url="/memory", status_code=303)


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

        s.flush()  # 获取自增 id
        event_id = ev.id
        created_at = ev.created_at
    return {"id": event_id, "created_at": created_at}


@app.get("/api/events/recent")
def recent_events(limit: int = 30) -> dict:
    """近期事件（用于 Dashboard 事件流）。"""

    limit = max(1, min(int(limit or 30), 200))
    with session_scope() as s:
        evs = s.query(Event).order_by(Event.id.desc()).limit(limit).all()

        # 只对真实存在的任务提供“打开”能力，避免 UI 出现能点但打不开的事件。
        cand_task_ids = [ev.task_id for ev in evs if ev.task_id]
        existing_task_ids: set[str] = set()
        if cand_task_ids:
            existing_task_ids = {
                r[0]
                for r in s.query(Task.public_id)
                .filter(Task.public_id.in_(cand_task_ids))
                .all()
            }

    items: list[dict] = []
    for ev in evs:
        payload = ev.payload or {}
        task_public_id = ev.task_id if (ev.task_id and ev.task_id in existing_task_ids) else None
        items.append(
            {
                "id": ev.id,
                "kind": ev.kind,
                "kind_label": _event_kind_label(ev.kind, payload),
                "source_label": _event_source_label(ev.agent),
                "task_id": ev.task_id,
                "task_public_id": task_public_id,
                "created_at": ev.created_at.isoformat() if hasattr(ev.created_at, "isoformat") else str(ev.created_at),
                "summary": _event_summary(ev.kind, payload),
            }
        )
    return {"items": items}


@app.post("/api/skills/focus_report")
def focus_report(report: FocusReportIn) -> dict:
    """Skill: focus_report

    用于外部 agent 上报任务执行情况。
    - 每次上报都会作为 Event 持久化（kind=skill.focus_report）
    - 注意：上报“完成”不等于真实完成，是否完成必须由人确认（详情页按钮）。
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

    with session_scope() as s:
        s.add(
            Event(
                kind="skill.focus_report",
                agent=report.agent,
                task_id=report.task_public_id,
                payload=payload,
            )
        )
        s.flush()
    return {"ok": True, "task_updated": None}


def _event_summary(kind: str, payload: object) -> str:
    """将 Event 转成用于 UI 展示的短摘要。"""

    if kind == "task.recommended_prompt.generated":
        return "已生成推荐提示词"
    if kind == "task.confirmed_done":
        return "已人工确认完成"
    if kind == "task.reopened":
        return "已重新打开（从完成状态恢复）"

    if isinstance(payload, dict):
        msg = payload.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()

        # focus_report 的 status 需要做可读化
        if kind == "skill.focus_report":
            tn = payload.get("task_name")
            st = payload.get("status")
            st_label = _status_label(st)
            if tn and st_label:
                return f"{tn} · {st_label}（待确认）"
            if st_label:
                return f"{st_label}（待确认）"

        # 常见上报：percent 进度
        # 产品约束：进度仅二元（完成/未完成），不要展示 80% 等百分比。
        if kind in {"task.progress", "task.started", "task.completed"}:
            if kind == "task.started":
                return "开始执行"
            if kind == "task.completed":
                return "上报完成（待确认）"
            return "有新进展（待确认）"

        # 避免直接暴露 status=... 这种调试风格
        st2 = payload.get("status")
        if isinstance(st2, str) and st2.strip():
            st_label2 = _status_label(st2)
            if st_label2:
                return st_label2

    # 兜底：返回可读 kind_label
    return _event_kind_label(kind, payload)


def _status_label(status: object) -> str:
    x = (str(status or "").strip().lower())
    if not x:
        return ""
    if x in {"succeeded", "success", "ok", "done", "completed"}:
        return "已完成"
    if x in {"failed", "fail", "error"}:
        return "失败"
    if x in {"running", "in_progress", "progress"}:
        return "进行中"
    return str(status).strip()


def _event_source_label(agent: str | None) -> str:
    a = (agent or "").strip()
    if not a:
        return "来源：未知"
    if a.lower() in {"ui", "web", "webui"} or a.lower().endswith("/ui"):
        return "来源：Web 操作"
    return f"来源：Agent（{a}）"


def _event_kind_label(kind: str, payload: object) -> str:
    # 面向人：把内部事件类型翻译成更容易理解的短标题
    if kind == "skill.focus_report":
        return "执行结果上报"
    if kind == "task.completed":
        return "上报完成"
    if kind == "task.progress":
        return "进度上报"
    if kind == "task.started":
        return "开始执行"
    if kind == "task.reopened":
        return "重新打开"
    if kind == "task.confirmed_done":
        return "人工确认完成"
    if kind == "task.recommended_prompt.generated":
        return "生成推荐提示词"
    return kind


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

    prompt = out["prompt"]

    # 记录生成历史（用于 UI 展示）。
    with session_scope() as s:
        s.add(
            Event(
                kind="task.recommended_prompt.generated",
                agent="openfocus/ui",
                task_id=task_public_id,
                payload={"prompt": prompt},
            )
        )

    return {"task_public_id": task_public_id, "prompt": prompt}


@app.get("/api/tasks/{task_public_id}/recommended_prompt_history")
def task_recommended_prompt_history(task_public_id: str, limit: int = 10) -> dict:
    """推荐提示词生成历史（按时间倒序）。"""

    limit = max(1, min(int(limit or 10), 50))
    with session_scope() as s:
        evs = (
            s.query(Event)
            .filter(Event.kind == "task.recommended_prompt.generated")
            .filter(Event.task_id == task_public_id)
            .order_by(Event.id.desc())
            .limit(limit)
            .all()
        )

    items: list[dict] = []
    for ev in evs:
        payload = ev.payload or {}
        prompt = ""
        if isinstance(payload, dict) and isinstance(payload.get("prompt"), str):
            prompt = payload.get("prompt")
        items.append(
            {
                "created_at": ev.created_at.isoformat() if hasattr(ev.created_at, "isoformat") else str(ev.created_at),
                "prompt": prompt,
            }
        )
    return {"task_public_id": task_public_id, "items": items}


@app.get("/tasks/{task_public_id}/agent_space", response_class=HTMLResponse)
def agent_space_view(request: Request, task_public_id: str) -> HTMLResponse:
    with session_scope() as s:
        task = s.query(Task).filter(Task.public_id == task_public_id).one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        goal = s.query(Goal).filter(Goal.id == task.goal_id).one_or_none()
        space = s.query(AgentSpace).filter(AgentSpace.task_public_id == task_public_id).one_or_none()
        companion = None
        if space is not None and getattr(space, "companion_id", None):
            companion = s.get(Companion, int(space.companion_id))

    return templates.TemplateResponse(
        request,
        "agent_space.html",
        {
            "task": task,
            "goal": goal,
            "space": space,
            "companion": companion,
        },
    )


@app.get("/api/tasks/{task_public_id}/agent_space")
def get_agent_space(task_public_id: str) -> dict:
    with session_scope() as s:
        space = s.query(AgentSpace).filter(AgentSpace.task_public_id == task_public_id).one_or_none()
        if space is None:
            return {"ok": True, "space": None}
        return {
            "ok": True,
            "space": {
                "id": space.id,
                "task_public_id": space.task_public_id,
                "companion_id": getattr(space, "companion_id", None),
                "root_path": space.root_path,
                "agent_type": space.agent_type,
            },
        }


@app.post("/api/tasks/{task_public_id}/agent_space")
def create_agent_space(task_public_id: str, payload: AgentSpaceCreateIn) -> dict:
    root_path = str((payload.root_path or "").strip())
    if not root_path:
        raise HTTPException(status_code=400, detail="root_path 不能为空")

    agent_type = (payload.agent_type or "").strip().lower()
    if agent_type not in {"trae-cli", "coco"}:
        raise HTTPException(status_code=400, detail="暂不支持该 agent_type（当前仅支持 trae-cli(coco)）")

    with session_scope() as s:
        task = s.query(Task).filter(Task.public_id == task_public_id).one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")

        comp = s.get(Companion, int(payload.companion_id))
        if comp is None:
            raise HTTPException(status_code=400, detail="Companion 不存在")
        if comp.status != "active" or not (comp.auth_token or "").strip():
            raise HTTPException(status_code=400, detail="Companion 未认证/不可用")

        existing = s.query(AgentSpace).filter(AgentSpace.task_public_id == task_public_id).one_or_none()
        if existing is not None:
            # 简化：已存在则更新（方便快速迭代）
            existing.companion_id = int(payload.companion_id)
            existing.root_path = root_path
            existing.agent_type = "trae-cli"  # 统一落库为 trae-cli
            s.add(existing)
            s.flush()
            space = existing
        else:
            space = AgentSpace(
                task_public_id=task_public_id,
                companion_id=int(payload.companion_id),
                root_path=root_path,
                agent_type="trae-cli",
            )
            s.add(space)
            s.flush()

    return {"ok": True, "space_id": space.id}


@app.delete("/api/tasks/{task_public_id}/agent_space")
def delete_agent_space(task_public_id: str) -> dict:
    with session_scope() as s:
        space = s.query(AgentSpace).filter(AgentSpace.task_public_id == task_public_id).one_or_none()
        if space is None:
            return {"ok": True}
        s.delete(space)
    return {"ok": True}


@app.get("/api/agent_spaces/{space_id}/files/list")
async def agent_space_files_list(space_id: int, path: str = "") -> dict:
    # 通过 Companion(gRPC) 从远端节点列目录（只读）
    with session_scope() as s:
        sp = s.get(AgentSpace, int(space_id))
        if sp is None:
            raise HTTPException(status_code=404, detail="AgentSpace not found")
        if not sp.companion_id:
            raise HTTPException(status_code=400, detail="AgentSpace 未绑定 Companion")

        c = s.get(Companion, int(sp.companion_id))
        if c is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        if _companion_display_status(c) != "active" or not (c.auth_token or "").strip():
            raise HTTPException(status_code=400, detail="Companion 未认证/不可用")

        root_path = str(sp.root_path or "")
        cid = int(c.id)

    conn = COMPANION_GRPC.registry.get(cid)
    if conn is None:
        raise HTTPException(status_code=502, detail="Companion 未在线（无可用 gRPC 长连接）")

    try:
        res = await conn.request_files_list(root_path=root_path, rel_path=str(path or ""), timeout_seconds=10.0)
    except CompanionGrpcError as e:
        raise _map_companion_files_error(e)

    entries = [
        {
            "name": it.name,
            "rel_path": it.rel_path,
            "kind": it.kind,
            "size": int(it.size),
            "mtime": float(it.mtime),
        }
        for it in (res.entries or [])
    ]
    return {"ok": True, "path": res.path, "entries": entries}


@app.get("/api/agent_spaces/{space_id}/files/read")
async def agent_space_files_read(space_id: int, path: str) -> dict:
    with session_scope() as s:
        sp = s.get(AgentSpace, int(space_id))
        if sp is None:
            raise HTTPException(status_code=404, detail="AgentSpace not found")
        if not sp.companion_id:
            raise HTTPException(status_code=400, detail="AgentSpace 未绑定 Companion")

        c = s.get(Companion, int(sp.companion_id))
        if c is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        if _companion_display_status(c) != "active" or not (c.auth_token or "").strip():
            raise HTTPException(status_code=400, detail="Companion 未认证/不可用")

        root_path = str(sp.root_path or "")
        cid = int(c.id)

    conn = COMPANION_GRPC.registry.get(cid)
    if conn is None:
        raise HTTPException(status_code=502, detail="Companion 未在线（无可用 gRPC 长连接）")

    try:
        res = await conn.request_files_read(root_path=root_path, rel_path=str(path or ""), max_bytes=256 * 1024)
    except CompanionGrpcError as e:
        raise _map_companion_files_error(e)

    return {
        "ok": True,
        "path": res.path,
        "content": res.content,
        "truncated": bool(res.truncated),
        "mime": res.mime,
    }


@app.get("/api/agent_spaces/{space_id}/files/raw")
async def agent_space_files_raw(space_id: int, path: str) -> Response:
    with session_scope() as s:
        sp = s.get(AgentSpace, int(space_id))
        if sp is None:
            raise HTTPException(status_code=404, detail="AgentSpace not found")
        if not sp.companion_id:
            raise HTTPException(status_code=400, detail="AgentSpace 未绑定 Companion")

        c = s.get(Companion, int(sp.companion_id))
        if c is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        if _companion_display_status(c) != "active" or not (c.auth_token or "").strip():
            raise HTTPException(status_code=400, detail="Companion 未认证/不可用")

        root_path = str(sp.root_path or "")
        cid = int(c.id)

    conn = COMPANION_GRPC.registry.get(cid)
    if conn is None:
        raise HTTPException(status_code=502, detail="Companion 未在线（无可用 gRPC 长连接）")

    try:
        res = await conn.request_files_raw(root_path=root_path, rel_path=str(path or ""), max_bytes=2 * 1024 * 1024)
    except CompanionGrpcError as e:
        raise _map_companion_files_error(e)

    return Response(content=bytes(res.data), media_type=(res.mime or "application/octet-stream"))
