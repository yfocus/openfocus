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


def test_openai_compat_fallback_on_400_removes_tools_and_response_format(monkeypatch):
    import io
    import json
    import urllib.error
    import urllib.request

    from openfocus.agent.llm.openai_compat import OpenAICompatConfig, OpenAICompatibleProvider

    calls: list[dict] = []

    class _Resp:
        def __init__(self, raw: str):
            self._raw = raw.encode("utf-8")

        def read(self):
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req: urllib.request.Request, timeout: float):
        payload = json.loads((req.data or b"{}").decode("utf-8"))
        calls.append(payload)
        if len(calls) == 1:
            assert "response_format" in payload
            assert "tools" in payload
            fp = io.BytesIO(b"{\"error\":\"unsupported response_format/tools\"}")
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=fp)
        assert "response_format" not in payload
        assert "tools" not in payload
        return _Resp(
            json.dumps(
                {
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            )
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    p = OpenAICompatibleProvider(OpenAICompatConfig(base_url="https://x/v1", api_key="k", model="m", retry_attempts=3))
    res = p.chat_completions(
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.0,
        max_tokens=10,
        tools=[{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}],
        response_format={"type": "json_object"},
    )
    assert res.content == "ok"
    assert len(calls) == 2
