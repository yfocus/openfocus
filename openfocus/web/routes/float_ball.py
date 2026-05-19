# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ...companion.grpc import CompanionGrpcServer
from ...domains.float_ball import service as float_ball_service


def _session_from_request(request: Request, response: Response) -> str:
    sid = float_ball_service.valid_browser_session_id(
        request.cookies.get(float_ball_service.BROWSER_SESSION_COOKIE)
    )
    if not sid:
        sid = float_ball_service.new_browser_session_id()
    response.set_cookie(
        float_ball_service.BROWSER_SESSION_COOKIE,
        sid,
        max_age=float_ball_service.SESSION_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return sid


def create_router(*, grpc_server: CompanionGrpcServer) -> APIRouter:
    router = APIRouter()

    @router.get("/api/float_ball/preflight")
    def float_ball_preflight(request: Request, response: Response) -> dict:
        sid = _session_from_request(request, response)
        return float_ball_service.preflight_payload(grpc_server, browser_session_id=sid)

    @router.get("/api/float_ball/target")
    def float_ball_target() -> dict:
        return float_ball_service.target_payload(grpc_server)

    @router.post("/api/float_ball/target")
    def float_ball_set_target(payload: dict) -> dict:
        companion_id = int((payload or {}).get("companion_id") or 0)
        return float_ball_service.set_target(grpc_server, companion_id=companion_id)

    @router.delete("/api/float_ball/target")
    async def float_ball_clear_target() -> dict:
        return await float_ball_service.clear_target(grpc_server)

    @router.post("/api/float_ball/start")
    async def float_ball_start(request: Request, response: Response) -> dict:
        sid = _session_from_request(request, response)
        return await float_ball_service.start_float_ball(
            grpc_server,
            browser_session_id=sid,
            openfocus_base_url=str(request.base_url).rstrip("/"),
        )

    @router.post("/api/float_ball/stop")
    async def float_ball_stop(request: Request, response: Response) -> dict:
        sid = _session_from_request(request, response)
        return await float_ball_service.stop_float_ball(
            grpc_server, browser_session_id=sid
        )

    @router.post("/api/float_ball/action")
    async def float_ball_action(
        request: Request, response: Response, payload: dict
    ) -> dict:
        sid = _session_from_request(request, response)
        return float_ball_service.record_float_ball_action(
            browser_session_id=sid,
            action=str(payload.get("action") if isinstance(payload, dict) else ""),
            payload=payload if isinstance(payload, dict) else {},
        )

    return router
