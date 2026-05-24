# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...domains.code_navigation import service as code_navigation_service
from ...domains.companion import service as companion_service


def _companion_http_error(
    exc: companion_service.CompanionUseCaseError,
) -> HTTPException:
    detail = exc.detail
    if isinstance(
        exc,
        (
            companion_service.CompanionAgentSpaceNotFoundError,
            companion_service.CompanionNotFoundError,
            companion_service.CompanionFileNotFoundError,
        ),
    ):
        return HTTPException(status_code=404, detail=detail)
    if isinstance(exc, companion_service.CompanionFileTooLargeError):
        return HTTPException(status_code=413, detail=detail)
    if isinstance(
        exc,
        (
            companion_service.CompanionValidationError,
            companion_service.CompanionUnavailableOrUnpairedError,
            companion_service.CompanionOfflineError,
            companion_service.CompanionFileValidationError,
        ),
    ):
        return HTTPException(status_code=400, detail=detail)
    if isinstance(exc, companion_service.CompanionRuntimeError):
        return HTTPException(status_code=502, detail=detail)
    return HTTPException(status_code=500, detail=detail)


def _code_navigation_http_error(
    exc: code_navigation_service.CodeNavigationUseCaseError,
) -> HTTPException:
    if isinstance(exc, code_navigation_service.CodeNavigationValidationError):
        return HTTPException(status_code=400, detail=exc.detail)
    return HTTPException(status_code=500, detail=exc.detail)


def create_router(*, grpc_server) -> APIRouter:
    router = APIRouter()

    @router.get("/api/agent_spaces/{space_id}/code/search")
    async def code_search(
        space_id: int,
        q: str = "",
        kind: str = "all",
        include: str | None = None,
        exclude: str | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
        limit: int | None = None,
    ) -> dict:
        try:
            return await code_navigation_service.search(
                grpc_server,
                space_id=space_id,
                q=q,
                kind=kind,
                include=include,
                exclude=exclude,
                case_sensitive=case_sensitive,
                regex=regex,
                limit=limit,
            )
        except code_navigation_service.CodeNavigationUseCaseError as exc:
            raise _code_navigation_http_error(exc) from exc
        except companion_service.CompanionUseCaseError as exc:
            raise _companion_http_error(exc) from exc

    @router.get("/api/agent_spaces/{space_id}/code/symbols")
    async def code_symbols(
        space_id: int,
        q: str = "",
        include: str | None = None,
        exclude: str | None = None,
        limit: int | None = None,
    ) -> dict:
        try:
            return await code_navigation_service.symbols(
                grpc_server,
                space_id=space_id,
                q=q,
                include=include,
                exclude=exclude,
                limit=limit,
            )
        except code_navigation_service.CodeNavigationUseCaseError as exc:
            raise _code_navigation_http_error(exc) from exc
        except companion_service.CompanionUseCaseError as exc:
            raise _companion_http_error(exc) from exc

    @router.post("/api/agent_spaces/{space_id}/code/definition")
    async def code_definition(space_id: int, payload: dict) -> dict:
        try:
            return await code_navigation_service.definition(
                grpc_server,
                space_id=space_id,
                payload=payload,
            )
        except code_navigation_service.CodeNavigationUseCaseError as exc:
            raise _code_navigation_http_error(exc) from exc
        except companion_service.CompanionUseCaseError as exc:
            raise _companion_http_error(exc) from exc

    @router.post("/api/agent_spaces/{space_id}/code/references")
    async def code_references(space_id: int, payload: dict) -> dict:
        try:
            return await code_navigation_service.references(
                grpc_server,
                space_id=space_id,
                payload=payload,
            )
        except code_navigation_service.CodeNavigationUseCaseError as exc:
            raise _code_navigation_http_error(exc) from exc
        except companion_service.CompanionUseCaseError as exc:
            raise _companion_http_error(exc) from exc

    @router.get("/api/agent_spaces/{space_id}/code/status")
    def code_status(space_id: int) -> dict:
        try:
            return code_navigation_service.status(grpc_server, space_id=space_id)
        except code_navigation_service.CodeNavigationUseCaseError as exc:
            raise _code_navigation_http_error(exc) from exc
        except companion_service.CompanionUseCaseError as exc:
            raise _companion_http_error(exc) from exc

    return router
