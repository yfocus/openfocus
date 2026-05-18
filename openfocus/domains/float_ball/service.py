# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import secrets
import urllib.parse
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ...companion.grpc import (
    CompanionGrpcError,
    add_browser_bind_proof_listener,
    add_float_ball_action_listener,
)
from ...db import session_scope
from ...models import BrowserBindChallenge, BrowserCompanionBinding, Companion
from ..agent_activity import service as agent_activity_service
from ..companion import service as companion_service
from ..events import service as event_service

BROWSER_SESSION_COOKIE = "openfocus_browser_session"
SESSION_COOKIE_MAX_AGE_SECONDS = 365 * 24 * 60 * 60
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
SYSTEM_FLOAT_BALL_CAPABILITY = "system_float_ball"
BIND_CHALLENGE_TTL_SECONDS = 90
_BIND_LISTENER_INSTALLED = False


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(ts: dt.datetime | None) -> dt.datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=dt.timezone.utc)
    return ts


def new_browser_session_id() -> str:
    return secrets.token_urlsafe(32)


def valid_browser_session_id(value: str | None) -> str:
    raw = str(value or "").strip()
    return raw if SESSION_ID_RE.match(raw) else ""


def nonce_hash(nonce: str) -> str:
    return hashlib.sha256(str(nonce or "").encode("utf-8")).hexdigest()


def _has_system_float_ball_capability(capabilities: Any) -> bool:
    for value in list(capabilities or []):
        cap = str(value or "").strip()
        if cap == SYSTEM_FLOAT_BALL_CAPABILITY or cap.startswith(
            f"{SYSTEM_FLOAT_BALL_CAPABILITY}."
        ):
            return True
    return False


def _companion_payload(companion: Companion | None, conn: Any | None = None) -> dict:
    if companion is None:
        return {}
    caps = list(getattr(conn, "capabilities", []) or []) if conn is not None else []
    return {
        "id": int(companion.id or 0),
        "device_id": str(companion.device_id or ""),
        "name": str(companion.name or ""),
        "status": str(companion.status or ""),
        "capabilities": caps,
    }


def _base_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    return raw or "http://127.0.0.1:8000"


def _is_loopback_host(value: str | None) -> bool:
    raw = str(value or "").strip().strip("[]").lower()
    if not raw:
        return False
    if raw == "localhost" or raw.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


def _is_loopback_request(*, openfocus_base_url: str, client_host: str | None) -> bool:
    try:
        parsed = urllib.parse.urlparse(_base_url(openfocus_base_url))
        base_host = parsed.hostname or ""
    except Exception:
        base_host = ""
    return _is_loopback_host(base_host) and _is_loopback_host(client_host)


def _registry_connections(grpc_server: Any) -> list[tuple[int, Any]]:
    registry = getattr(grpc_server, "registry", None)
    by_id = getattr(registry, "_by_companion_id", None)
    if isinstance(by_id, dict):
        return [(int(cid), conn) for cid, conn in list(by_id.items())]
    conn = getattr(registry, "conn", None)
    if conn is not None:
        with session_scope() as s:
            companions = (
                s.query(Companion)
                .filter(Companion.status == "active")
                .order_by(Companion.id.asc())
                .all()
            )
            if len(companions) == 1:
                return [(int(companions[0].id), conn)]
    return []


def _auto_bind_single_loopback_companion(
    grpc_server: Any,
    *,
    browser_session_id: str,
    openfocus_base_url: str,
    client_host: str | None,
) -> bool:
    browser_session_id = valid_browser_session_id(browser_session_id)
    if not browser_session_id or not _is_loopback_request(
        openfocus_base_url=openfocus_base_url, client_host=client_host
    ):
        return False

    online = _registry_connections(grpc_server)
    if not online:
        return False

    now = utcnow()
    candidates: list[int] = []
    with session_scope() as s:
        for cid, conn in online:
            if not _has_system_float_ball_capability(
                getattr(conn, "capabilities", []) or []
            ):
                continue
            companion = s.get(Companion, int(cid))
            if companion is None:
                continue
            if not (companion.auth_token or "").strip():
                continue
            if str(companion.status or "") != "active":
                continue
            candidates.append(int(cid))

        # Local loopback is only used as a convenience fallback when selection is
        # unambiguous. Multiple online capable companions must still use the
        # explicit openfocus:// nonce proof to avoid binding the wrong machine.
        if len(candidates) != 1:
            return False

        cid = candidates[0]
        binding = (
            s.query(BrowserCompanionBinding)
            .filter(BrowserCompanionBinding.browser_session_id == browser_session_id)
            .one_or_none()
        )
        previous_companion_id = int(binding.companion_id or 0) if binding else 0
        if binding is None:
            binding = BrowserCompanionBinding(
                browser_session_id=browser_session_id,
                companion_id=cid,
                trust_method="loopback_auto",
                created_at=now,
                last_verified_at=now,
                updated_at=now,
            )
        else:
            binding.companion_id = cid
            binding.trust_method = "loopback_auto"
            binding.last_verified_at = now
            binding.updated_at = now
        s.add(binding)

        if previous_companion_id != cid:
            event_service.record_event(
                s,
                kind="float_ball.browser_bound",
                agent="openfocus/system",
                task_id=None,
                payload={
                    "browser_session_id": browser_session_id,
                    "companion_id": cid,
                    "trust_method": "loopback_auto",
                },
                audit=False,
            )
    return True


