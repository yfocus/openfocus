# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...db import session_scope
from ...domains.events import service as event_service
from ...schemas import AgentEventIn, FocusReportIn

router = APIRouter()


@router.post("/api/agent/events")
def agent_report_event(payload: AgentEventIn) -> dict:
    """Persist external agent progress without changing task completion state."""

    with session_scope() as s:
        return event_service.report_agent_event(
            s,
            kind=payload.kind,
            agent=payload.agent,
            task_id=payload.task_id,
            payload=payload.payload,
        )


@router.get("/api/events/recent")
def recent_events(limit: int = 30) -> dict:
    with session_scope() as s:
        return event_service.recent_events_payload(s, limit=limit)


@router.get("/api/calendar/month")
def calendar_month(ym: str | None = None) -> dict:
    try:
        with session_scope() as s:
            return event_service.calendar_month_payload(s, ym=ym)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/skills/focus_report")
def focus_report(report: FocusReportIn) -> dict:
    """Persist focus_report skill output without auto-completing tasks."""

    with session_scope() as s:
        return event_service.report_focus_result(s, report)
