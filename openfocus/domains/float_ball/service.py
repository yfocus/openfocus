# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json
import re
import secrets
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ...companion.grpc import (
    CompanionGrpcError,
    add_companion_connected_listener,
    add_float_ball_action_listener,
)
from ...db import session_scope
from ...models import Companion, SystemInboxTarget
from ..agent_activity import service as agent_activity_service
from ..companion import service as companion_service
from ..events import service as event_service

BROWSER_SESSION_COOKIE = "openfocus_browser_session"
SESSION_COOKIE_MAX_AGE_SECONDS = 365 * 24 * 60 * 60
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
SYSTEM_FLOAT_BALL_CAPABILITY = "system_float_ball"
SYSTEM_INBOX_TARGET_ID = 1
SYSTEM_INBOX_SETTINGS_URL = "/companions?system_inbox=1"
_LISTENER_INSTALLED = False


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_browser_session_id() -> str:
    return secrets.token_urlsafe(32)


def valid_browser_session_id(value: str | None) -> str:
    raw = str(value or "").strip()
    return raw if SESSION_ID_RE.match(raw) else ""


def _has_system_float_ball_capability(capabilities: Any) -> bool:
    for value in list(capabilities or []):
        cap = str(value or "").strip()
        if cap == SYSTEM_FLOAT_BALL_CAPABILITY or cap.startswith(
            f"{SYSTEM_FLOAT_BALL_CAPABILITY}."
        ):
            return True
    return False


def _companion_payload(companion: Any | None, conn: Any | None = None) -> dict:
    if companion is None:
        return {}
    caps = list(getattr(conn, "capabilities", []) or []) if conn is not None else []
    return {
        "id": int(getattr(companion, "id", 0) or 0),
        "device_id": str(getattr(companion, "device_id", "") or ""),
        "name": str(getattr(companion, "name", "") or ""),
        "status": str(getattr(companion, "status", "") or ""),
        "capabilities": caps,
    }


def _target_payload(target: Any | None) -> dict:
    if target is None:
        return {"set": False}
    return {
        "set": True,
        "companion_id": int(getattr(target, "companion_id", 0) or 0),
        "browser_session_id": str(getattr(target, "browser_session_id", "") or ""),
        "float_ball_enabled": bool(getattr(target, "float_ball_enabled", False)),
        "float_ball_base_url": str(getattr(target, "float_ball_base_url", "") or ""),
        "float_ball_backend": str(getattr(target, "float_ball_backend", "") or ""),
        "float_ball_last_error": str(
            getattr(target, "float_ball_last_error", "") or ""
        ),
    }


def _base_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    return raw or "http://127.0.0.1:8000"


def _remember_float_ball_state(
    s: Session,
    *,
    enabled: bool,
    browser_session_id: str = "",
    openfocus_base_url: str = "",
    backend: str = "",
    error: str = "",
) -> None:
    target = s.get(SystemInboxTarget, SYSTEM_INBOX_TARGET_ID)
    if target is None:
        return
    now = utcnow()
    sid = valid_browser_session_id(browser_session_id)
    if sid:
        target.browser_session_id = sid
    target.float_ball_enabled = bool(enabled)
    if openfocus_base_url:
        target.float_ball_base_url = _base_url(openfocus_base_url)
    if backend:
        target.float_ball_backend = str(backend or "").strip()
    target.float_ball_last_error = str(error or "")[:4000]
    if enabled and not error:
        target.float_ball_last_started_at = now
    target.updated_at = now
    s.add(target)


def _copy_target(target: SystemInboxTarget | None) -> SimpleNamespace | None:
    if target is None:
        return None
    return SimpleNamespace(
        id=target.id,
        companion_id=target.companion_id,
        browser_session_id=target.browser_session_id,
        float_ball_enabled=target.float_ball_enabled,
        float_ball_base_url=target.float_ball_base_url,
        float_ball_backend=target.float_ball_backend,
        float_ball_last_started_at=target.float_ball_last_started_at,
        float_ball_last_error=target.float_ball_last_error,
        created_at=target.created_at,
        updated_at=target.updated_at,
    )


