# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ...models import (
    AgentRuntimeSession,
    AgentSession,
    AgentSpace,
    AgentTurn,
    Goal,
    RemoteTerminalSession,
    Task,
    TaskAgentActivity,
)
from ..events import service as event_service

RUNNING_STATE = "running"
WAITING_STATE = "waiting"
REVIEW_READY_STATE = "review_ready"
FAILED_STATE = "failed"
CANCELED_STATE = "canceled"
STALE_STATE = "stale"

ACTIVE_TURN_STATES = {RUNNING_STATE, WAITING_STATE}
VISIBLE_ACTIVITY_STATES = {
    RUNNING_STATE,
    WAITING_STATE,
    REVIEW_READY_STATE,
    FAILED_STATE,
    CANCELED_STATE,
    STALE_STATE,
}
WAITING_KINDS = {
    "runtime.turn.waiting_for_approval",
    "runtime.turn.waiting_for_input",
    "runtime.turn.waiting_for_confirmation",
}

RAW_KIND_MAP: dict[str, str] = {
    "session-start": "runtime.session.started",
    "session_start": "runtime.session.started",
    "SessionStart": "runtime.session.started",
    "session-end": "runtime.session.ended",
    "session_end": "runtime.session.ended",
    "SessionEnd": "runtime.session.ended",
    "user-prompt-submit": "runtime.turn.submitted",
    "user_prompt_submit": "runtime.turn.submitted",
    "UserPromptSubmit": "runtime.turn.submitted",
    "permission-request": "runtime.turn.waiting_for_approval",
    "permission_request": "runtime.turn.waiting_for_approval",
    "PermissionRequest": "runtime.turn.waiting_for_approval",
    "pre-tool-use": "runtime.turn.activity",
    "pre_tool_use": "runtime.turn.activity",
    "PreToolUse": "runtime.turn.activity",
    "post-tool-use": "runtime.turn.activity",
    "post_tool_use": "runtime.turn.activity",
    "PostToolUse": "runtime.turn.activity",
    "post-tool-use-failure": "runtime.turn.activity",
    "post_tool_use_failure": "runtime.turn.activity",
    "PostToolUseFailure": "runtime.turn.activity",
    "turn/started": "runtime.turn.started",
    "TurnStartedNotification": "runtime.turn.started",
    "turn_started": "runtime.turn.started",
    "task_started": "runtime.turn.started",
    "turn/completed": "runtime.turn.completed",
    "TurnCompletedNotification": "runtime.turn.completed",
    "turn_complete": "runtime.turn.completed",
    "task_complete": "runtime.turn.completed",
    "turn-ended": "runtime.turn.completed",
    "stop": "runtime.turn.completed",
    "Stop": "runtime.turn.completed",
    "item/commandExecution/requestApproval": "runtime.turn.waiting_for_approval",
    "item/fileChange/requestApproval": "runtime.turn.waiting_for_approval",
    "item/permissions/requestApproval": "runtime.turn.waiting_for_approval",
    "WaitingOnApproval": "runtime.turn.waiting_for_approval",
    "item/tool/requestUserInput": "runtime.turn.waiting_for_input",
    "WaitingOnUserInput": "runtime.turn.waiting_for_input",
    "subagent-start": "runtime.subagent.started",
    "SubagentStart": "runtime.subagent.started",
    "subagent-stop": "runtime.subagent.completed",
    "SubagentStop": "runtime.subagent.completed",
    "pre-compact": "runtime.context.compacted",
    "PreCompact": "runtime.context.compacted",
    "post-compact": "runtime.context.compacted",
    "PostCompact": "runtime.context.compacted",
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


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
    current = now or utcnow()
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return max(0, int((current - value).total_seconds()))


def _clean(value: object, *, max_len: int = 4000) -> str:
    text = str(value or "").strip()
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _payload_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _clean(value, max_len=2000)
    return ""


def normalize_kind(
    *, kind: str | None = None, raw_kind: str | None = None, payload: Any = None
) -> str:
    explicit = _clean(kind, max_len=128)
    if explicit.startswith("runtime.") or explicit.startswith("journal."):
        return explicit
    raw = _clean(raw_kind or explicit, max_len=128)
    if raw in RAW_KIND_MAP:
        return RAW_KIND_MAP[raw]
    if raw == "notification" or raw == "Notification":
        return _normalize_notification(payload if isinstance(payload, dict) else {})
    return explicit or RAW_KIND_MAP.get(raw, "journal.runtime.raw_signal")


def _normalize_notification(payload: dict[str, Any]) -> str:
    text = " ".join(
        _clean(payload.get(k), max_len=500).lower()
        for k in ("message", "summary", "title", "reason", "body")
    )
    if any(token in text for token in ("confirm", "confirmation", "manual_confirm")):
        return "runtime.turn.waiting_for_confirmation"
    if any(token in text for token in ("input", "answer", "waiting for your")):
        return "runtime.turn.waiting_for_input"
    return "journal.agent.note"


def _task_by_public_id(s: Session, task_public_id: str) -> Task | None:
    tid = _clean(task_public_id, max_len=36)
    if not tid:
        return None
    return s.query(Task).filter(Task.public_id == tid).one_or_none()


def _correlate_task_public_id(
    s: Session,
    *,
    task_public_id: str | None = None,
    session_id: str | None = None,
    terminal_id: str | None = None,
    cwd: str | None = None,
) -> str:
    direct = _clean(task_public_id, max_len=36)
    if direct and _task_by_public_id(s, direct) is not None:
        return direct

    sid = _clean(session_id, max_len=128)
    if sid:
        runtime_sess = (
            s.query(AgentRuntimeSession)
            .filter(AgentRuntimeSession.session_id == sid)
            .order_by(AgentRuntimeSession.id.desc())
            .first()
        )
        if runtime_sess is not None and _clean(runtime_sess.task_public_id, max_len=36):
            return _clean(runtime_sess.task_public_id, max_len=36)

        sess = (
            s.query(AgentSession)
            .filter(AgentSession.session_id == sid)
            .order_by(AgentSession.id.desc())
            .first()
        )
        if sess is not None and _clean(sess.task_public_id, max_len=36):
            return _clean(sess.task_public_id, max_len=36)

    tid = _clean(terminal_id, max_len=64)
    if tid:
        term = (
            s.query(RemoteTerminalSession)
            .filter(RemoteTerminalSession.terminal_id == tid)
            .order_by(RemoteTerminalSession.id.desc())
            .first()
        )
        if term is not None and _clean(term.task_public_id, max_len=36):
            return _clean(term.task_public_id, max_len=36)

    root = _clean(cwd, max_len=4000)
    if root:
        spaces = (
            s.query(AgentSpace)
            .filter(AgentSpace.root_path == root)
            .order_by(AgentSpace.id.desc())
            .limit(2)
            .all()
        )
        if len(spaces) == 1 and _clean(spaces[0].task_public_id, max_len=36):
            return _clean(spaces[0].task_public_id, max_len=36)

    return ""


def _upsert_runtime_session(
    s: Session,
    *,
    session_id: str,
    agent_runtime: str,
    task_public_id: str,
    companion_id: int | None,
    terminal_id: str,
    workspace_root: str,
    state: str,
    kind: str,
    payload: dict[str, Any],
    now: dt.datetime,
) -> AgentRuntimeSession | None:
    sid = _clean(session_id, max_len=128)
    if not sid:
        return None
    row = (
        s.query(AgentRuntimeSession)
        .filter(AgentRuntimeSession.session_id == sid)
        .one_or_none()
    )
    if row is None:
        row = AgentRuntimeSession(
            session_id=sid,
            started_at=now,
            last_seen_at=now,
            updated_at=now,
        )
        s.add(row)
    row.agent_runtime = _clean(agent_runtime, max_len=64) or row.agent_runtime
    row.task_public_id = _clean(task_public_id, max_len=36) or row.task_public_id
    row.companion_id = companion_id if companion_id is not None else row.companion_id
    row.terminal_id = _clean(terminal_id, max_len=64) or row.terminal_id
    row.workspace_root = _clean(workspace_root, max_len=4000) or row.workspace_root
    row.state = _clean(state, max_len=32) or row.state
    row.last_signal_kind = _clean(kind, max_len=128)
    row.payload = {**(row.payload or {}), "last_signal_payload": payload}
    row.last_seen_at = now
    row.updated_at = now
    if row.state in {"ended", "offline"}:
        row.ended_at = now
    else:
        row.ended_at = None
    s.flush()
    return row


def _find_active_turn(
    s: Session, *, turn_id: str, session_id: str, task_public_id: str
) -> AgentTurn | None:
    tid = _clean(turn_id, max_len=64)
    if tid:
        row = s.query(AgentTurn).filter(AgentTurn.turn_id == tid).one_or_none()
        if row is not None:
            return row

    q = s.query(AgentTurn).filter(AgentTurn.state.in_(ACTIVE_TURN_STATES))
    sid = _clean(session_id, max_len=128)
    task_id = _clean(task_public_id, max_len=36)
    if sid:
        row = (
            q.filter(AgentTurn.session_id == sid).order_by(AgentTurn.id.desc()).first()
        )
        if row is not None:
            return row
    if task_id:
        return (
            q.filter(AgentTurn.task_public_id == task_id)
            .order_by(AgentTurn.id.desc())
            .first()
        )
    return None


def _ensure_turn(
    s: Session,
    *,
    kind: str,
    turn_id: str,
    session_id: str,
    agent_runtime: str,
    task_public_id: str,
    companion_id: int | None,
    terminal_id: str,
    source: str,
    payload: dict[str, Any],
) -> AgentTurn:
    existing = _find_active_turn(
        s, turn_id=turn_id, session_id=session_id, task_public_id=task_public_id
    )
    if existing is not None:
        return existing
    tid = _clean(turn_id, max_len=64) or str(uuid.uuid4())
    row = AgentTurn(
        turn_id=tid,
        session_id=_clean(session_id, max_len=128),
        agent_runtime=_clean(agent_runtime, max_len=64),
        task_public_id=_clean(task_public_id, max_len=36),
        companion_id=companion_id,
        terminal_id=_clean(terminal_id, max_len=64),
        state=RUNNING_STATE,
        source=_clean(source, max_len=64),
        last_signal_kind=_clean(kind, max_len=128),
        summary=_payload_text(payload, "message", "summary", "reason"),
        payload=payload,
    )
    s.add(row)
    s.flush()
    return row


def _state_for_kind(kind: str) -> str | None:
    if kind in {
        "runtime.turn.submitted",
        "runtime.turn.started",
        "runtime.turn.activity",
        "runtime.turn.resumed",
    }:
        return RUNNING_STATE
    if kind in WAITING_KINDS:
        return WAITING_STATE
    if kind == "runtime.turn.completed":
        return "completed"
    if kind == "runtime.turn.failed":
        return FAILED_STATE
    if kind == "runtime.turn.canceled":
        return CANCELED_STATE
    if kind == "runtime.turn.stale":
        return STALE_STATE
    return None


def _activity_state_for_turn_state(turn_state: str) -> str:
    if turn_state == "completed":
        return REVIEW_READY_STATE
    return turn_state


def _runtime_session_state_for_turn_state(turn_state: str) -> str:
    if turn_state in ACTIVE_TURN_STATES:
        return WAITING_STATE if turn_state == WAITING_STATE else RUNNING_STATE
    return "idle"


def _severity_for_state(state: str) -> str:
    if state == RUNNING_STATE:
        return "info"
    if state == REVIEW_READY_STATE:
        return "success"
    if state == FAILED_STATE:
        return "error"
    return "warning"


def _summary_for(kind: str, state: str, payload: dict[str, Any]) -> str:
    text = _payload_text(payload, "message", "summary", "reason", "error")
    if text:
        return text
    if kind == "runtime.turn.submitted":
        return "Prompt submitted."
    if state == RUNNING_STATE:
        return "Agent turn is running."
    if state == WAITING_STATE:
        return "Agent is waiting for user input."
    if state == REVIEW_READY_STATE:
        return "Agent turn completed; waiting for review."
    if state == FAILED_STATE:
        return "Agent turn failed."
    if state == STALE_STATE:
        return "Agent turn state is stale."
    return "Agent activity updated."


def _update_activity(
    s: Session,
    *,
    turn: AgentTurn,
    task: Task,
    activity_state: str,
    kind: str,
    payload: dict[str, Any],
    now: dt.datetime,
) -> TaskAgentActivity:
    row = (
        s.query(TaskAgentActivity)
        .filter(TaskAgentActivity.task_public_id == task.public_id)
        .one_or_none()
    )
    if row is None:
        row = TaskAgentActivity(
            task_public_id=task.public_id,
            created_at=now,
            state_started_at=now,
            last_activity_at=now,
        )
        s.add(row)
    if row.state != activity_state:
        row.state_started_at = now
    row.active_turn_id = turn.turn_id
    row.session_id = turn.session_id
    row.agent_runtime = turn.agent_runtime
    row.companion_id = turn.companion_id
    row.terminal_id = turn.terminal_id
    row.state = activity_state
    row.severity = _severity_for_state(activity_state)
    row.title = task.title or ""
    row.summary = _summary_for(kind, activity_state, payload)
    row.payload = {
        "event_kind": kind,
        "turn_id": turn.turn_id,
        "turn_state": turn.state,
        "waiting_kind": kind if kind in WAITING_KINDS else "",
        "last_signal_payload": payload,
    }
    row.last_activity_at = now
    row.updated_at = now
    row.dismissed_at = None
    s.flush()
    return row


def _record_runtime_journal(
    s: Session,
    *,
    kind: str,
    agent_runtime: str,
    task_public_id: str,
    payload: dict[str, Any],
) -> None:
    if kind.startswith("journal."):
        journal_kind = kind
    else:
        journal_kind = kind
    event_service.record_event(
        s,
        kind=journal_kind,
        agent=agent_runtime or "runtime",
        task_id=task_public_id or None,
        payload=payload,
        create_attention=False,
        audit={
            "kind": f"event.{journal_kind}",
            "source": f"runtime:{agent_runtime or 'unknown'}",
            "summary": f"Runtime signal `{journal_kind}` was recorded.",
            "detail": json.dumps(payload or {}, ensure_ascii=False, indent=2),
            "task_public_id": task_public_id or None,
        },
    )


def handle_runtime_signal(
    s: Session,
    *,
    kind: str | None = None,
    raw_kind: str | None = None,
    agent_runtime: str = "",
    session_id: str = "",
    turn_id: str = "",
    task_public_id: str | None = None,
    terminal_id: str = "",
    companion_id: int | None = None,
    cwd: str = "",
    source: str = "",
    payload: dict[str, Any] | None = None,
    create_journal: bool = True,
) -> dict[str, Any]:
    payload = dict(payload or {})
    normalized = normalize_kind(kind=kind, raw_kind=raw_kind, payload=payload)
    runtime = _clean(
        agent_runtime or payload.get("agent_runtime") or "agent", max_len=64
    )
    sid = _clean(session_id or payload.get("session_id"), max_len=128)
    tid = _clean(turn_id or payload.get("turn_id"), max_len=64)
    terminal = _clean(terminal_id or payload.get("terminal_id"), max_len=64)
    task_id = _correlate_task_public_id(
        s,
        task_public_id=(
            task_public_id or payload.get("task_public_id") or payload.get("task_id")
        ),
        session_id=sid,
        terminal_id=terminal,
        cwd=cwd or _clean(payload.get("cwd"), max_len=4000),
    )
    workspace_root = cwd or _clean(payload.get("cwd"), max_len=4000)

    if create_journal:
        journal_payload = {
            **payload,
            "raw_kind": _clean(raw_kind or kind, max_len=128),
            "normalized_kind": normalized,
            "session_id": sid,
            "turn_id": tid,
            "task_public_id": task_id,
            "terminal_id": terminal,
            "source": _clean(source, max_len=64),
        }
        _record_runtime_journal(
            s,
            kind=normalized,
            agent_runtime=runtime,
            task_public_id=task_id,
            payload=journal_payload,
        )

    now = utcnow()
    if normalized in {
        "runtime.session.started",
        "runtime.session.resumed",
        "runtime.context.compacted",
        "runtime.subagent.started",
        "runtime.subagent.completed",
    }:
        if normalized in {"runtime.session.started", "runtime.session.resumed"}:
            _upsert_runtime_session(
                s,
                session_id=sid,
                agent_runtime=runtime,
                task_public_id=task_id,
                companion_id=companion_id,
                terminal_id=terminal,
                workspace_root=workspace_root,
                state="idle",
                kind=normalized,
                payload=payload,
                now=now,
            )
        return {"ok": True, "kind": normalized, "state": "ignored_for_activity"}

    if normalized in {"runtime.session.ended", "runtime.session.offline"}:
        _upsert_runtime_session(
            s,
            session_id=sid,
            agent_runtime=runtime,
            task_public_id=task_id,
            companion_id=companion_id,
            terminal_id=terminal,
            workspace_root=workspace_root,
            state="ended" if normalized == "runtime.session.ended" else "offline",
            kind=normalized,
            payload=payload,
            now=now,
        )
        return _stale_active_turns_for_session(
            s,
            session_id=sid,
            reason=normalized,
            agent_runtime=runtime,
            now=now,
        )

    next_turn_state = _state_for_kind(normalized)
    if next_turn_state is None:
        return {"ok": True, "kind": normalized, "state": "journal_only"}

    task = _task_by_public_id(s, task_id)
    if task is None:
        return {"ok": True, "kind": normalized, "state": "uncorrelated"}
    if normalized == "runtime.turn.activity":
        existing_turn = _find_active_turn(
            s, turn_id=tid, session_id=sid, task_public_id=task.public_id
        )
        if existing_turn is None:
            return {"ok": True, "kind": normalized, "state": "activity_without_turn"}

    turn = _ensure_turn(
        s,
        kind=normalized,
        turn_id=tid,
        session_id=sid,
        agent_runtime=runtime,
        task_public_id=task.public_id,
        companion_id=companion_id,
        terminal_id=terminal,
        source=source,
        payload=payload,
    )
    if turn.state != next_turn_state:
        turn.state_started_at = now
    turn.state = next_turn_state
    turn.session_id = sid or turn.session_id
    turn.agent_runtime = runtime or turn.agent_runtime
    turn.task_public_id = task.public_id
    turn.companion_id = companion_id if companion_id is not None else turn.companion_id
    turn.terminal_id = terminal or turn.terminal_id
    turn.last_signal_kind = normalized
    turn.summary = _summary_for(
        normalized, _activity_state_for_turn_state(next_turn_state), payload
    )
    turn.error = _payload_text(payload, "error")
    turn.payload = {**(turn.payload or {}), "last_signal_payload": payload}
    turn.last_activity_at = now
    turn.updated_at = now
    if next_turn_state in {"completed", FAILED_STATE, CANCELED_STATE, STALE_STATE}:
        turn.completed_at = now
    s.add(turn)
    _upsert_runtime_session(
        s,
        session_id=sid,
        agent_runtime=runtime,
        task_public_id=task.public_id,
        companion_id=companion_id,
        terminal_id=terminal,
        workspace_root=workspace_root,
        state=_runtime_session_state_for_turn_state(next_turn_state),
        kind=normalized,
        payload=payload,
        now=now,
    )

    activity = _update_activity(
        s,
        turn=turn,
        task=task,
        activity_state=_activity_state_for_turn_state(next_turn_state),
        kind=normalized,
        payload=payload,
        now=now,
    )
    return {
        "ok": True,
        "kind": normalized,
        "turn_id": turn.turn_id,
        "task_public_id": task.public_id,
        "activity_state": activity.state,
    }


def _stale_active_turns_for_session(
    s: Session, *, session_id: str, reason: str, agent_runtime: str, now: dt.datetime
) -> dict[str, Any]:
    sid = _clean(session_id, max_len=128)
    if not sid:
        return {"ok": True, "kind": reason, "state": "missing_session_id"}
    rows = (
        s.query(AgentTurn)
        .filter(AgentTurn.session_id == sid)
        .filter(AgentTurn.state.in_(ACTIVE_TURN_STATES))
        .all()
    )
    count = 0
    for turn in rows:
        task = _task_by_public_id(s, turn.task_public_id)
        if task is None:
            continue
        turn.state = STALE_STATE
        turn.last_signal_kind = reason
        turn.error = reason
        turn.completed_at = now
        turn.updated_at = now
        turn.last_activity_at = now
        _update_activity(
            s,
            turn=turn,
            task=task,
            activity_state=STALE_STATE,
            kind="runtime.turn.stale",
            payload={"reason": reason, "agent_runtime": agent_runtime},
            now=now,
        )
        count += 1
    return {"ok": True, "kind": reason, "stale_turns": count}


def dismiss_activity(s: Session, *, activity_id: int) -> dict[str, Any]:
    row = (
        s.query(TaskAgentActivity)
        .filter(TaskAgentActivity.id == int(activity_id))
        .one_or_none()
    )
    if row is None:
        raise LookupError("agent activity item not found")
    row.dismissed_at = utcnow()
    s.flush()
    return {"ok": True, "id": int(row.id or 0), "status": "dismissed"}


def _action_for_task(task_public_id: str, has_space: bool) -> dict[str, Any]:
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


def _bucket_for_state(state: str) -> str:
    if state == RUNNING_STATE:
        return "running"
    return "waiting"


def _agent_name(agent_runtime: object) -> str:
    return _clean(agent_runtime, max_len=64) or "agent"


def _serialize_activity(
    row: TaskAgentActivity,
    *,
    task: Task | None,
    goal: Goal | None,
    has_space: bool,
    now: dt.datetime,
) -> dict[str, Any]:
    task_public_id = str(row.task_public_id or (task.public_id if task else ""))
    bucket = _bucket_for_state(str(row.state or ""))
    payload = row.payload if isinstance(row.payload, dict) else {}
    waiting_kind = str(payload.get("waiting_kind") or "")
    return {
        "id": int(row.id or 0),
        "source": "agent_activity",
        "type": row.state,
        "bucket": bucket,
        "severity": row.severity,
        "status": "active",
        "title": row.title or (task.title if task else ""),
        "summary": row.summary or "",
        "task_public_id": task_public_id,
        "task_title": task.title if task else "",
        "goal_id": int(goal.id) if goal else (int(task.goal_id) if task else None),
        "goal_title": goal.title if goal else "",
        "agent_runtime": row.agent_runtime,
        "agent_name": _agent_name(row.agent_runtime),
        "session_id": row.session_id,
        "turn_id": row.active_turn_id,
        "waiting_kind": waiting_kind,
        "has_agent_space": bool(has_space),
        "action": _action_for_task(task_public_id, has_space) if task_public_id else {},
        "dismiss_url": f"/api/agent_activity/items/{int(row.id or 0)}/dismiss",
        "created_at": _ts(row.created_at),
        "state_since": _ts(row.state_started_at),
        "state_age_seconds": _age_seconds(row.state_started_at, now=now),
        "last_event_at": _ts(row.last_activity_at),
        "dismissed_at": _ts(row.dismissed_at),
    }


def summary_payload(s: Session, *, limit: int = 30) -> dict[str, Any]:
    limit = max(1, min(int(limit or 30), 100))
    rows = (
        s.query(TaskAgentActivity)
        .filter(TaskAgentActivity.state.in_(VISIBLE_ACTIVITY_STATES))
        .filter(TaskAgentActivity.dismissed_at.is_(None))
        .order_by(TaskAgentActivity.updated_at.desc(), TaskAgentActivity.id.desc())
        .limit(limit)
        .all()
    )
    task_ids = [str(row.task_public_id or "") for row in rows if row.task_public_id]
    tasks = (
        {
            t.public_id: t
            for t in s.query(Task).filter(Task.public_id.in_(task_ids)).all()
        }
        if task_ids
        else {}
    )
    goal_ids = {int(t.goal_id) for t in tasks.values() if t.goal_id}
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

    now = utcnow()
    items = [
        _serialize_activity(
            row,
            task=tasks.get(str(row.task_public_id or "")),
            goal=goals.get(int(tasks[str(row.task_public_id)].goal_id))
            if str(row.task_public_id or "") in tasks
            else None,
            has_space=str(row.task_public_id or "") in spaces,
            now=now,
        )
        for row in rows
    ]
    running = [x for x in items if x.get("bucket") == "running"]
    waiting = [x for x in items if x.get("bucket") == "waiting"]
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "counts": {
            "running": len(running),
            "waiting": len(waiting),
            "review_ready": len(
                [x for x in waiting if x.get("type") == REVIEW_READY_STATE]
            ),
        },
        "buckets": {
            "running": running,
            "waiting": waiting,
            "completed": waiting,
            "next_move": _next_move_items(s, limit=10, now=now),
        },
        "sections": {
            "running": running,
            "waiting": waiting,
            "review_ready": [x for x in waiting if x.get("type") == REVIEW_READY_STATE],
            "recent_journal": [],
        },
    }


