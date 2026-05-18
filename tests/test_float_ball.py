# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from openfocus.db import session_scope
from openfocus.domains.float_ball import service as float_ball_service
from openfocus.models import BrowserCompanionBinding, Companion


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


def test_float_ball_preflight_requires_trusted_browser_binding() -> None:
    payload = float_ball_service.preflight_payload(
        _Grpc(conn=_FloatBallConn()), browser_session_id="browser-session-id-12345"
    )

    assert payload["mode"] == "web"
    assert payload["reason"] == "browser_not_bound"
    assert payload["bound"] is False


def test_nonce_proof_confirms_binding_to_paired_companion() -> None:
    cid = _paired_companion()
    browser_session_id = "browser-session-id-12345"
    with session_scope() as s:
        challenge = float_ball_service.create_bind_challenge(
            s,
            browser_session_id=browser_session_id,
            openfocus_base_url="http://testserver",
        )
    nonce = challenge["bind"]["nonce"]

    result = float_ball_service.confirm_browser_bind_nonce(
        nonce=nonce, companion_id=cid
    )

    assert result["ok"] is True
    assert result["companion_id"] == cid
    with session_scope() as s:
        binding = (
            s.query(BrowserCompanionBinding)
            .filter(BrowserCompanionBinding.browser_session_id == browser_session_id)
            .one()
        )
        assert binding.companion_id == cid
        assert binding.trust_method == "nonce_protocol"


def test_float_ball_preflight_checks_capability_after_binding() -> None:
    cid = _paired_companion()
    with session_scope() as s:
        s.add(
            BrowserCompanionBinding(
                browser_session_id="browser-session-id-12345",
                companion_id=cid,
                trust_method="nonce_protocol",
                last_verified_at=dt.datetime.now(dt.timezone.utc),
            )
        )

    conn = SimpleNamespace(capabilities=["terminal"])
    payload = float_ball_service.preflight_payload(
        _Grpc(conn=conn), browser_session_id="browser-session-id-12345"
    )

    assert payload["mode"] == "web"
    assert payload["reason"] == "unsupported_capability"
    assert payload["bound"] is True


def test_float_ball_start_uses_bound_capable_companion() -> None:
    import asyncio

    cid = _paired_companion()
    with session_scope() as s:
        s.add(
            BrowserCompanionBinding(
                browser_session_id="browser-session-id-12345",
                companion_id=cid,
                trust_method="nonce_protocol",
                last_verified_at=dt.datetime.now(dt.timezone.utc),
            )
        )
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


def test_float_ball_start_auto_binds_single_loopback_companion() -> None:
    import asyncio

    cid = _paired_companion()
    conn = _FloatBallConn()

    async def _run() -> dict:
        return await float_ball_service.start_float_ball(
            _Grpc(conn=conn),
            browser_session_id="browser-session-id-12345",
            openfocus_base_url="http://127.0.0.1:8001",
            client_host="127.0.0.1",
        )

    payload = asyncio.run(_run())

    assert payload["ok"] is True
    assert payload["mode"] == "system"
    assert payload["backend"] == "test"
    assert conn.started
    with session_scope() as s:
        binding = (
            s.query(BrowserCompanionBinding)
            .filter(
                BrowserCompanionBinding.browser_session_id == "browser-session-id-12345"
            )
            .one()
        )
        assert binding.companion_id == cid
        assert binding.trust_method == "loopback_auto"


def test_float_ball_start_requires_protocol_binding_for_non_loopback_browser() -> None:
    import asyncio

    _paired_companion()
    conn = _FloatBallConn()

    async def _run() -> dict:
        return await float_ball_service.start_float_ball(
            _Grpc(conn=conn),
            browser_session_id="browser-session-id-12345",
            openfocus_base_url="http://127.0.0.1:8001",
            client_host="192.168.1.20",
        )

    payload = asyncio.run(_run())

    assert payload["mode"] == "bind_required"
    assert payload["reason"] == "browser_not_bound"
    assert not conn.started