def _copy_companion(companion: Companion | None) -> SimpleNamespace | None:
    if companion is None:
        return None
    return SimpleNamespace(
        id=companion.id,
        device_id=companion.device_id,
        name=companion.name,
        base_url=companion.base_url,
        status=companion.status,
        auth_token=companion.auth_token,
        last_seen_at=companion.last_seen_at,
    )


def _target_connection(
    grpc_server: Any,
) -> tuple[SimpleNamespace | None, SimpleNamespace | None, Any | None, str]:
    with session_scope() as s:
        target = s.get(SystemInboxTarget, SYSTEM_INBOX_TARGET_ID)
        if target is None:
            return None, None, None, "target_required"
        companion = s.get(Companion, int(target.companion_id or 0))
        target_payload = _copy_target(target)
        companion_payload = _copy_companion(companion)

    if companion_payload is None:
        return target_payload, None, None, "target_companion_missing"
    status = companion_service.display_status(companion_payload, grpc_server)
    if status != companion_service.COMPANION_STATUS_ACTIVE:
        return target_payload, companion_payload, None, "target_companion_offline"
    conn = grpc_server.registry.get(int(companion_payload.id or 0))
    if conn is None:
        return target_payload, companion_payload, None, "target_companion_offline"
    if not _has_system_float_ball_capability(getattr(conn, "capabilities", []) or []):
        return target_payload, companion_payload, conn, "unsupported_capability"
    return target_payload, companion_payload, conn, "ready"


def target_payload(grpc_server: Any) -> dict:
    target, companion, conn, reason = _target_connection(grpc_server)
    return {
        "ok": True,
        "reason": reason,
        "target": _target_payload(target),
        "companion": _companion_payload(companion, conn),
        "settings_url": SYSTEM_INBOX_SETTINGS_URL,
    }


def set_target(grpc_server: Any, *, companion_id: int) -> dict:
    cid = int(companion_id or 0)
    if cid <= 0:
        raise HTTPException(status_code=400, detail="companion_id is required")
    conn = grpc_server.registry.get(cid)
    with session_scope() as s:
        companion = s.get(Companion, cid)
        if companion is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        if companion_service.display_status(companion, grpc_server) != (
            companion_service.COMPANION_STATUS_ACTIVE
        ):
            raise HTTPException(status_code=400, detail="Companion is not active")
        if conn is None:
            raise HTTPException(
                status_code=502, detail="Companion is not online (no gRPC connection)"
            )
        if not _has_system_float_ball_capability(
            getattr(conn, "capabilities", []) or []
        ):
            raise HTTPException(
                status_code=400, detail="Companion does not support system inbox"
            )

        now = utcnow()
        target = s.get(SystemInboxTarget, SYSTEM_INBOX_TARGET_ID)
        previous_companion_id = int(target.companion_id or 0) if target else 0
        if target is None:
            target = SystemInboxTarget(
                id=SYSTEM_INBOX_TARGET_ID,
                companion_id=cid,
                created_at=now,
                updated_at=now,
            )
        else:
            target.companion_id = cid
            if previous_companion_id != cid:
                target.browser_session_id = ""
                target.float_ball_enabled = False
                target.float_ball_base_url = ""
                target.float_ball_backend = ""
                target.float_ball_last_error = ""
                target.float_ball_last_started_at = None
            target.updated_at = now
        s.add(target)
        event_service.record_event(
            s,
            kind="float_ball.target_set",
            agent="openfocus/system",
            task_id=None,
            payload={
                "previous_companion_id": previous_companion_id or None,
                "companion_id": cid,
            },
            audit=False,
        )
    return target_payload(grpc_server)


