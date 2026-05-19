# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from openfocus.db import session_scope
from openfocus.domains.float_ball import service as float_ball_service
from openfocus.models import Companion, Event, SystemInboxTarget


class _Registry:
    def __init__(self, conn=None):
        self.conn = conn

    def get(self, companion_id: int):
        return self.conn


class _Grpc:
    def __init__(self, conn=None):
        self.registry = _Registry(conn)


class _FloatBallConn:
    capabilities = ["system_float_ball", "system_float_ball.test"]

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.stopped: list[str] = []

    async def request_float_ball_start(self, **kwargs):
        self.started.append(kwargs)
        return SimpleNamespace(backend="test")

    async def request_float_ball_stop(self, **kwargs):
        self.stopped.append(str(kwargs.get("browser_session_id") or ""))
        return SimpleNamespace(ok=True)


def _paired_companion() -> int:
    with session_scope() as s:
        comp = Companion(
            device_id="dev-float-ball",
            name="local",
            base_url="grpc://",
            status="active",
            auth_token="tok_test",
            last_seen_at=dt.datetime.now(dt.timezone.utc),
        )
        s.add(comp)
        s.flush()
        return int(comp.id)


def _set_target(companion_id: int, **kwargs) -> None:
    with session_scope() as s:
        s.add(
            SystemInboxTarget(
                id=float_ball_service.SYSTEM_INBOX_TARGET_ID,
                companion_id=companion_id,
                **kwargs,
            )
        )


def test_float_ball_preflight_requires_system_inbox_target() -> None:
    payload = float_ball_service.preflight_payload(
        _Grpc(conn=_FloatBallConn()), browser_session_id="browser-session-id-12345"
    )

    assert payload["mode"] == "web"
    assert payload["reason"] == "target_required"
    assert payload["target"]["set"] is False
    assert payload["settings_url"] == "/companions?system_inbox=1"


def test_set_system_inbox_target_requires_capability() -> None:
    cid = _paired_companion()
    conn = SimpleNamespace(capabilities=["terminal"])

    with pytest.raises(HTTPException):
        float_ball_service.set_target(_Grpc(conn=conn), companion_id=cid)

    with session_scope() as s:
        assert (
            s.get(SystemInboxTarget, float_ball_service.SYSTEM_INBOX_TARGET_ID) is None
        )


def test_set_system_inbox_target_records_target() -> None:
    cid = _paired_companion()
    conn = _FloatBallConn()

    payload = float_ball_service.set_target(_Grpc(conn=conn), companion_id=cid)

    assert payload["ok"] is True
    assert payload["reason"] == "ready"
    assert payload["target"]["companion_id"] == cid
    assert payload["companion"]["id"] == cid
    with session_scope() as s:
        target = s.get(SystemInboxTarget, float_ball_service.SYSTEM_INBOX_TARGET_ID)
        assert target is not None
        assert target.companion_id == cid
        assert s.query(Event).filter(Event.kind == "float_ball.target_set").count() == 1


def test_float_ball_preflight_checks_capability_after_target_selection() -> None:
    cid = _paired_companion()
    _set_target(cid)

    conn = SimpleNamespace(capabilities=["terminal"])
    payload = float_ball_service.preflight_payload(
        _Grpc(conn=conn), browser_session_id="browser-session-id-12345"
    )

    assert payload["mode"] == "web"
    assert payload["reason"] == "unsupported_capability"
    assert payload["target"]["set"] is True


def test_float_ball_start_uses_selected_target_companion() -> None:
    import asyncio

    cid = _paired_companion()
    _set_target(cid)
    conn = _FloatBallConn()

    async def _run() -> dict:
        return await float_ball_service.start_float_ball(
            _Grpc(conn=conn),
            browser_session_id="browser-session-id-12345",
            openfocus_base_url="http://testserver",
        )

    payload = asyncio.run(_run())

    assert payload["ok"] is True
    assert payload["mode"] == "system"
    assert payload["backend"] == "test"
    assert conn.started
    assert conn.started[0]["browser_session_id"] == "browser-session-id-12345"
    assert conn.started[0]["openfocus_base_url"] == "http://testserver"
    assert "summary_json" in conn.started[0]
    with session_scope() as s:
        target = s.get(SystemInboxTarget, float_ball_service.SYSTEM_INBOX_TARGET_ID)
        assert target.float_ball_enabled is True
        assert target.browser_session_id == "browser-session-id-12345"
        assert target.float_ball_base_url == "http://testserver"
        assert target.float_ball_backend == "test"
        assert target.float_ball_last_started_at is not None
        assert target.float_ball_last_error == ""