def _challenge_payload(challenge: BrowserBindChallenge, *, nonce: str = "") -> dict:
    payload = {
        "nonce": nonce,
        "status": str(challenge.status or "pending"),
        "expires_at": _aware(challenge.expires_at).isoformat()
        if challenge.expires_at
        else None,
        "companion_id": int(challenge.companion_id or 0) or None,
    }
    return payload


def create_bind_challenge(
    s: Session,
    *,
    browser_session_id: str,
    openfocus_base_url: str,
) -> dict:
    browser_session_id = valid_browser_session_id(browser_session_id)
    if not browser_session_id:
        raise ValueError("browser_session_id is required")
    now = utcnow()
    nonce = secrets.token_urlsafe(32)
    challenge = BrowserBindChallenge(
        nonce_hash=nonce_hash(nonce),
        browser_session_id=browser_session_id,
        status="pending",
        created_at=now,
        expires_at=now + dt.timedelta(seconds=BIND_CHALLENGE_TTL_SECONDS),
        updated_at=now,
    )
    s.add(challenge)
    s.flush()

    query = urllib.parse.urlencode(
        {
            "server": _base_url(openfocus_base_url),
            "nonce": nonce,
            "browser_session_id": browser_session_id,
            "instance_id": _safe_instance_id(os.environ.get("OPENFOCUS_INSTANCE_ID")),
        }
    )
    return {
        "ok": True,
        "mode": "bind_required",
        "reason": "browser_not_bound",
        "bind": {
            **_challenge_payload(challenge, nonce=nonce),
            "open_url": f"openfocus://bind?{query}",
            "poll_url": f"/api/float_ball/bind_status?nonce={urllib.parse.quote(nonce)}",
            "ttl_seconds": BIND_CHALLENGE_TTL_SECONDS,
        },
    }


def bind_status(*, nonce: str) -> dict:
    raw_nonce = str(nonce or "").strip()
    if not raw_nonce:
        raise HTTPException(status_code=400, detail="nonce is required")
    h = nonce_hash(raw_nonce)
    now = utcnow()
    with session_scope() as s:
        challenge = (
            s.query(BrowserBindChallenge)
            .filter(BrowserBindChallenge.nonce_hash == h)
            .one_or_none()
        )
        if challenge is None:
            return {"ok": True, "status": "missing", "mode": "bind_required"}
        exp = _aware(challenge.expires_at)
        if str(challenge.status or "") == "pending" and exp and exp <= now:
            challenge.status = "expired"
            challenge.updated_at = now
            s.add(challenge)
        return {"ok": True, **_challenge_payload(challenge)}


def confirm_browser_bind_nonce(*, nonce: str, companion_id: int) -> dict:
    raw_nonce = str(nonce or "").strip()
    cid = int(companion_id or 0)
    if not raw_nonce or cid <= 0:
        raise ValueError("nonce and companion_id are required")
    h = nonce_hash(raw_nonce)
    now = utcnow()
    with session_scope() as s:
        challenge = (
            s.query(BrowserBindChallenge)
            .filter(BrowserBindChallenge.nonce_hash == h)
            .one_or_none()
        )
        if challenge is None:
            raise LookupError("bind challenge not found")
        exp = _aware(challenge.expires_at)
        if exp and exp <= now:
            challenge.status = "expired"
            challenge.updated_at = now
            s.add(challenge)
            raise ValueError("bind challenge expired")
        companion = s.get(Companion, cid)
        if companion is None:
            raise LookupError("Companion not found")
        if (
            not (companion.auth_token or "").strip()
            or str(companion.status or "") != "active"
        ):
            raise ValueError("Companion is not paired")

        challenge.status = "confirmed"
        challenge.companion_id = cid
        challenge.confirmed_at = now
        challenge.updated_at = now
        s.add(challenge)

        binding = (
            s.query(BrowserCompanionBinding)
            .filter(
                BrowserCompanionBinding.browser_session_id
                == challenge.browser_session_id
            )
            .one_or_none()
        )
        previous_companion_id = int(binding.companion_id or 0) if binding else 0
        if binding is None:
            binding = BrowserCompanionBinding(
                browser_session_id=challenge.browser_session_id,
                companion_id=cid,
                trust_method="nonce_protocol",
                created_at=now,
                last_verified_at=now,
                updated_at=now,
            )
        else:
            binding.companion_id = cid
            binding.trust_method = "nonce_protocol"
            binding.last_verified_at = now
            binding.updated_at = now
        s.add(binding)

        if previous_companion_id != cid:
            event_service.record_event(
                s,
                kind="float_ball.browser_bound",
                agent="openfocus/system",
                task_id=None,
                payload={
                    "browser_session_id": challenge.browser_session_id,
                    "companion_id": cid,
                    "trust_method": "nonce_protocol",
                },
                audit=False,
            )
        return {
            "ok": True,
            "browser_session_id": challenge.browser_session_id,
            "companion_id": cid,
        }


