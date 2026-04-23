from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.anyio
async def test_plan_route_not_swallowed_by_goal_id():
    # 不配置 LLM key，确保 plan 页面能正常展示“不可用”提示，而不是报错。
    import os

    os.environ.pop("OPENFOCUS_OPENAI_API_KEY", None)

    from openfocus.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/goals/plan")
        assert r.status_code == 200
