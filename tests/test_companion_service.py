# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.templating import Jinja2Templates
from httpx import ASGITransport, AsyncClient

from openfocus.domains.companion import service as companion_service
from openfocus.web.routes.companions import create_router


class _Registry:
    def get(self, companion_id: int) -> Any:
        return None


class _GrpcServer:
    registry = _Registry()


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
