# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...companion.grpc import CompanionGrpcServer
from ...domains.companion import service as companion_service


def _companion_error_to_http(
    exc: companion_service.CompanionUseCaseError,
) -> HTTPException:
    if isinstance(exc, companion_service.CompanionNotFoundError):
        return HTTPException(status_code=404, detail=exc.detail)
    if isinstance(exc, companion_service.CompanionRateLimitError):
        return HTTPException(status_code=429, detail=exc.detail)
    if isinstance(
        exc,
        (
            companion_service.CompanionValidationError,
            companion_service.CompanionUnavailableOrUnpairedError,
            companion_service.CompanionOfflineError,
        ),
    ):
        return HTTPException(status_code=400, detail=exc.detail)
    if isinstance(exc, companion_service.CompanionRuntimeError):
        return HTTPException(status_code=502, detail=exc.detail)
    return HTTPException(status_code=500, detail=exc.detail)


def _raise_companion_http_error(exc: companion_service.CompanionUseCaseError) -> None:
    raise _companion_error_to_http(exc) from exc


def create_router(
    *, grpc_server: CompanionGrpcServer, templates: Jinja2Templates
) -> APIRouter:
    router = APIRouter()

    @router.get("/companions", response_class=HTMLResponse)
    def companions_view(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "companions.html", {})

    @router.post("/api/companions/register")
    def companion_register(payload: Any = Body(...)) -> dict:
        try:
            return companion_service.register_companion(payload)
        except companion_service.CompanionUseCaseError as exc:
            _raise_companion_http_error(exc)

    @router.get("/api/companions")
    def companions_list(limit: int = 50) -> dict:
        try:
            return companion_service.list_companions(grpc_server, limit=limit)
        except companion_service.CompanionUseCaseError as exc:
            _raise_companion_http_error(exc)

    @router.delete("/api/companions/{companion_id:int}")
    def companion_delete(companion_id: int) -> dict:
        try:
            return companion_service.delete_companion(grpc_server, companion_id)
        except companion_service.CompanionUseCaseError as exc:
            _raise_companion_http_error(exc)

    @router.post("/api/companions/{companion_id:int}/pair")
    async def companion_pair(companion_id: int, payload: Any = Body(...)) -> dict:
        try:
            return await companion_service.pair_companion(
                grpc_server, companion_id, payload
            )
        except companion_service.CompanionUseCaseError as exc:
            _raise_companion_http_error(exc)

    @router.post("/api/companions/{companion_id:int}/pairing_code")
    async def companion_pairing_code(companion_id: int) -> dict:
        try:
            return await companion_service.request_pairing_code(
                grpc_server, companion_id
            )
        except companion_service.CompanionUseCaseError as exc:
            _raise_companion_http_error(exc)

    @router.post("/api/companions/{companion_id:int}/choose_directory")
    async def companion_choose_directory_proxy(companion_id: int) -> dict:
        try:
            return await companion_service.choose_directory(grpc_server, companion_id)
        except companion_service.CompanionUseCaseError as exc:
            _raise_companion_http_error(exc)

    return router
