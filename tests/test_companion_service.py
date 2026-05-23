# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.templating import Jinja2Templates
from httpx import ASGITransport, AsyncClient

from openfocus.companion.grpc import CompanionGrpcError
from openfocus.domains.companion import service as companion_service
from openfocus.web.routes import agent_spaces as agent_spaces_routes
from openfocus.web.routes.companions import create_router


class _Registry:
    def __init__(self, items: dict[int, Any] | None = None) -> None:
        self.items = {int(key): value for key, value in (items or {}).items()}

    def get(self, companion_id: int) -> Any:
        return self.items.get(int(companion_id))


class _GrpcServer:
    def __init__(self, items: dict[int, Any] | None = None) -> None:
        self.registry = _Registry(items)


class _FakeCommandPort:
    def __init__(
        self,
        *,
        choose_path: str = "/tmp/workspace",
        files_error: str | None = None,
    ) -> None:
        self.choose_path = choose_path
        self.files_error = files_error
        self.calls: list[str] = []

    async def request_choose_directory(self, *, timeout_seconds: float = 30.0) -> str:
        self.calls.append(f"choose_directory:{timeout_seconds}")
        return self.choose_path

    async def request_files_read(
        self, *, root_path: str, rel_path: str, max_bytes: int
    ) -> Any:
        self.calls.append(f"files_read:{root_path}:{rel_path}:{max_bytes}")
        if self.files_error is not None:
            raise CompanionGrpcError(self.files_error)
        return SimpleNamespace(
            path=rel_path, content="", truncated=False, mime="text/plain"
        )


def _read_audit_text(memory_root):
    audit_files = list((memory_root / "audit").glob("**/*.md"))
    return "\n".join(path.read_text(encoding="utf-8") for path in audit_files)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_router(
            grpc_server=_GrpcServer(),
            templates=Jinja2Templates(directory="openfocus/templates"),
        )
    )
    return app


def _agent_spaces_app(grpc_server: _GrpcServer) -> FastAPI:
    app = FastAPI()

    async def _subscribe(_session_id: str) -> asyncio.Queue:
        return asyncio.Queue()

    app.include_router(
        agent_spaces_routes.create_router(
            grpc_server=grpc_server,
            templates=Jinja2Templates(directory="openfocus/templates"),
            ttyd_auto_prompts={},
            agent_sse_subscribe=_subscribe,
            agent_sse_unsubscribe=lambda _session_id: None,
            agent_sse_publish=lambda _session_id, _event: None,
            rewrite_ttyd_input_for_auto_prompts=lambda *args, **kwargs: None,
        )
    )
    return app


def _create_companion(*, status: str = "active", auth_token: str = "token") -> int:
    from openfocus.db import session_scope
    from openfocus.models import Companion

    with session_scope() as session:
        companion = Companion(
            device_id="companion-test-device",
            name="test companion",
            base_url="grpc://test-companion",
            status=status,
            auth_token=auth_token,
        )
        session.add(companion)
        session.flush()
        return int(companion.id)


def _create_bound_agent_space(tmp_path) -> tuple[int, int]:
    from openfocus.db import session_scope
    from openfocus.models import AgentSpace, Companion

    with session_scope() as session:
        companion = Companion(
            device_id="files-test-device",
            name="files companion",
            base_url="grpc://files-companion",
            status="active",
            auth_token="token",
        )
        session.add(companion)
        session.flush()
        companion_id = int(companion.id)
        space = AgentSpace(
            task_public_id="files-test-task",
            companion_id=companion_id,
            root_path=str(tmp_path),
        )
        session.add(space)
        session.flush()
        return companion_id, int(space.id)


def test_register_companion_invalid_payload_raises_domain_validation_error() -> None:
    with pytest.raises(companion_service.CompanionValidationError) as exc_info:
        companion_service.register_companion([])

    assert exc_info.value.detail == "invalid payload"
    assert not isinstance(exc_info.value, HTTPException)


def test_register_companion_invalid_device_id_raises_domain_validation_error() -> None:
    with pytest.raises(companion_service.CompanionValidationError) as exc_info:
        companion_service.register_companion({"base_url": "grpc://127.0.0.1"})

    assert exc_info.value.detail == "device_id is required"
    assert not isinstance(exc_info.value, HTTPException)


