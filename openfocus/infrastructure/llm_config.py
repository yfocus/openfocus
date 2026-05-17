# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from ..agent.llm.openai_compat import OpenAICompatibleProvider
from . import env as env_loader

_DOTENV_LOADED = False


def load_dotenv_once() -> None:
    """Best-effort load `.env` into process env (only if variables are missing)."""

    global _DOTENV_LOADED
    if not _DOTENV_LOADED:
        env_loader._DOTENV_LOADED = False
    env_loader.load_dotenv_once()
    _DOTENV_LOADED = env_loader._DOTENV_LOADED


def get_llm_provider_or_error() -> tuple[OpenAICompatibleProvider | None, str | None]:
    load_dotenv_once()
    try:
        return OpenAICompatibleProvider.from_env(), None
    except Exception as e:
        return None, (
            "Missing LLM configuration. LLM-powered features are unavailable.\n"
            "Set one of the following environment variable groups:\n"
            "- OpenAI-compatible: OPENFOCUS_OPENAI_API_KEY (optionally OPENFOCUS_OPENAI_BASE_URL / OPENFOCUS_OPENAI_MODEL)\n"
            "- Ark: OPENFOCUS_ARK_API_KEY (or ARK_API_KEY), plus OPENFOCUS_ARK_BASE_URL / OPENFOCUS_ARK_MODEL (or ARK_BASE_URL / ARK_MODEL)\n"
            "You can also place a `.env` file in the startup directory (see `.env-default` at the repo root), or point to one with OPENFOCUS_ENV_FILE.\n"
            f"Error: {e}"
        )
