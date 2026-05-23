# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

from ...companion.grpc import CompanionGrpcError
from ...db import session_scope
from ...models import AgentSpace, Companion
from ..events import service as event_service
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


class CompanionUseCaseError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class CompanionValidationError(CompanionUseCaseError):
    pass


class CompanionNotFoundError(CompanionUseCaseError):
    pass


class CompanionAgentSpaceNotFoundError(CompanionNotFoundError):
    pass


class CompanionUnavailableOrUnpairedError(CompanionUseCaseError):
    pass


class CompanionOfflineError(CompanionUseCaseError):
    pass


class CompanionRuntimeError(CompanionUseCaseError):
    pass


class CompanionRateLimitError(CompanionUseCaseError):
    pass


class CompanionFileNotFoundError(CompanionUseCaseError):
    pass


class CompanionFileTooLargeError(CompanionUseCaseError):
    pass


class CompanionFileValidationError(CompanionUseCaseError):
    pass


@dataclass(frozen=True)
class SelectedCompanion:
    """Session-independent companion identity returned with a live connection."""

    id: int


@dataclass(frozen=True)
class CompanionRawFileResult:
    data: bytes
    mime: str


class CompanionCommandPort(Protocol):
    async def request_pair(
        self, code: str, *, timeout_seconds: float = 10.0
    ) -> str: ...

    async def request_pairing_code(
        self, *, force_new: bool, timeout_seconds: float = 10.0
    ) -> tuple[str, str]: ...

    async def request_choose_directory(
        self, *, timeout_seconds: float = 30.0
    ) -> str: ...

    async def request_files_list(
        self, *, root_path: str, rel_path: str, timeout_seconds: float = 10.0
    ) -> Any: ...

    async def request_files_read(
        self, *, root_path: str, rel_path: str, max_bytes: int
    ) -> Any: ...

    async def request_files_raw(
        self, *, root_path: str, rel_path: str, max_bytes: int
    ) -> Any: ...


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _is_unpaired(companion: Companion) -> bool:
    return (
        companion.status or ""
    ).strip() == COMPANION_STATUS_PENDING_CERTIFICATION or not (
        companion.auth_token or ""
    ).strip()


def _get_command_port(
    grpc_server: Any, companion_id: int
) -> CompanionCommandPort | None:
    return grpc_server.registry.get(int(companion_id))


def _raise_missing_command_port() -> NoReturn:
    raise CompanionRuntimeError("Companion is not online (no gRPC connection)")


def _require_command_port(grpc_server: Any, companion_id: int) -> CompanionCommandPort:
    port = _get_command_port(grpc_server, int(companion_id))
    if port is None:
        _raise_missing_command_port()
    return port


def _require_paired_command_port(
    grpc_server: Any, companion_id: int
) -> CompanionCommandPort:
    with session_scope() as session:
        companion = CompanionRepository(session).get(int(companion_id))
        if companion is None:
            raise CompanionNotFoundError("Companion not found")
        if _is_unpaired(companion):
            raise CompanionUnavailableOrUnpairedError(
                "Companion is not paired or unavailable"
            )
    return _require_command_port(grpc_server, int(companion_id))


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
    online = bool(
        companion_id and (_get_command_port(grpc_server, companion_id) is not None)
    )
    return COMPANION_STATUS_ACTIVE if online else COMPANION_STATUS_OFFLINE


