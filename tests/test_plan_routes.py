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


def test_llm_provider_env_supports_ark(monkeypatch):
    # 验证：项目支持火山 Ark（OpenAI-compatible）环境变量别名。
    from openfocus.agent.llm.openai_compat import OpenAICompatibleProvider

    monkeypatch.delenv("OPENFOCUS_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENFOCUS_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENFOCUS_OPENAI_MODEL", raising=False)

    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    monkeypatch.setenv("ARK_BASE_URL", "https://ark.example.com/api/v3")
    monkeypatch.setenv("ARK_MODEL", "doubao-test")

    p = OpenAICompatibleProvider.from_env()
    assert p.cfg.api_key == "test-ark-key"
    assert p.cfg.base_url == "https://ark.example.com/api/v3"
    assert p.cfg.model == "doubao-test"


def test_llm_provider_env_openai_takes_precedence(monkeypatch):
    # 验证：若同时配置 OpenAI 与 Ark，则优先使用 OpenAI 配置。
    from openfocus.agent.llm.openai_compat import OpenAICompatibleProvider

    monkeypatch.setenv("OPENFOCUS_OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENFOCUS_OPENAI_BASE_URL", "https://openai.example.com/v1")
    monkeypatch.setenv("OPENFOCUS_OPENAI_MODEL", "gpt-test")

    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    monkeypatch.setenv("ARK_BASE_URL", "https://ark.example.com/api/v3")
    monkeypatch.setenv("ARK_MODEL", "doubao-test")

    p = OpenAICompatibleProvider.from_env()
    assert p.cfg.api_key == "test-openai-key"
    assert p.cfg.base_url == "https://openai.example.com/v1"
    assert p.cfg.model == "gpt-test"
