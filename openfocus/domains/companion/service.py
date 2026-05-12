# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import HTTPException, Response

from ...companion.grpc import CompanionGrpcError
from ...db import session_scope
from ...models import AgentSpace, Companion, Event
from .repository import CompanionAgentSpaceRepository, CompanionRepository

COMPANION_STATUS_PENDING_CERTIFICATION = "pending_certification"
COMPANION_STATUS_ACTIVE = "active"
COMPANION_STATUS_OFFLINE = "offline"
COMPANION_STATUSES = frozenset(
    {
        COMPANION_STATUS_PENDING_CERTIFICATION,
        COMPANION_STATUS_ACTIVE,
        COMPANION_STATUS_OFFLINE,
    }
)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def display_status(
    companion: Companion | None, grpc_server: Any, *, now: dt.datetime | None = None
) -> str | None:
    """Return the UI-facing companion status.

    Pairing state comes from the database; online/offline is determined by the
    control-plane gRPC registry, because an authenticated companion can be active
    in DB while its long-lived stream is currently disconnected.
    """

    if companion is None:
        return None
    if (
        companion.status or ""
    ).strip() == COMPANION_STATUS_PENDING_CERTIFICATION or not (
        companion.auth_token or ""
    ).strip():
        return COMPANION_STATUS_PENDING_CERTIFICATION

    companion_id = int(getattr(companion, "id", 0) or 0)
    online = bool(companion_id and (grpc_server.registry.get(companion_id) is not None))
    return COMPANION_STATUS_ACTIVE if online else COMPANION_STATUS_OFFLINE


