from __future__ import annotations

import base64
import asyncio
import datetime as dt
import os

import pytest
from httpx import ASGITransport, AsyncClient


_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/6X9G3cAAAAASUVORK5CYII="
)


async def _wait_until_companion_ready(client: AsyncClient, *, timeout_s: float = 2.0) -> dict:
    """等待 Companion 完成注册，并且 gRPC 长连接已进入 registry。"""

    from openfocus.main import COMPANION_GRPC

    deadline = asyncio.get_running_loop().time() + float(timeout_s)
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
    raise AssertionError(f"companion not ready within {timeout_s}s, last={last}")


def test_companion_grpc_connect_pair_choose_directory_and_create_agent_space(tmp_path):
    async def _run() -> None:
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
        from openfocus.db import get_engine, session_scope
        from openfocus.main import COMPANION_GRPC, app
        from openfocus.models import Base, Event, Goal, Task

        Base.metadata.create_all(bind=get_engine())

        await COMPANION_GRPC.start()
        assert COMPANION_GRPC.bound_addr

        stop = asyncio.Event()
        comp_task = asyncio.create_task(run_companion(grpc_addr=COMPANION_GRPC.bound_addr, stop_event=stop))
        try:
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
                comp = await _wait_until_companion_ready(client)
                cid = comp["id"]
                assert comp["status"] == "pending_certification"

                # 点击“认证”会先生成 10 分钟有效的配对码（服务端通过 gRPC 向 Companion 请求）
                r = await client.post(f"/api/companions/{cid}/pairing_code")
                assert r.status_code == 200
                assert r.json().get("expires_at")

                # 申请配对码事件应落库
                with session_scope() as s:
                    assert (
                        s.query(Event).filter(Event.kind == "companion.pairing_code.requested").count() >= 1
                    )

                # pair (OpenFocus -> gRPC -> Companion)
                r = await client.post(f"/api/companions/{cid}/pair", json={"code": "A1B2C3D4E5"})
                assert r.status_code == 200

                # 配对尝试 + 配对成功事件应落库
                with session_scope() as s:
                    assert s.query(Event).filter(Event.kind == "companion.pair.attempted").count() >= 1
                    assert s.query(Event).filter(Event.kind == "companion.paired").count() >= 1

                # choose directory (OpenFocus -> gRPC -> Companion)
                r = await client.post(f"/api/companions/{cid}/choose_directory")
                assert r.status_code == 200
                assert r.json()["path"] == str(tmp_path / "ws")

                # create agent space bound to companion
                r = await client.post(
                    f"/api/tasks/{task_pid}/agent_space",
                    json={"companion_id": cid, "root_path": str(tmp_path / "ws")},
                )
                assert r.status_code == 200

                r = await client.get(f"/api/tasks/{task_pid}/agent_space")
                assert r.status_code == 200
                space = r.json()["space"]
                assert space["companion_id"] == cid
        finally:
            stop.set()
            await asyncio.wait_for(comp_task, timeout=5.0)
            await COMPANION_GRPC.stop()

    asyncio.run(_run())


def test_agent_space_files_list_read_and_raw_preview_via_grpc(tmp_path):
    async def _run() -> None:
        os.environ["OPENFOCUS_COMPANION_STATE"] = str(tmp_path / "companion_state.json")
        os.environ["OPENFOCUS_TEST_PAIRING_CODE"] = "A1B2C3D4E5"

        os.environ["OPENFOCUS_GRPC_AUTOSTART"] = "0"
        os.environ["OPENFOCUS_GRPC_PORT"] = "0"

        from openfocus.companion import run_companion
        from openfocus.db import session_scope
        from openfocus.main import COMPANION_GRPC, app
        from openfocus.models import Goal, Task

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        (ws / "README.md").write_text("# Title\n\nHello\n", encoding="utf-8")
        (ws / "img.png").write_bytes(_PNG_1x1)
        (ws / "sub").mkdir()
        (ws / "sub" / "a.txt").write_text("ok", encoding="utf-8")

        await COMPANION_GRPC.start()
        assert COMPANION_GRPC.bound_addr

        stop = asyncio.Event()
        comp_task = asyncio.create_task(run_companion(grpc_addr=COMPANION_GRPC.bound_addr, stop_event=stop))
        try:
            with session_scope() as s:
                g = Goal(content="g", description="d", due_date=dt.date.today())
                s.add(g)
                s.flush()
                t = Task(goal_id=g.id, title="t", description="d", status="todo")
                s.add(t)
                s.flush()
                pid = t.public_id

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                comp = await _wait_until_companion_ready(client)
                cid = comp["id"]

                # pairing
                r = await client.post(f"/api/companions/{cid}/pairing_code")
                assert r.status_code == 200
                r = await client.post(f"/api/companions/{cid}/pair", json={"code": "A1B2C3D4E5"})
                assert r.status_code == 200

                # create agent space
                r = await client.post(
                    f"/api/tasks/{pid}/agent_space",
                    json={"companion_id": cid, "root_path": str(ws)},
                )
                assert r.status_code == 200
                space_id = r.json()["space_id"]

                r = await client.get(f"/api/agent_spaces/{space_id}/files/list", params={"path": ""})
                assert r.status_code == 200
                names = {e["name"] for e in r.json()["entries"]}
                assert {"hello.py", "README.md", "img.png", "sub"}.issubset(names)

                r = await client.get(f"/api/agent_spaces/{space_id}/files/read", params={"path": "hello.py"})
                assert r.status_code == 200
                assert "print('hi')" in r.json()["content"]

                r = await client.get(f"/api/agent_spaces/{space_id}/files/list", params={"path": "sub"})
                assert r.status_code == 200
                names2 = {e["name"] for e in r.json()["entries"]}
                assert "a.txt" in names2

                r = await client.get(f"/api/agent_spaces/{space_id}/files/raw", params={"path": "img.png"})
                assert r.status_code == 200
                assert r.headers.get("content-type", "").startswith("image/png")
                assert r.content[:8] == _PNG_1x1[:8]
        finally:
            stop.set()
            await asyncio.wait_for(comp_task, timeout=5.0)
            await COMPANION_GRPC.stop()

    asyncio.run(_run())


