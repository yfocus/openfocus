# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...companion.grpc import CompanionGrpcServer
from ...domains.companion import service as companion_service


def create_router(
    *, grpc_server: CompanionGrpcServer, templates: Jinja2Templates
) -> APIRouter:
    router = APIRouter()

    @router.get("/companions", response_class=HTMLResponse)
    def companions_view(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "companions.html", {})

    @router.post("/api/companions/register")
    def companion_register(payload: dict) -> dict:
        return companion_service.register_companion(payload)

    @router.get("/api/companions")
    def companions_list(limit: int = 50) -> dict:
        return companion_service.list_companions(grpc_server, limit=limit)

    @router.delete("/api/companions/{companion_id:int}")
    def companion_delete(companion_id: int) -> dict:
        return companion_service.delete_companion(grpc_server, companion_id)

    @router.post("/api/companions/{companion_id:int}/pair")
    async def companion_pair(companion_id: int, payload: dict) -> dict:
        return await companion_service.pair_companion(
            grpc_server, companion_id, payload
        )

    @router.post("/api/companions/{companion_id:int}/pairing_code")
    async def companion_pairing_code(companion_id: int) -> dict:
        return await companion_service.request_pairing_code(grpc_server, companion_id)

    @router.post("/api/companions/{companion_id:int}/choose_directory")
    async def companion_choose_directory_proxy(companion_id: int) -> dict:
        return await companion_service.choose_directory(grpc_server, companion_id)

    return router