def test_float_ball_start_without_target_redirects_to_companions() -> None:
    import asyncio

    _paired_companion()
    conn = _FloatBallConn()

    async def _run() -> dict:
        return await float_ball_service.start_float_ball(
            _Grpc(conn=conn),
            browser_session_id="browser-session-id-12345",
            openfocus_base_url="http://127.0.0.1:8001",
        )

    payload = asyncio.run(_run())

    assert payload["ok"] is False
    assert payload["mode"] == "target_required"
    assert payload["reason"] == "target_required"
    assert payload["settings_url"] == "/companions?system_inbox=1"
    assert not conn.started


def test_float_ball_stop_clears_persisted_restore_intent_when_offline() -> None:
    import asyncio

    cid = _paired_companion()
    _set_target(
        cid,
        browser_session_id="browser-session-id-12345",
        float_ball_enabled=True,
        float_ball_base_url="http://testserver",
        float_ball_backend="test",
    )

    async def _run() -> dict:
        return await float_ball_service.stop_float_ball(
            _Grpc(conn=None), browser_session_id="browser-session-id-12345"
        )

    payload = asyncio.run(_run())

    assert payload["ok"] is True
    assert payload["reason"] == "target_companion_offline"
    with session_scope() as s:
        target = s.get(SystemInboxTarget, float_ball_service.SYSTEM_INBOX_TARGET_ID)
        assert target.float_ball_enabled is False


def test_restore_desired_float_ball_for_reconnected_companion() -> None:
    import asyncio

    cid = _paired_companion()
    _set_target(
        cid,
        browser_session_id="browser-session-id-12345",
        float_ball_enabled=True,
        float_ball_base_url="http://testserver",
        float_ball_backend="test",
    )
    conn = _FloatBallConn()

    restored = asyncio.run(
        float_ball_service.restore_desired_float_balls_for_companion(
            companion_id=cid, conn=conn
        )
    )

    assert restored == 1
    assert len(conn.started) == 1
    assert conn.started[0]["browser_session_id"] == "browser-session-id-12345"
    assert conn.started[0]["openfocus_base_url"] == "http://testserver"
    with session_scope() as s:
        target = s.get(SystemInboxTarget, float_ball_service.SYSTEM_INBOX_TARGET_ID)
        assert target.float_ball_enabled is True
        assert target.float_ball_last_error == ""
        assert s.query(Event).filter(Event.kind == "float_ball.restored").count() == 1


@pytest.mark.anyio
async def test_float_ball_target_routes_drive_system_inbox_flow() -> None:
    from fastapi import FastAPI

    from openfocus.web.routes.float_ball import create_router

    cid = _paired_companion()
    conn = _FloatBallConn()
    app = FastAPI()
    app.include_router(create_router(grpc_server=_Grpc(conn=conn)))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        target_res = await client.get("/api/float_ball/target")
        assert target_res.status_code == 200
        assert target_res.json()["reason"] == "target_required"

        missing_start = await client.post("/api/float_ball/start")
        assert missing_start.status_code == 200
        missing_payload = missing_start.json()
        assert missing_payload["mode"] == "target_required"
        assert missing_payload["settings_url"] == "/companions?system_inbox=1"
        assert not conn.started

        set_res = await client.post(
            "/api/float_ball/target", json={"companion_id": cid}
        )
        assert set_res.status_code == 200
        set_payload = set_res.json()
        assert set_payload["reason"] == "ready"
        assert set_payload["target"]["companion_id"] == cid

        started = await client.post("/api/float_ball/start")
        assert started.status_code == 200
        assert started.json()["mode"] == "system"
        assert len(conn.started) == 1

        cleared = await client.delete("/api/float_ball/target")
        assert cleared.status_code == 200
        clear_payload = cleared.json()
        assert clear_payload["target"]["set"] is False
        assert clear_payload["stop_requested"] is True
        assert clear_payload["stopped"] is True
        assert len(conn.stopped) == 1
        with session_scope() as s:
            assert (
                s.get(SystemInboxTarget, float_ball_service.SYSTEM_INBOX_TARGET_ID)
                is None
            )