def test_float_ball_nonce_protocol_binding_and_start_via_grpc(tmp_path) -> None:
    import asyncio
    import os
    import socket
    import tempfile
    import uuid

    from httpx import ASGITransport, AsyncClient

    async def _wait_until_companion_ready(client: AsyncClient) -> dict:
        from openfocus.app import COMPANION_GRPC

        deadline = asyncio.get_running_loop().time() + 2.0
        last = None
        while asyncio.get_running_loop().time() < deadline:
            r = await client.get("/api/companions")
            assert r.status_code == 200
            items = r.json().get("items") or []
            if items:
                comp = items[0]
                cid = int(comp.get("id") or 0)
                if cid and COMPANION_GRPC.registry.get(cid) is not None:
                    return comp
            last = items
            await asyncio.sleep(0.02)
        raise AssertionError(f"companion not ready, last={last}")

    async def _run() -> None:
        os.environ["OPENFOCUS_COMPANION_STATE"] = str(tmp_path / "companion_state.json")
        os.environ["OPENFOCUS_TEST_PAIRING_CODE"] = "A1B2C3D4E5"
        os.environ["OPENFOCUS_SYSTEM_FLOAT_BALL_BACKEND"] = "test"
        short_sock_dir = tempfile.gettempdir()
        os.environ["OPENFOCUS_HOOK_SOCK"] = os.path.join(
            short_sock_dir, f"of-hook-{uuid.uuid4().hex}.sock"
        )
        os.environ["OPENFOCUS_PROTOCOL_SOCK"] = os.path.join(
            short_sock_dir, f"of-proto-{uuid.uuid4().hex}.sock"
        )
        os.environ["OPENFOCUS_GRPC_AUTOSTART"] = "0"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
        except PermissionError:
            pytest.skip("sandbox does not allow binding localhost sockets")
        os.environ["OPENFOCUS_GRPC_PORT"] = str(port)

        from openfocus.app import COMPANION_GRPC, app
        from openfocus.companion import run_companion
        from openfocus.companion.runtime import send_protocol_url

        await COMPANION_GRPC.start()
        assert COMPANION_GRPC.bound_addr
        stop = asyncio.Event()
        comp_task = asyncio.create_task(
            run_companion(grpc_addr=COMPANION_GRPC.bound_addr, stop_event=stop)
        )
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                comp = await _wait_until_companion_ready(client)
                cid = int(comp["id"])
                r = await client.post(f"/api/companions/{cid}/pairing_code")
                assert r.status_code == 200
                r = await client.post(
                    f"/api/companions/{cid}/pair", json={"code": "A1B2C3D4E5"}
                )
                assert r.status_code == 200

                r = await client.post("/api/float_ball/start")
                assert r.status_code == 200
                first = r.json()
                assert first["mode"] == "bind_required"
                bind = first["bind"]
                assert send_protocol_url(bind["open_url"]) is True

                deadline = asyncio.get_running_loop().time() + 2.0
                status = {}
                while asyncio.get_running_loop().time() < deadline:
                    r = await client.get(
                        "/api/float_ball/bind_status",
                        params={"nonce": bind["nonce"]},
                    )
                    assert r.status_code == 200
                    status = r.json()
                    if status.get("status") == "confirmed":
                        break
                    await asyncio.sleep(0.02)
                assert status.get("status") == "confirmed"
                assert status.get("companion_id") == cid

                r = await client.post("/api/float_ball/start")
                assert r.status_code == 200
                started = r.json()
                assert started["ok"] is True
                assert started["mode"] == "system"
                assert started["backend"] == "test"
        finally:
            stop.set()
            await asyncio.wait_for(comp_task, timeout=5.0)
            await COMPANION_GRPC.stop()
            for key in (
                "OPENFOCUS_TEST_PAIRING_CODE",
                "OPENFOCUS_SYSTEM_FLOAT_BALL_BACKEND",
                "OPENFOCUS_HOOK_SOCK",
                "OPENFOCUS_PROTOCOL_SOCK",
            ):
                os.environ.pop(key, None)

    asyncio.run(_run())
