from __future__ import annotations

import asyncio
import datetime as dt
import os

import anyio
import pytest
from httpx import ASGITransport, AsyncClient


async def _wait_until_companion_registered(client: AsyncClient, *, timeout_s: float = 2.0) -> dict:
    deadline = anyio.current_time() + timeout_s
    last = None
    while anyio.current_time() < deadline:
        r = await client.get("/api/companions")
        assert r.status_code == 200
        items = r.json().get("items") or []
        if items:
            return items[0]
        last = items
        await anyio.sleep(0.02)
    raise AssertionError(f"companion not registered within {timeout_s}s, last={last}")


@pytest.mark.anyio
async def test_companion_grpc_connect_pair_choose_directory_and_create_agent_space(tmp_path):
    # 让 companion 的 state 写入临时目录，避免污染 ~/.openfocus
    os.environ["OPENFOCUS_COMPANION_STATE"] = str(tmp_path / "companion_state.json")

    # 固定配对码 + 固定目录选择返回
    os.environ["OPENFOCUS_TEST_PAIRING_CODE"] = "A1B2C3D4E5"
    os.environ["OPENFOCUS_TEST_CHOOSE_DIRECTORY"] = str(tmp_path / "ws")
    (tmp_path / "ws").mkdir()

    # 测试里手动启动 gRPC server（随机端口），避免端口冲突
    os.environ["OPENFOCUS_GRPC_AUTOSTART"] = "0"
    os.environ["OPENFOCUS_GRPC_PORT"] = "0"

    from openfocus.companion import run_companion
    from openfocus.db import session_scope
    from openfocus.main import COMPANION_GRPC, app
    from openfocus.models import Goal, Task

    await COMPANION_GRPC.start()
    assert COMPANION_GRPC.bound_addr

    stop = asyncio.Event()
    async with anyio.create_task_group() as tg:
        async def _run():
            await run_companion(grpc_addr=COMPANION_GRPC.bound_addr, stop_event=stop)

        tg.start_soon(_run)

        # create a goal/task
        with session_scope() as s:
            g = Goal(content="g", description="d", due_date=dt.date.today())
            s.add(g)
            s.flush()
            t = Task(goal_id=g.id, title="t", description="d", status="todo")
            s.add(t)
            s.flush()
            task_pid = t.public_id

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            comp = await _wait_until_companion_registered(client)
            cid = comp["id"]
            assert comp["status"] == "pending_certification"

            # 点击“认证”会先生成 10 分钟有效的配对码（服务端通过 gRPC 向 Companion 请求）
            r = await client.post(f"/api/companions/{cid}/pairing_code")
            assert r.status_code == 200
            assert r.json().get("expires_at")

            # pair (OpenFocus -> gRPC -> Companion)
            r = await client.post(f"/api/companions/{cid}/pair", json={"code": "A1B2C3D4E5"})
            assert r.status_code == 200

            # choose directory (OpenFocus -> gRPC -> Companion)
            r = await client.post(f"/api/companions/{cid}/choose_directory")
            assert r.status_code == 200
            assert r.json()["path"] == str(tmp_path / "ws")

            # create agent space bound to companion
            r = await client.post(
                f"/api/tasks/{task_pid}/agent_space",
                json={"companion_id": cid, "root_path": str(tmp_path / "ws"), "agent_type": "trae-cli"},
            )
            assert r.status_code == 200

            r = await client.get(f"/api/tasks/{task_pid}/agent_space")
            assert r.status_code == 200
            space = r.json()["space"]
            assert space["companion_id"] == cid

        stop.set()
        tg.cancel_scope.cancel()

    await COMPANION_GRPC.stop()


@pytest.mark.anyio
async def test_companion_restart_reuses_server_companion_id(tmp_path):
    os.environ["OPENFOCUS_COMPANION_STATE"] = str(tmp_path / "companion_state.json")
    os.environ["OPENFOCUS_TEST_PAIRING_CODE"] = "A1B2C3D4E5"

    os.environ["OPENFOCUS_GRPC_AUTOSTART"] = "0"
    os.environ["OPENFOCUS_GRPC_PORT"] = "0"

    from openfocus.companion import run_companion
    from openfocus.main import COMPANION_GRPC, app

    await COMPANION_GRPC.start()
    assert COMPANION_GRPC.bound_addr

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 第一次启动
        stop1 = asyncio.Event()
        async with anyio.create_task_group() as tg1:
            async def _run1():
                await run_companion(grpc_addr=COMPANION_GRPC.bound_addr, stop_event=stop1)

            tg1.start_soon(_run1)
            comp1 = await _wait_until_companion_registered(client)
            cid1 = comp1["id"]
            stop1.set()
            tg1.cancel_scope.cancel()

        # 第二次启动（复用同一份 state）
        stop2 = asyncio.Event()
        async with anyio.create_task_group() as tg2:
            async def _run2():
                await run_companion(grpc_addr=COMPANION_GRPC.bound_addr, stop_event=stop2)

            tg2.start_soon(_run2)
            comp2 = await _wait_until_companion_registered(client)
            cid2 = comp2["id"]
            assert cid2 == cid1
            stop2.set()
            tg2.cancel_scope.cancel()

    await COMPANION_GRPC.stop()


@pytest.mark.anyio
async def test_companion_pair_rate_limited_per_minute_grpc(tmp_path):
    os.environ["OPENFOCUS_COMPANION_STATE"] = str(tmp_path / "companion_state.json")
    os.environ["OPENFOCUS_TEST_PAIRING_CODE"] = "A1B2C3D4E5"

    os.environ["OPENFOCUS_GRPC_AUTOSTART"] = "0"
    os.environ["OPENFOCUS_GRPC_PORT"] = "0"

    from openfocus.companion import run_companion
    from openfocus.main import COMPANION_GRPC, app

    await COMPANION_GRPC.start()
    assert COMPANION_GRPC.bound_addr

    stop = asyncio.Event()
    async with anyio.create_task_group() as tg:
        async def _run():
            await run_companion(grpc_addr=COMPANION_GRPC.bound_addr, stop_event=stop)

        tg.start_soon(_run)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            comp = await _wait_until_companion_registered(client)
            cid = comp["id"]

            # wrong code 10 times => not yet rate limited
            for _ in range(10):
                r = await client.post(f"/api/companions/{cid}/pair", json={"code": "0000000000"})
                assert r.status_code == 502

            # 11th within same minute => 429
            r = await client.post(f"/api/companions/{cid}/pair", json={"code": "0000000000"})
            assert r.status_code == 429

        stop.set()
        tg.cancel_scope.cancel()

    await COMPANION_GRPC.stop()