def register_companion(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise CompanionValidationError("invalid payload")
    device_id = str(payload.get("device_id") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not device_id or len(device_id) > 64:
        raise CompanionValidationError("device_id is required")
    if not base_url or len(base_url) > 1024:
        raise CompanionValidationError("base_url is required")

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
        raise CompanionValidationError("invalid companion_id")

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
            raise CompanionNotFoundError("Companion not found")
        device_id = str(companion.device_id or "")

        spaces = CompanionAgentSpaceRepository(session).list_by_companion_id(
            companion_id
        )
        unbound = len(spaces)
        for space in spaces:
            space.companion_id = None
            session.add(space)

        companion_repo.delete(companion)
        event_service.record_companion_deleted(
            session,
            companion_id=companion_id,
            device_id=device_id,
            unbound_spaces=unbound,
        )

    return {"ok": True, "companion_id": companion_id, "unbound_spaces": unbound}


async def pair_companion(grpc_server: Any, companion_id: int, payload: Any) -> dict:
    code = str((payload.get("code") if isinstance(payload, dict) else "") or "").strip()
    if not code:
        raise CompanionValidationError("code is required")
    if len(code) != 10:
        raise CompanionValidationError("Pairing code must be 10 characters")

    now = utcnow()
    minute_start = now.replace(second=0, microsecond=0)

    with session_scope() as session:
        companion = CompanionRepository(session).get(int(companion_id))
        if companion is None:
            raise CompanionNotFoundError("Companion not found")

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
            raise CompanionRateLimitError(
                "Pairing attempt limit reached for this minute (10 attempts)"
            )
        companion.pair_attempt_count += 1
        session.add(companion)

        device_id = companion.device_id
        event_service.record_companion_pair_attempted(
            session,
            companion_id=companion_id,
            device_id=device_id,
        )

    conn = _require_command_port(grpc_server, int(companion_id))
    try:
        token = await conn.request_pair(code, timeout_seconds=10.0)
    except CompanionGrpcError as exc:
        raise CompanionRuntimeError(f"Companion pairing failed: {exc}") from exc

    with session_scope() as session:
        companion = CompanionRepository(session).get(int(companion_id))
        if companion is None:
            raise CompanionNotFoundError("Companion not found")
        companion.auth_token = token
        companion.status = COMPANION_STATUS_ACTIVE
        companion.last_seen_at = now
        session.add(companion)
        event_service.record_companion_paired(
            session,
            companion_id=companion_id,
            device_id=device_id,
        )
    return {"ok": True}


async def request_pairing_code(grpc_server: Any, companion_id: int) -> dict:
    with session_scope() as session:
        companion = CompanionRepository(session).get(int(companion_id))
        if companion is None:
            raise CompanionNotFoundError("Companion not found")
        device_id = companion.device_id

        event_service.record_companion_pairing_code_requested(
            session,
            companion_id=companion_id,
            device_id=device_id,
        )

        if display_status(companion, grpc_server) == COMPANION_STATUS_OFFLINE:
            raise CompanionOfflineError("Companion offline")

    conn = _require_command_port(grpc_server, int(companion_id))

    try:
        _code, expires_at = await conn.request_pairing_code(
            force_new=True, timeout_seconds=10.0
        )
    except CompanionGrpcError as exc:
        raise CompanionRuntimeError(
            f"Companion failed to get pairing code: {exc}"
        ) from exc

    return {"ok": True, "expires_at": expires_at}


async def choose_directory(grpc_server: Any, companion_id: int) -> dict:
    conn = _require_paired_command_port(grpc_server, int(companion_id))
    try:
        path = await conn.request_choose_directory(timeout_seconds=30.0)
    except CompanionGrpcError as exc:
        raise CompanionRuntimeError(
            f"Companion directory selection failed: {exc}"
        ) from exc
    return {"ok": True, "path": path}


def load_space_and_optional_companion(
    space_id: int,
) -> tuple[AgentSpace, Companion | None]:
    with session_scope() as session:
        space = session.get(AgentSpace, int(space_id))
        if space is None:
            raise CompanionAgentSpaceNotFoundError("AgentSpace not found")
        companion = None
        if getattr(space, "companion_id", None):
            companion = session.get(Companion, int(space.companion_id))
        return space, companion


def require_online(grpc_server: Any, *, companion: Companion | None):
    if companion is None:
        raise CompanionValidationError("AgentSpace is not bound to a Companion")
    if _is_unpaired(companion):
        raise CompanionUnavailableOrUnpairedError(
            "Companion is not paired or unavailable"
        )
    return _require_command_port(grpc_server, int(companion.id))


def select_online(
    grpc_server: Any, companion_id: int | None = None
) -> tuple[SelectedCompanion, Any]:
    with session_scope() as session:
        repo = CompanionRepository(session)
        if companion_id:
            companion = repo.get(int(companion_id))
            companions = [companion] if companion is not None else []
        else:
            companions = repo.list_all_recent()
        for companion in companions:
            if _is_unpaired(companion):
                continue
            conn = _get_command_port(grpc_server, int(companion.id))
            if conn is None:
                continue
            # Do not leak SQLAlchemy ORM instances outside the repository/session
            # boundary. Terminal routes only need the identity plus the live
            # gRPC connection, so return a tiny DTO that cannot become detached.
            return SelectedCompanion(id=int(companion.id)), conn
    raise CompanionRuntimeError("No online Companion is available")


def has_online(grpc_server: Any) -> bool:
    with session_scope() as session:
        companions = CompanionRepository(session).list_all_recent()
        for companion in companions:
            if _is_unpaired(companion):
                continue
            if _get_command_port(grpc_server, int(companion.id)) is not None:
                return True
    return False


def raise_file_error(exc: CompanionGrpcError) -> NoReturn:
    msg = str(exc or "").strip()
    low = msg.lower()
    if ("not found" in low) or ("no such file" in low):
        raise CompanionFileNotFoundError(msg or "not found") from exc
    if ("too large" in low) or ("file too large" in low):
        raise CompanionFileTooLargeError(msg or "file too large") from exc
    if (
        ("traversal" in low)
        or ("invalid path" in low)
        or ("must be absolute" in low)
        or ("not a directory" in low)
        or ("root_path" in low)
    ):
        raise CompanionFileValidationError(msg or "bad request") from exc
    raise CompanionRuntimeError(f"Companion file service error: {msg}") from exc


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
        raise_file_error(exc)

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
        raise_file_error(exc)

    return {
        "ok": True,
        "path": res.path,
        "content": res.content,
        "truncated": bool(res.truncated),
        "mime": res.mime,
    }


async def raw_space_file(
    grpc_server: Any, *, space_id: int, path: str
) -> CompanionRawFileResult:
    space, companion = load_space_and_optional_companion(space_id)
    conn = require_online(grpc_server, companion=companion)
    try:
        res = await conn.request_files_raw(
            root_path=str(space.root_path or ""),
            rel_path=str(path or ""),
            max_bytes=2 * 1024 * 1024,
        )
    except CompanionGrpcError as exc:
        raise_file_error(exc)

    return CompanionRawFileResult(
        data=bytes(res.data), mime=(res.mime or "application/octet-stream")
    )