def _safe_instance_id(value: str | None) -> str:
    raw = str(value or "").strip() or "default"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-._")
    return safe or "default"


def handle_browser_bind_proof(proof: Any) -> None:
    try:
        confirm_browser_bind_nonce(
            nonce=str(getattr(proof, "nonce", "") or ""),
            companion_id=int(getattr(proof, "companion_id", 0) or 0),
        )
    except Exception:
        return


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


def install_browser_bind_listener_once() -> None:
    global _BIND_LISTENER_INSTALLED
    if _BIND_LISTENER_INSTALLED:
        return
    add_browser_bind_proof_listener(handle_browser_bind_proof)
    add_float_ball_action_listener(handle_float_ball_action)
    _BIND_LISTENER_INSTALLED = True


def _bound_connection(
    grpc_server: Any, *, browser_session_id: str
) -> tuple[BrowserCompanionBinding | None, Companion | None, Any | None, str]:
    browser_session_id = valid_browser_session_id(browser_session_id)
    if not browser_session_id:
        return None, None, None, "missing_browser_session"
    with session_scope() as s:
        binding = (
            s.query(BrowserCompanionBinding)
            .filter(BrowserCompanionBinding.browser_session_id == browser_session_id)
            .one_or_none()
        )
        if binding is None:
            return None, None, None, "browser_not_bound"
        companion = s.get(Companion, int(binding.companion_id or 0))
        if companion is None:
            return binding, None, None, "companion_missing"
        companion_payload = SimpleNamespace(
            id=companion.id,
            device_id=companion.device_id,
            name=companion.name,
            base_url=companion.base_url,
            status=companion.status,
            auth_token=companion.auth_token,
            last_seen_at=companion.last_seen_at,
        )
        binding_payload = SimpleNamespace(
            browser_session_id=binding.browser_session_id,
            companion_id=binding.companion_id,
            trust_method=binding.trust_method,
            created_at=binding.created_at,
            last_verified_at=binding.last_verified_at,
            updated_at=binding.updated_at,
        )
    status = companion_service.display_status(companion_payload, grpc_server)
    if status != companion_service.COMPANION_STATUS_ACTIVE:
        return binding_payload, companion_payload, None, "companion_offline"
    conn = grpc_server.registry.get(int(companion_payload.id or 0))
    if conn is None:
        return binding_payload, companion_payload, None, "companion_offline"
    if not _has_system_float_ball_capability(getattr(conn, "capabilities", []) or []):
        return binding_payload, companion_payload, conn, "unsupported_capability"
    return binding_payload, companion_payload, conn, "ready"


def preflight_payload(grpc_server: Any, *, browser_session_id: str) -> dict:
    binding, companion, conn, reason = _bound_connection(
        grpc_server, browser_session_id=browser_session_id
    )
    mode = "system" if reason == "ready" else "web"
    return {
        "ok": True,
        "mode": mode,
        "reason": reason,
        "bound": binding is not None,
        "companion": _companion_payload(companion, conn),
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
    client_host: str | None = None,
) -> dict:
    binding, companion, conn, reason = _bound_connection(
        grpc_server, browser_session_id=browser_session_id
    )
    if reason == "browser_not_bound" and _auto_bind_single_loopback_companion(
        grpc_server,
        browser_session_id=browser_session_id,
        openfocus_base_url=openfocus_base_url,
        client_host=client_host,
    ):
        binding, companion, conn, reason = _bound_connection(
            grpc_server, browser_session_id=browser_session_id
        )
    if reason in {
        "missing_browser_session",
        "browser_not_bound",
        "companion_missing",
        "companion_offline",
    }:
        with session_scope() as s:
            challenge = create_bind_challenge(
                s,
                browser_session_id=browser_session_id,
                openfocus_base_url=openfocus_base_url,
            )
        challenge["reason"] = reason
        return challenge
    if reason != "ready" or conn is None:
        return {
            "ok": False,
            "mode": "web",
            "reason": reason,
            "bound": binding is not None,
            "companion": _companion_payload(companion, conn),
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
            "companion": _companion_payload(companion, conn),
        }
    backend = str(getattr(res, "backend", "") or "")
    with session_scope() as s:
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
        "companion": _companion_payload(companion, conn),
    }


async def stop_float_ball(grpc_server: Any, *, browser_session_id: str) -> dict:
    _binding, companion, conn, reason = _bound_connection(
        grpc_server, browser_session_id=browser_session_id
    )
    if reason != "ready" or conn is None:
        return {"ok": True, "mode": "web", "reason": reason}
    try:
        await conn.request_float_ball_stop(
            browser_session_id=browser_session_id, timeout_seconds=5.0
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
                "browser_session_id": browser_session_id,
                "companion_id": int(getattr(companion, "id", 0) or 0),
            },
            audit=False,
        )
    return {"ok": True, "mode": "system", "reason": "stopped"}


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