def test_agent_space_files_path_traversal_is_blocked_via_grpc(tmp_path):
    async def _run() -> None:
        os.environ["OPENFOCUS_COMPANION_STATE"] = str(tmp_path / "companion_state.json")
        os.environ["OPENFOCUS_TEST_PAIRING_CODE"] = "A1B2C3D4E5"

        os.environ["OPENFOCUS_GRPC_AUTOSTART"] = "0"
        os.environ["OPENFOCUS_GRPC_PORT"] = "0"

        from openfocus.companion import run_companion
        from openfocus.db import session_scope
        from openfocus.main import COMPANION_GRPC, app
        from openfocus.models import Goal, Task

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("ok", encoding="utf-8")
        outside = tmp_path / "outside.txt"
        outside.write_text("no", encoding="utf-8")
        (ws / "link").symlink_to(outside)

        await COMPANION_GRPC.start()
        assert COMPANION_GRPC.bound_addr

        stop = asyncio.Event()
        comp_task = asyncio.create_task(run_companion(grpc_addr=COMPANION_GRPC.bound_addr, stop_event=stop))
        try:
            with session_scope() as s:
                g = Goal(content="g", description="d", due_date=dt.date.today())
                s.add(g)
                s.flush()
                t = Task(goal_id=g.id, title="t", description="d", status="todo")
                s.add(t)
                s.flush()
                pid = t.public_id

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                comp = await _wait_until_companion_ready(client)
                cid = comp["id"]

                r = await client.post(f"/api/companions/{cid}/pairing_code")
                assert r.status_code == 200
                r = await client.post(f"/api/companions/{cid}/pair", json={"code": "A1B2C3D4E5"})
                assert r.status_code == 200

                r = await client.post(
                    f"/api/tasks/{pid}/agent_space",
                    json={"companion_id": cid, "root_path": str(ws)},
                )
                assert r.status_code == 200
                space_id = r.json()["space_id"]

                r = await client.get(f"/api/agent_spaces/{space_id}/files/read", params={"path": "../outside.txt"})
                assert r.status_code == 400

                r = await client.get(f"/api/agent_spaces/{space_id}/files/read", params={"path": str(outside)})
                assert r.status_code == 400

                r = await client.get(f"/api/agent_spaces/{space_id}/files/read", params={"path": "link"})
                assert r.status_code == 400
        finally:
            stop.set()
            await asyncio.wait_for(comp_task, timeout=5.0)
            await COMPANION_GRPC.stop()

    asyncio.run(_run())