async def clear_target(grpc_server: Any) -> dict:
    previous_companion_id = 0
    stop_browser_session_id = ""
    should_stop = False
    with session_scope() as s:
        target = s.get(SystemInboxTarget, SYSTEM_INBOX_TARGET_ID)
        previous_companion_id = int(target.companion_id or 0) if target else 0
        if target is not None:
            stop_browser_session_id = valid_browser_session_id(
                str(target.browser_session_id or "")
            )
            should_stop = bool(target.float_ball_enabled and stop_browser_session_id)
            s.delete(target)

    stopped = False
    stop_error = ""
    if should_stop and previous_companion_id > 0:
        conn = grpc_server.registry.get(previous_companion_id)
        if conn is not None:
            try:
                await conn.request_float_ball_stop(
                    browser_session_id=stop_browser_session_id,
                    timeout_seconds=5.0,
                )
                stopped = True
            except Exception as exc:
                stop_error = str(exc)
        else:
            stop_error = "target_companion_offline"

    with session_scope() as s:
        event_service.record_event(
            s,
            kind="float_ball.target_cleared",
            agent="openfocus/system",
            task_id=None,
            payload={
                "previous_companion_id": previous_companion_id or None,
                "browser_session_id": stop_browser_session_id,
                "stop_requested": should_stop,
                "stopped": stopped,
                "stop_error": stop_error,
            },
            audit=False,
        )
    return {
        "ok": True,
        "reason": "target_required",
        "target": {"set": False},
        "stop_requested": should_stop,
        "stopped": stopped,
        "stop_error": stop_error,
    }


def preflight_payload(grpc_server: Any, *, browser_session_id: str) -> dict:
    target, companion, conn, reason = _target_connection(grpc_server)
    mode = "system" if reason == "ready" else "web"
    return {
        "ok": True,
        "mode": mode,
        "reason": reason,
        "bound": target is not None,
        "target": _target_payload(target),
        "companion": _companion_payload(companion, conn),
        "settings_url": SYSTEM_INBOX_SETTINGS_URL,
    }


def _summary_json() -> str:
    with session_scope() as s:
        payload = agent_activity_service.summary_payload(s, limit=30)
    return json.dumps(payload, ensure_ascii=False)


async def start_float_ball(
    grpc_server: Any,
    *,
    browser_session_id: str,
    openfocus_base_url: str,
) -> dict:
    target, companion, conn, reason = _target_connection(grpc_server)
    if reason == "target_required":
        return {
            "ok": False,
            "mode": "target_required",
            "reason": reason,
            "settings_url": SYSTEM_INBOX_SETTINGS_URL,
            "target": _target_payload(target),
            "companion": {},
        }
    if reason != "ready" or conn is None:
        return {
            "ok": False,
            "mode": "web",
            "reason": reason,
            "bound": target is not None,
            "target": _target_payload(target),
            "companion": _companion_payload(companion, conn),
            "settings_url": SYSTEM_INBOX_SETTINGS_URL,
        }
    try:
        res = await conn.request_float_ball_start(
            browser_session_id=browser_session_id,
            openfocus_base_url=_base_url(openfocus_base_url),
            summary_json=_summary_json(),
            timeout_seconds=10.0,
        )
    except CompanionGrpcError as exc:
        return {
            "ok": False,
            "mode": "web",
            "reason": "grpc_error",
            "error": str(exc),
            "target": _target_payload(target),
            "companion": _companion_payload(companion, conn),
        }
    backend = str(getattr(res, "backend", "") or "")
    with session_scope() as s:
        _remember_float_ball_state(
            s,
            browser_session_id=browser_session_id,
            enabled=True,
            openfocus_base_url=openfocus_base_url,
            backend=backend,
        )
        event_service.record_event(
            s,
            kind="float_ball.started",
            agent="openfocus/system",
            task_id=None,
            payload={
                "browser_session_id": browser_session_id,
                "companion_id": int(getattr(companion, "id", 0) or 0),
                "backend": backend,
            },
            audit=False,
        )
    return {
        "ok": True,
        "mode": "system",
        "reason": "started",
        "backend": backend,
        "target": _target_payload(target),
        "companion": _companion_payload(companion, conn),
    }


async def stop_float_ball(grpc_server: Any, *, browser_session_id: str) -> dict:
    target, companion, conn, reason = _target_connection(grpc_server)
    stop_browser_session_id = valid_browser_session_id(
        str(getattr(target, "browser_session_id", "") or "")
    ) or valid_browser_session_id(browser_session_id)
    with session_scope() as s:
        _remember_float_ball_state(
            s, browser_session_id=stop_browser_session_id, enabled=False
        )
    if reason != "ready" or conn is None:
        return {"ok": True, "mode": "web", "reason": reason}
    try:
        await conn.request_float_ball_stop(
            browser_session_id=stop_browser_session_id, timeout_seconds=5.0
        )
    except CompanionGrpcError as exc:
        return {"ok": False, "mode": "web", "reason": "grpc_error", "error": str(exc)}
    with session_scope() as s:
        event_service.record_event(
            s,
            kind="float_ball.stopped",
            agent="openfocus/system",
            task_id=None,
            payload={
                "browser_session_id": stop_browser_session_id,
                "companion_id": int(getattr(companion, "id", 0) or 0),
            },
            audit=False,
        )
    return {"ok": True, "mode": "system", "reason": "stopped"}