def _next_move_items(
    s: Session, *, limit: int, now: dt.datetime
) -> list[dict[str, Any]]:
    from ...models import AttentionItem
    from ..attention import service as attention_service

    rows = (
        s.query(AttentionItem)
        .filter(AttentionItem.status == attention_service.ACTIVE_STATUS)
        .filter(AttentionItem.item_type == "next_move")
        .order_by(AttentionItem.created_at.desc(), AttentionItem.id.desc())
        .limit(max(1, min(int(limit or 10), 20)))
        .all()
    )
    if not rows:
        return []
    task_ids = [str(row.task_public_id or "") for row in rows if row.task_public_id]
    tasks = (
        {
            t.public_id: t
            for t in s.query(Task).filter(Task.public_id.in_(task_ids)).all()
        }
        if task_ids
        else {}
    )
    goal_ids = {int(t.goal_id) for t in tasks.values() if t.goal_id}
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
    return [
        attention_service._serialize_item(
            row,
            task=tasks.get(str(row.task_public_id or "")),
            goal=goals.get(int(tasks[str(row.task_public_id)].goal_id))
            if str(row.task_public_id or "") in tasks
            else None,
            has_space=str(row.task_public_id or "") in spaces,
            now=now,
        )
        for row in rows
    ]


def active_activity_count(s: Session) -> int:
    return int(
        s.query(func.count(TaskAgentActivity.id))
        .filter(TaskAgentActivity.state.in_(VISIBLE_ACTIVITY_STATES))
        .filter(TaskAgentActivity.dismissed_at.is_(None))
        .scalar()
        or 0
    )