def test_agent_space_agent_new_session_send_stream_and_persist(tmp_path):
    async def _run() -> None:
        os.environ["OPENFOCUS_DB_PATH"] = str(tmp_path / "openfocus_test.db")
        from openfocus.db import reset_engine

        reset_engine()

        os.environ["OPENFOCUS_COMPANION_STATE"] = str(tmp_path / "companion_state.json")
        os.environ["OPENFOCUS_TEST_PAIRING_CODE"] = "A1B2C3D4E5"
        os.environ["OPENFOCUS_TEST_AGENT_ECHO"] = "1"

        os.environ["OPENFOCUS_GRPC_AUTOSTART"] = "0"
        os.environ["OPENFOCUS_GRPC_PORT"] = "0"

        from openfocus.companion import run_companion
        from openfocus.db import get_engine, session_scope
        from openfocus.main import COMPANION_GRPC, app
        from openfocus.models import Base, Goal, Task

        Base.metadata.create_all(bind=get_engine())

        ws = tmp_path / "ws"
        ws.mkdir()

        await COMPANION_GRPC.start()
        assert COMPANION_GRPC.bound_addr

        stop = asyncio.Event()
        comp_task = asyncio.create_task(run_companion(grpc_addr=COMPANION_GRPC.bound_addr, stop_event=stop))
        try:
            with session_scope() as s:
                g = Goal(content="g", description="d", due_date=dt.date.today())
                s.add(g)
                s.flush()
                t = Task(goal_id=g.id, title="t", description="d", status="todo")
                s.add(t)
                s.flush()
                pid = t.public_id

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                comp = await _wait_until_companion_ready(client)
                cid = comp["id"]

                # pairing
                r = await client.post(f"/api/companions/{cid}/pairing_code")
                assert r.status_code == 200
                r = await client.post(f"/api/companions/{cid}/pair", json={"code": "A1B2C3D4E5"})
                assert r.status_code == 200

                # create agent space
                r = await client.post(
                    f"/api/tasks/{pid}/agent_space",
                    json={"companion_id": cid, "root_path": str(ws)},
                )
                assert r.status_code == 200
                space_id = r.json()["space_id"]

                # new session
                r = await client.post(f"/api/agent_spaces/{space_id}/agent/sessions/new")
                assert r.status_code == 200
                sid = r.json()["session"]["session_id"]
                assert sid

                # send message
                r = await client.post(
                    f"/api/agent_spaces/{space_id}/agent/sessions/{sid}/send",
                    json={"text": "hello"},
                )
                assert r.status_code == 200
                rid = r.json().get("request_id")
                assert rid

                # wait until assistant message is persisted (AgentChunk -> DB)
                deadline = asyncio.get_running_loop().time() + 2.0
                content = ""
                while asyncio.get_running_loop().time() < deadline:
                    rr = await client.get(f"/api/agent_spaces/{space_id}/agent/sessions/{sid}/messages")
                    assert rr.status_code == 200
                    msgs = rr.json().get("messages") or []
                    hit = [m for m in msgs if m.get("role") == "assistant" and m.get("request_id") == rid]
                    if hit:
                        m = hit[-1]
                        content = m.get("content") or ""
                        if m.get("done") is True and content:
                            break
                    await asyncio.sleep(0.02)

                # persist: assistant message should contain injected header and user prompt
                assert content
                assert "taskId=" in content
                assert pid in content
                assert "hello" in content
        finally:
            stop.set()
            await asyncio.wait_for(comp_task, timeout=5.0)
            await COMPANION_GRPC.stop()

    asyncio.run(_run())


def test_agent_space_agent_offline_returns_502(tmp_path):
    async def _run() -> None:
        os.environ["OPENFOCUS_DB_PATH"] = str(tmp_path / "openfocus_test.db")
        from openfocus.db import reset_engine

        reset_engine()
        os.environ["OPENFOCUS_GRPC_AUTOSTART"] = "0"

        from openfocus.db import session_scope
        from openfocus.main import app
        from openfocus.db import get_engine
        from openfocus.models import Base, Companion, Goal, Task

        Base.metadata.create_all(bind=get_engine())

        # create a goal/task + a fake active companion (but no online gRPC connection)
        with session_scope() as s:
            g = Goal(content="g", description="d", due_date=dt.date.today())
            s.add(g)
            s.flush()
            t = Task(goal_id=g.id, title="t", description="d", status="todo")
            s.add(t)
            s.flush()
            pid = t.public_id
            c = Companion(
                device_id="dev_test_offline",
                name="offline",
                base_url="grpc://",
                status="active",
                auth_token="tok",
                last_seen_at=dt.datetime.now(dt.timezone.utc),
            )
            s.add(c)
            s.flush()
            cid = c.id

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                f"/api/tasks/{pid}/agent_space",
                json={"companion_id": cid, "root_path": str(tmp_path)},
            )
            assert r.status_code == 200
            space_id = r.json()["space_id"]

            r = await client.post(f"/api/agent_spaces/{space_id}/agent/sessions/new")
            assert r.status_code == 502

    asyncio.run(_run())


