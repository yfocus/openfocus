# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from ...db import session_scope
from ...domains.recommendations import service as recommendation_service


def create_router(
    *, provider_factory: Callable[[], tuple[Any | None, str | None]]
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/recommendations/next")
    def recommendations_next(
        limit: int = 2, trigger: str = "manual_refresh"
    ) -> JSONResponse:
        provider, provider_error = provider_factory()
        with session_scope() as s:
            payload = recommendation_service.generate_next_moves(
                s,
                provider=provider,
                provider_error=provider_error,
                trigger=trigger,
                limit=limit,
            )
        return JSONResponse(
            payload,
            headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        )

    @router.get("/api/recommendations/latest")
    def recommendations_latest() -> JSONResponse:
        with session_scope() as s:
            payload = recommendation_service.latest_payload(s)
        return JSONResponse(
            payload,
            headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        )

    @router.post("/api/recommendations/feedback")
    def recommendations_feedback(payload: dict) -> JSONResponse:
        try:
            with session_scope() as s:
                result = recommendation_service.submit_feedback(s, payload)
        except recommendation_service.RecommendationTaskNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except recommendation_service.RecommendationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    return router