def register_companion(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    device_id = str(payload.get("device_id") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not device_id or len(device_id) > 64:
        raise HTTPException(status_code=400, detail="device_id is required")
    if not base_url or len(base_url) > 1024:
        raise HTTPException(status_code=400, detail="base_url is required")

    now = utcnow()
    with session_scope() as session:
        repo = CompanionRepository(session)
        companion = repo.get_by_device_id(device_id)
        if companion is None:
            companion = Companion(device_id=device_id, base_url=base_url, name=name)
            repo.add(companion)
        else:
            companion.base_url = base_url
            if name:
                companion.name = name
        companion.last_seen_at = now
        companion.status = (
            COMPANION_STATUS_ACTIVE
            if (companion.auth_token or "").strip()
            else COMPANION_STATUS_PENDING_CERTIFICATION
        )
        session.add(companion)
        companion_id = companion.id
        status_out = companion.status

    return {"ok": True, "id": companion_id, "status": status_out}


def list_companions(grpc_server: Any, *, limit: int = 50) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    with session_scope() as session:
        companions = CompanionRepository(session).list_recent(limit=limit)
        companion_ids = [companion.id for companion in companions]
        spaces_by_companion: dict[int, list[dict]] = {
            int(companion_id): [] for companion_id in companion_ids
        }
        spaces = CompanionAgentSpaceRepository(session).list_by_companion_ids(
            companion_ids
        )
        for space in spaces:
            companion_id = int(getattr(space, "companion_id", 0) or 0)
            if companion_id in spaces_by_companion:
                spaces_by_companion[companion_id].append(
                    {"id": space.id, "task_public_id": space.task_public_id}
                )

    items: list[dict] = []
    for companion in companions:
        items.append(
            {
                "id": companion.id,
                "device_id": companion.device_id,
                "name": companion.name,
                "base_url": companion.base_url,
                "status": display_status(companion, grpc_server),
                "last_seen_at": companion.last_seen_at.isoformat()
                if companion.last_seen_at
                else None,
                "created_at": companion.created_at.isoformat()
                if getattr(companion, "created_at", None)
                else None,
                "agent_spaces": spaces_by_companion.get(companion.id, []),
            }
        )
    return {"ok": True, "items": items}


def delete_companion(grpc_server: Any, companion_id: int) -> dict:
    companion_id = int(companion_id)
    if companion_id <= 0:
        raise HTTPException(status_code=400, detail="invalid companion_id")

    try:
        conn = grpc_server.registry.get(companion_id)
        if conn is not None:
            conn.close()
    except Exception:
        pass

    with session_scope() as session:
        companion_repo = CompanionRepository(session)
        companion = companion_repo.get(companion_id)
        if companion is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        device_id = str(companion.device_id or "")

        spaces = CompanionAgentSpaceRepository(session).list_by_companion_id(
            companion_id
        )
        unbound = len(spaces)
        for space in spaces:
            space.companion_id = None
            session.add(space)

        companion_repo.delete(companion)
        session.add(
            Event(
                kind="companion.deleted",
                agent="openfocus/ui",
                task_id=None,
                payload={
                    "companion_id": companion_id,
                    "device_id": device_id,
                    "unbound_spaces": unbound,
                },
            )
        )

    return {"ok": True, "companion_id": companion_id, "unbound_spaces": unbound}


async def pair_companion(grpc_server: Any, companion_id: int, payload: dict) -> dict:
    code = str((payload.get("code") if isinstance(payload, dict) else "") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    if len(code) != 10:
        raise HTTPException(
            status_code=400, detail="Pairing code must be 10 characters"
        )

    now = utcnow()
    minute_start = now.replace(second=0, microsecond=0)

    with session_scope() as session:
        companion = CompanionRepository(session).get(int(companion_id))
        if companion is None:
            raise HTTPException(status_code=404, detail="Companion not found")

        window_start = companion.pair_attempt_window_start
        if (
            window_start is None
            or (
                window_start.replace(tzinfo=dt.timezone.utc)
                if window_start.tzinfo is None
                else window_start
            )
            != minute_start
        ):
            companion.pair_attempt_window_start = minute_start
            companion.pair_attempt_count = 0
        if companion.pair_attempt_count >= 10:
            raise HTTPException(
                status_code=429,
                detail="Pairing attempt limit reached for this minute (10 attempts)",
            )
        companion.pair_attempt_count += 1
        session.add(companion)

        device_id = companion.device_id
        session.add(
            Event(
                kind="companion.pair.attempted",
                agent="openfocus/ui",
                task_id=None,
                payload={"companion_id": companion_id, "device_id": device_id},
            )
        )

    conn = grpc_server.registry.get(int(companion_id))
    if conn is None:
        raise HTTPException(
            status_code=502, detail="Companion is not online (no gRPC connection)"
        )
    try:
        token = await conn.request_pair(code, timeout_seconds=10.0)
    except CompanionGrpcError as exc:
        raise HTTPException(status_code=502, detail=f"Companion pairing failed: {exc}")

    with session_scope() as session:
        companion = CompanionRepository(session).get(int(companion_id))
        if companion is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        companion.auth_token = token
        companion.status = COMPANION_STATUS_ACTIVE
        companion.last_seen_at = now
        session.add(companion)
        session.add(
            Event(
                kind="companion.paired",
                agent="openfocus/ui",
                task_id=None,
                payload={"companion_id": companion_id, "device_id": device_id},
            )
        )
    return {"ok": True}


async def request_pairing_code(grpc_server: Any, companion_id: int) -> dict:
    with session_scope() as session:
        companion = CompanionRepository(session).get(int(companion_id))
        if companion is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        device_id = companion.device_id

        session.add(
            Event(
                kind="companion.pairing_code.requested",
                agent="openfocus/ui",
                task_id=None,
                payload={"companion_id": companion_id, "device_id": device_id},
            )
        )

        if display_status(companion, grpc_server) == COMPANION_STATUS_OFFLINE:
            raise HTTPException(status_code=400, detail="Companion offline")

    conn = grpc_server.registry.get(int(companion_id))
    if conn is None:
        raise HTTPException(
            status_code=502, detail="Companion is not online (no gRPC connection)"
        )

    try:
        _code, expires_at = await conn.request_pairing_code(
            force_new=True, timeout_seconds=10.0
        )
    except CompanionGrpcError as exc:
        raise HTTPException(
            status_code=502, detail=f"Companion failed to get pairing code: {exc}"
        )

    return {"ok": True, "expires_at": expires_at}


async def choose_directory(grpc_server: Any, companion_id: int) -> dict:
    with session_scope() as session:
        companion = CompanionRepository(session).get(int(companion_id))
        if companion is None:
            raise HTTPException(status_code=404, detail="Companion not found")
        if (
            companion.status or ""
        ).strip() == COMPANION_STATUS_PENDING_CERTIFICATION or not (
            companion.auth_token or ""
        ).strip():
            raise HTTPException(
                status_code=400, detail="Companion is not paired or unavailable"
            )

    conn = grpc_server.registry.get(int(companion_id))
    if conn is None:
        raise HTTPException(
            status_code=502, detail="Companion is not online (no gRPC connection)"
        )
    try:
        path = await conn.request_choose_directory(timeout_seconds=30.0)
    except CompanionGrpcError as exc:
        raise HTTPException(
            status_code=502, detail=f"Companion directory selection failed: {exc}"
        )
    return {"ok": True, "path": path}


def load_space_and_optional_companion(
    space_id: int,
) -> tuple[AgentSpace, Companion | None]:
    with session_scope() as session:
        space = session.get(AgentSpace, int(space_id))
        if space is None:
            raise HTTPException(status_code=404, detail="AgentSpace not found")
        companion = None
        if getattr(space, "companion_id", None):
            companion = session.get(Companion, int(space.companion_id))
        return space, companion


def require_online(grpc_server: Any, *, companion: Companion | None):
    if companion is None:
        raise HTTPException(
            status_code=400, detail="AgentSpace is not bound to a Companion"
        )
    if (
        companion.status or ""
    ).strip() == COMPANION_STATUS_PENDING_CERTIFICATION or not (
        companion.auth_token or ""
    ).strip():
        raise HTTPException(
            status_code=400, detail="Companion is not paired or unavailable"
        )
    conn = grpc_server.registry.get(int(companion.id))
    if conn is None:
        raise HTTPException(
            status_code=502, detail="Companion is not online (no gRPC connection)"
        )
    return conn


def select_online(
    grpc_server: Any, companion_id: int | None = None
) -> tuple[Companion, Any]:
    with session_scope() as session:
        repo = CompanionRepository(session)
        if companion_id:
            companion = repo.get(int(companion_id))
            companions = [companion] if companion is not None else []
        else:
            companions = repo.list_all_recent()
        for companion in companions:
            if (
                companion.status or ""
            ).strip() == COMPANION_STATUS_PENDING_CERTIFICATION or not (
                companion.auth_token or ""
            ).strip():
                continue
            conn = grpc_server.registry.get(int(companion.id))
            if conn is None:
                continue
            return companion, conn
    raise HTTPException(status_code=502, detail="No online Companion is available")


def has_online(grpc_server: Any) -> bool:
    with session_scope() as session:
        companions = CompanionRepository(session).list_all_recent()
        for companion in companions:
            if (
                companion.status or ""
            ).strip() == COMPANION_STATUS_PENDING_CERTIFICATION or not (
                companion.auth_token or ""
            ).strip():
                continue
            if grpc_server.registry.get(int(companion.id)) is not None:
                return True
    return False


def map_files_error(exc: CompanionGrpcError) -> HTTPException:
    msg = str(exc or "").strip()
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
    return HTTPException(status_code=502, detail=f"Companion file service error: {msg}")


async def list_space_files(grpc_server: Any, *, space_id: int, path: str = "") -> dict:
    space, companion = load_space_and_optional_companion(space_id)
    conn = require_online(grpc_server, companion=companion)
    try:
        res = await conn.request_files_list(
            root_path=str(space.root_path or ""),
            rel_path=str(path or ""),
            timeout_seconds=10.0,
        )
    except CompanionGrpcError as exc:
        raise map_files_error(exc)

    entries = [
        {
            "name": item.name,
            "rel_path": item.rel_path,
            "kind": item.kind,
            "size": int(item.size),
            "mtime": float(item.mtime),
        }
        for item in (res.entries or [])
    ]
    return {"ok": True, "path": res.path, "entries": entries}


async def read_space_file(grpc_server: Any, *, space_id: int, path: str) -> dict:
    space, companion = load_space_and_optional_companion(space_id)
    conn = require_online(grpc_server, companion=companion)
    try:
        res = await conn.request_files_read(
            root_path=str(space.root_path or ""),
            rel_path=str(path or ""),
            max_bytes=256 * 1024,
        )
    except CompanionGrpcError as exc:
        raise map_files_error(exc)

    return {
        "ok": True,
        "path": res.path,
        "content": res.content,
        "truncated": bool(res.truncated),
        "mime": res.mime,
    }


async def raw_space_file(grpc_server: Any, *, space_id: int, path: str) -> Response:
    space, companion = load_space_and_optional_companion(space_id)
    conn = require_online(grpc_server, companion=companion)
    try:
        res = await conn.request_files_raw(
            root_path=str(space.root_path or ""),
            rel_path=str(path or ""),
            max_bytes=2 * 1024 * 1024,
        )
    except CompanionGrpcError as exc:
        raise map_files_error(exc)

    return Response(
        content=bytes(res.data), media_type=(res.mime or "application/octet-stream")
    )