def test_companion_delete_unbinds_agent_spaces_and_removes_companion(tmp_path):
    async def _run() -> None:
        os.environ["OPENFOCUS_DB_PATH"] = str(tmp_path / "openfocus_test.db")
        from openfocus.db import reset_engine

        reset_engine()
        os.environ["OPENFOCUS_GRPC_AUTOSTART"] = "0"

        from openfocus.db import get_engine, session_scope
        from openfocus.main import app
        from openfocus.models import AgentSpace, Base, Companion

        Base.metadata.create_all(bind=get_engine())

        with session_scope() as s:
            c = Companion(
                device_id="dev_test_delete",
                name="to_delete",
                base_url="grpc://",
                status="active",
                auth_token="tok",
                last_seen_at=dt.datetime.now(dt.timezone.utc),
            )
            s.add(c)
            s.flush()
            cid = c.id

            sp = AgentSpace(
                task_public_id="31b8ef3d-cb5d-4074-8bb3-5144e01e04d9",
                companion_id=cid,
                root_path=str(tmp_path),
                agent_type="trae-cli",
            )
            s.add(sp)
            s.flush()
            space_id = sp.id

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.delete(f"/api/companions/{cid}")
            assert r.status_code == 200
            assert r.json().get("ok") is True
            assert int(r.json().get("unbound_spaces") or 0) == 1

        with session_scope() as s:
            assert s.get(Companion, cid) is None
            sp2 = s.get(AgentSpace, space_id)
            assert sp2 is not None
            assert sp2.companion_id is None

    asyncio.run(_run())


def test_companion_restart_reuses_server_companion_id(tmp_path):
    async def _run() -> None:
        os.environ["OPENFOCUS_DB_PATH"] = str(tmp_path / "openfocus_test.db")
        from openfocus.db import reset_engine

        reset_engine()
        os.environ["OPENFOCUS_COMPANION_STATE"] = str(tmp_path / "companion_state.json")
        os.environ["OPENFOCUS_TEST_PAIRING_CODE"] = "A1B2C3D4E5"

        os.environ["OPENFOCUS_GRPC_AUTOSTART"] = "0"
        os.environ["OPENFOCUS_GRPC_PORT"] = "0"

        from openfocus.companion import run_companion
        from openfocus.db import get_engine
        from openfocus.main import COMPANION_GRPC, app
        from openfocus.models import Base

        Base.metadata.create_all(bind=get_engine())
        from openfocus.db import session_scope
        from openfocus.models import Event

        await COMPANION_GRPC.start()
        assert COMPANION_GRPC.bound_addr

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 第一次启动
            stop1 = asyncio.Event()
            t1 = asyncio.create_task(run_companion(grpc_addr=COMPANION_GRPC.bound_addr, stop_event=stop1))
            comp1 = await _wait_until_companion_ready(client)
            cid1 = comp1["id"]
            stop1.set()
            await asyncio.wait_for(t1, timeout=5.0)

            # 第二次启动（复用同一份 state）
            stop2 = asyncio.Event()
            t2 = asyncio.create_task(run_companion(grpc_addr=COMPANION_GRPC.bound_addr, stop_event=stop2))
            comp2 = await _wait_until_companion_ready(client)
            cid2 = comp2["id"]
            assert cid2 == cid1
            stop2.set()
            await asyncio.wait_for(t2, timeout=5.0)

        await COMPANION_GRPC.stop()

    asyncio.run(_run())


def test_companion_pair_rate_limited_per_minute_grpc(tmp_path):
    async def _run() -> None:
        os.environ["OPENFOCUS_COMPANION_STATE"] = str(tmp_path / "companion_state.json")
        os.environ["OPENFOCUS_TEST_PAIRING_CODE"] = "A1B2C3D4E5"

        os.environ["OPENFOCUS_GRPC_AUTOSTART"] = "0"
        os.environ["OPENFOCUS_GRPC_PORT"] = "0"

        from openfocus.companion import run_companion
        from openfocus.main import COMPANION_GRPC, app

        await COMPANION_GRPC.start()
        assert COMPANION_GRPC.bound_addr

        stop = asyncio.Event()
        comp_task = asyncio.create_task(run_companion(grpc_addr=COMPANION_GRPC.bound_addr, stop_event=stop))
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                comp = await _wait_until_companion_ready(client)
                cid = comp["id"]

                # wrong code 10 times => not yet rate limited
                for _ in range(10):
                    r = await client.post(f"/api/companions/{cid}/pair", json={"code": "0000000000"})
                    assert r.status_code == 502

                # 11th within same minute => 429
                r = await client.post(f"/api/companions/{cid}/pair", json={"code": "0000000000"})
                assert r.status_code == 429
        finally:
            stop.set()
            await asyncio.wait_for(comp_task, timeout=5.0)
            await COMPANION_GRPC.stop()

    asyncio.run(_run())