def test_register_companion_route_maps_invalid_payload_to_http_400() -> None:
    async def _run() -> None:
        transport = ASGITransport(app=_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/companions/register", json=[])

        assert response.status_code == 400
        assert response.json() == {"detail": "invalid payload"}

    asyncio.run(_run())


def test_delete_companion_missing_raises_domain_not_found_error() -> None:
    with pytest.raises(companion_service.CompanionNotFoundError) as exc_info:
        companion_service.delete_companion(_GrpcServer(), 999)

    assert exc_info.value.detail == "Companion not found"
    assert not isinstance(exc_info.value, HTTPException)


def test_delete_companion_records_event_without_audit_memory(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("OPENFOCUS_MEMORY_DIR", str(tmp_path / "memory"))

    from openfocus.db import session_scope
    from openfocus.models import AgentSpace, Companion, Event

    with session_scope() as session:
        companion = Companion(
            device_id="delete-companion-device",
            name="delete companion",
            base_url="grpc://delete-companion",
            status="active",
            auth_token="tok_delete",
        )
        session.add(companion)
        session.flush()
        companion_id = int(companion.id)
        session.add(
            AgentSpace(
                task_public_id="delete-companion-task",
                companion_id=companion_id,
                root_path=str(tmp_path),
            )
        )

    result = companion_service.delete_companion(_GrpcServer(), companion_id)

    assert result == {
        "ok": True,
        "companion_id": companion_id,
        "unbound_spaces": 1,
    }
    with session_scope() as session:
        event = session.query(Event).filter(Event.kind == "companion.deleted").one()
        assert event.agent == "openfocus/ui"
        assert event.task_id is None
        assert event.payload == {
            "companion_id": companion_id,
            "device_id": "delete-companion-device",
            "unbound_spaces": 1,
        }
        assert session.get(Companion, companion_id) is None
        space = (
            session.query(AgentSpace)
            .filter_by(task_public_id="delete-companion-task")
            .one()
        )
        assert space.companion_id is None

    assert _read_audit_text(tmp_path / "memory") == ""


def test_load_missing_agent_space_raises_domain_error_not_http_exception() -> None:
    with pytest.raises(companion_service.CompanionAgentSpaceNotFoundError) as exc_info:
        companion_service.load_space_and_optional_companion(999)

    assert exc_info.value.detail == "AgentSpace not found"
    assert not isinstance(exc_info.value, HTTPException)


def test_select_online_without_online_companion_raises_domain_runtime_error() -> None:
    with pytest.raises(companion_service.CompanionRuntimeError) as exc_info:
        companion_service.select_online(_GrpcServer())

    assert exc_info.value.detail == "No online Companion is available"
    assert not isinstance(exc_info.value, HTTPException)


def test_delete_companion_route_maps_missing_to_http_404() -> None:
    async def _run() -> None:
        transport = ASGITransport(app=_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete("/api/companions/999")

        assert response.status_code == 404
        assert response.json() == {"detail": "Companion not found"}

    asyncio.run(_run())


def test_pairing_code_length_validation_maps_to_http_400() -> None:
    async def _run() -> None:
        transport = ASGITransport(app=_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/companions/1/pair", json={"code": "short"}
            )

        assert response.status_code == 400
        assert response.json() == {"detail": "Pairing code must be 10 characters"}

    asyncio.run(_run())


def test_choose_directory_unpaired_companion_raises_before_command_port() -> None:
    companion_id = _create_companion(
        status=companion_service.COMPANION_STATUS_PENDING_CERTIFICATION,
        auth_token="",
    )
    port = _FakeCommandPort(choose_path="/tmp/unused")

    with pytest.raises(
        companion_service.CompanionUnavailableOrUnpairedError
    ) as exc_info:
        asyncio.run(
            companion_service.choose_directory(
                _GrpcServer({companion_id: port}), companion_id
            )
        )

    assert exc_info.value.detail == "Companion is not paired or unavailable"
    assert port.calls == []


def test_choose_directory_active_paired_but_offline_raises_runtime_error() -> None:
    companion_id = _create_companion()

    with pytest.raises(companion_service.CompanionRuntimeError) as exc_info:
        asyncio.run(companion_service.choose_directory(_GrpcServer(), companion_id))

    assert exc_info.value.detail == "Companion is not online (no gRPC connection)"


@pytest.mark.parametrize(
    ("grpc_error", "expected_error", "expected_detail"),
    [
        (
            "not found",
            companion_service.CompanionFileNotFoundError,
            "not found",
        ),
        (
            "file too large",
            companion_service.CompanionFileTooLargeError,
            "file too large",
        ),
        (
            "invalid path",
            companion_service.CompanionFileValidationError,
            "invalid path",
        ),
        (
            "path traversal rejected",
            companion_service.CompanionFileValidationError,
            "path traversal rejected",
        ),
        (
            "unexpected companion failure",
            companion_service.CompanionRuntimeError,
            "Companion file service error: unexpected companion failure",
        ),
    ],
)
def test_read_space_file_maps_command_port_file_errors_to_domain_errors(
    tmp_path,
    grpc_error: str,
    expected_error: type[companion_service.CompanionUseCaseError],
    expected_detail: str,
) -> None:
    companion_id, space_id = _create_bound_agent_space(tmp_path)
    port = _FakeCommandPort(files_error=grpc_error)

    with pytest.raises(expected_error) as exc_info:
        asyncio.run(
            companion_service.read_space_file(
                _GrpcServer({companion_id: port}),
                space_id=space_id,
                path="README.md",
            )
        )

    assert exc_info.value.detail == expected_detail
    assert len(port.calls) == 1
    assert port.calls[0].startswith(f"files_read:{tmp_path}:README.md:")


@pytest.mark.parametrize(
    ("grpc_error", "expected_status", "expected_detail"),
    [
        ("not found", 404, "not found"),
        ("file too large", 413, "file too large"),
        ("invalid path", 400, "invalid path"),
        ("path traversal rejected", 400, "path traversal rejected"),
        (
            "unexpected companion failure",
            502,
            "Companion file service error: unexpected companion failure",
        ),
    ],
)
def test_agent_space_file_route_maps_command_port_errors_to_http_status(
    tmp_path, grpc_error: str, expected_status: int, expected_detail: str
) -> None:
    async def _run() -> None:
        companion_id, space_id = _create_bound_agent_space(tmp_path)
        port = _FakeCommandPort(files_error=grpc_error)
        transport = ASGITransport(
            app=_agent_spaces_app(_GrpcServer({companion_id: port}))
        )
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/api/agent_spaces/{space_id}/files/read",
                params={"path": "README.md"},
            )

        assert response.status_code == expected_status
        assert response.json() == {"detail": expected_detail}
        assert len(port.calls) == 1

    asyncio.run(_run())