async def restore_desired_float_balls_for_companion(
    *, companion_id: int, conn: Any
) -> int:
    cid = int(companion_id or 0)
    if cid <= 0 or conn is None:
        return 0
    if not _has_system_float_ball_capability(getattr(conn, "capabilities", []) or []):
        return 0

    with session_scope() as s:
        companion = s.get(Companion, cid)
        if companion is None:
            return 0
        if not (companion.auth_token or "").strip():
            return 0
        if str(companion.status or "") != "active":
            return 0
        target = s.get(SystemInboxTarget, SYSTEM_INBOX_TARGET_ID)
        if (
            target is None
            or int(target.companion_id or 0) != cid
            or not bool(target.float_ball_enabled)
        ):
            return 0
        browser_session_id = str(target.browser_session_id or "")
        openfocus_base_url = str(target.float_ball_base_url or "")

    sid = valid_browser_session_id(browser_session_id)
    base_url = _base_url(openfocus_base_url)
    if not sid:
        return 0
    try:
        res = await conn.request_float_ball_start(
            browser_session_id=sid,
            openfocus_base_url=base_url,
            summary_json=_summary_json(),
            timeout_seconds=10.0,
        )
        backend = str(getattr(res, "backend", "") or "")
    except Exception as exc:
        with session_scope() as s:
            _remember_float_ball_state(
                s,
                browser_session_id=sid,
                enabled=True,
                error=str(exc),
            )
            event_service.record_event(
                s,
                kind="float_ball.restore_failed",
                agent="openfocus/system",
                task_id=None,
                payload={
                    "browser_session_id": sid,
                    "companion_id": cid,
                    "error": str(exc),
                },
                audit=False,
            )
        return 0

    with session_scope() as s:
        _remember_float_ball_state(
            s,
            browser_session_id=sid,
            enabled=True,
            openfocus_base_url=base_url,
            backend=backend,
        )
        event_service.record_event(
            s,
            kind="float_ball.restored",
            agent="openfocus/system",
            task_id=None,
            payload={
                "browser_session_id": sid,
                "companion_id": cid,
                "backend": backend,
            },
            audit=False,
        )
    return 1


def record_float_ball_action(
    *, browser_session_id: str, action: str, payload: dict
) -> dict:
    action = str(action or "").strip()
    if not action:
        raise HTTPException(status_code=400, detail="action is required")
    with session_scope() as s:
        event_service.record_event(
            s,
            kind="float_ball.action",
            agent="openfocus/companion",
            task_id=None,
            payload={
                "browser_session_id": valid_browser_session_id(browser_session_id),
                "action": action,
                "payload": payload if isinstance(payload, dict) else {},
            },
            audit=False,
        )
    return {"ok": True}


def handle_float_ball_action(action_msg: Any) -> None:
    try:
        raw = str(getattr(action_msg, "payload_json", "") or "{}")
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            payload = {}
        record_float_ball_action(
            browser_session_id=str(getattr(action_msg, "browser_session_id", "") or ""),
            action=str(getattr(action_msg, "action", "") or ""),
            payload=payload,
        )
    except Exception:
        return


def handle_companion_connected(companion_id: int, conn: Any) -> Any:
    return restore_desired_float_balls_for_companion(
        companion_id=int(companion_id or 0), conn=conn
    )


def install_float_ball_listeners_once() -> None:
    global _LISTENER_INSTALLED
    if _LISTENER_INSTALLED:
        return
    add_float_ball_action_listener(handle_float_ball_action)
    add_companion_connected_listener(handle_companion_connected)
    _LISTENER_INSTALLED = True
