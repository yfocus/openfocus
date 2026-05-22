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
