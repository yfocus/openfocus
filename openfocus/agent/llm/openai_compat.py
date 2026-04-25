from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .types import LLMCallResult


@dataclass
class OpenAICompatConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 120.0
    retry_attempts: int = 3


class OpenAICompatibleProvider:
    """最小 OpenAI-compatible Provider（参考 honcho 的调用结构与 token usage 解析）。

    环境变量（按优先级选择一套配置；只要命中其中一套就可用）：

    1) OpenAI-compatible（默认）
    - `OPENFOCUS_OPENAI_BASE_URL`（例如 `https://api.openai.com/v1` 或自建网关 `/v1`）
    - `OPENFOCUS_OPENAI_API_KEY`
    - `OPENFOCUS_OPENAI_MODEL`

    2) 火山方舟 Ark（OpenAI-compatible；提供别名以兼容其他项目的 .env）
    - `OPENFOCUS_ARK_BASE_URL` / `ARK_BASE_URL`（例如 `https://ark.cn-beijing.volces.com/api/v3`）
    - `OPENFOCUS_ARK_API_KEY` / `ARK_API_KEY`
    - `OPENFOCUS_ARK_MODEL` / `ARK_MODEL`
    """

    def __init__(self, cfg: OpenAICompatConfig):
        self.cfg = cfg

    @classmethod
    def from_env(cls) -> "OpenAICompatibleProvider":
        def _first_env(*names: str) -> str:
            for n in names:
                v = os.environ.get(n, "")
                if v:
                    return v
            return ""

        # 1) OpenAI-compatible（优先）
        openai_api_key = _first_env("OPENFOCUS_OPENAI_API_KEY")
        if openai_api_key:
            base_url = _first_env("OPENFOCUS_OPENAI_BASE_URL") or "https://api.openai.com/v1"
            model = _first_env("OPENFOCUS_OPENAI_MODEL") or "gpt-4.1-mini"
            return cls(OpenAICompatConfig(base_url=base_url.rstrip("/"), api_key=openai_api_key, model=model))

        # 2) Ark（OpenAI-compatible 兼容层）
        ark_api_key = _first_env("OPENFOCUS_ARK_API_KEY", "ARK_API_KEY")
        if ark_api_key:
            base_url = (
                _first_env("OPENFOCUS_ARK_BASE_URL", "ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3"
            )
            model = _first_env("OPENFOCUS_ARK_MODEL", "ARK_MODEL", "OPENFOCUS_OPENAI_MODEL") or "doubao-seed-1-6"
            return cls(OpenAICompatConfig(base_url=base_url.rstrip("/"), api_key=ark_api_key, model=model))

        raise RuntimeError(
            "缺少 LLM 配置环境变量：请设置 OPENFOCUS_OPENAI_API_KEY，或设置 OPENFOCUS_ARK_API_KEY/ARK_API_KEY（Ark）。"
        )

    def chat_completions(
        self,
        *,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMCallResult:
        url = f"{self.cfg.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # 注意：部分 OpenAI-compatible 网关可能不支持 tools/response_format。
        # 我们仍然先按 OpenAI 语义发送；若返回 400，再做兼容性降级重试。
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format

        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
        }

        last_err: Exception | None = None
        last_err_body: str | None = None
        for attempt in range(1, self.cfg.retry_attempts + 1):
            try:
                req = urllib.request.Request(url=url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.cfg.timeout_seconds) as resp:
                    raw = resp.read().decode("utf-8")
                obj = json.loads(raw)

                choice = (obj.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                content = message.get("content") or ""
                tool_calls = message.get("tool_calls")
                finish_reason = choice.get("finish_reason")

                usage = obj.get("usage") or {}
                # OpenAI-style
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")
                total_tokens = usage.get("total_tokens")

                # cache token（兼容不同网关字段；参考 honcho 的 extract_openai_cache_tokens 思路）
                prompt_details = usage.get("prompt_tokens_details") or {}
                cached_tokens = prompt_details.get("cached_tokens") or usage.get("cached_tokens")
                cache_read_input_tokens = usage.get("cache_read_input_tokens")
                cache_creation_input_tokens = usage.get("cache_creation_input_tokens")

                return LLMCallResult(
                    content=content,
                    finish_reason=finish_reason,
                    tool_calls=tool_calls,
                    usage={
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                        "cached_tokens": cached_tokens,
                        "cache_read_input_tokens": cache_read_input_tokens,
                        "cache_creation_input_tokens": cache_creation_input_tokens,
                        "raw": usage,
                    },
                )

            except urllib.error.HTTPError as e:
                last_err = e
                try:
                    last_err_body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    last_err_body = None

                # 兼容性降级：如果网关返回 400，尝试去掉 response_format / tools 后重试。
                if e.code == 400:
                    changed = False
                    if "response_format" in payload:
                        payload.pop("response_format", None)
                        response_format = None
                        changed = True
                    if "tools" in payload:
                        payload.pop("tools", None)
                        payload.pop("tool_choice", None)
                        tools = None
                        changed = True
                    if changed:
                        data = json.dumps(payload).encode("utf-8")
                        if attempt < self.cfg.retry_attempts:
                            continue

                # honcho 风格：temperature 为 0 时，重试第 2 次开始略微升温
                if temperature == 0.0 and attempt > 1:
                    temperature = 0.2
                    payload["temperature"] = temperature
                    data = json.dumps(payload).encode("utf-8")

                if attempt < self.cfg.retry_attempts:
                    sleep_s = min(10.0, max(4.0, 2 ** attempt))
                    time.sleep(sleep_s)
                    continue
                break

            except (urllib.error.URLError, TimeoutError) as e:
                last_err = e
                # honcho 风格：temperature 为 0 时，重试第 2 次开始略微升温
                if temperature == 0.0 and attempt > 1:
                    temperature = 0.2
                    payload["temperature"] = temperature
                    data = json.dumps(payload).encode("utf-8")

                if attempt < self.cfg.retry_attempts:
                    # 指数退避（对齐 honcho 的 4~10s 区间）
                    sleep_s = min(10.0, max(4.0, 2 ** attempt))
                    time.sleep(sleep_s)
                    continue
                break

        detail = f"{last_err}"
        if last_err_body:
            detail += f"\nresponse_body={last_err_body}"
        raise RuntimeError(
            f"LLM 调用失败（attempts={self.cfg.retry_attempts} base_url={self.cfg.base_url} model={self.cfg.model}）：{detail}"
        )
