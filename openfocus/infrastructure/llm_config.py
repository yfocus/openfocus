# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
from pathlib import Path

from ..agent.llm.openai_compat import OpenAICompatibleProvider

_DOTENV_LOADED = False


def load_dotenv_once() -> None:
    """Best-effort load `.env` into process env (only if variables are missing)."""

    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True

    candidates: list[Path] = []
    env_file = str(os.environ.get("OPENFOCUS_ENV_FILE") or "").strip()

    mode = str(os.environ.get("OPENFOCUS_DOTENV") or "auto").strip().lower()
    if mode in {"0", "false", "off", "no"}:
        return
    if mode == "auto" and os.environ.get("PYTEST_CURRENT_TEST") and not env_file:
        return

    if env_file:
        try:
            candidates.append(Path(env_file).expanduser())
        except Exception:
            pass
    candidates.append(Path.cwd() / ".env")
    try:
        repo_env = Path(__file__).resolve().parent.parent.parent / ".env"
        if repo_env not in candidates:
            candidates.append(repo_env)
    except Exception:
        pass

    def _strip_quotes(v: str) -> str:
        s = (v or "").strip()
        if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
            return s[1:-1]
        return s

    def _parse_line(line: str) -> tuple[str, str] | None:
        raw = (line or "").strip()
        if not raw or raw.startswith("#"):
            return None
        if raw.startswith("export "):
            raw = raw[len("export ") :].lstrip()
        if "=" not in raw:
            return None
        k, v = raw.split("=", 1)
        key = (k or "").strip()
        if not key:
            return None
        val = (v or "").strip()
        if val and not (val.startswith('"') or val.startswith("'")):
            hash_idx = val.find("#")
            if hash_idx >= 0:
                before = val[:hash_idx]
                if before.rstrip() != before:
                    val = before.strip()
        return key, _strip_quotes(val)

    for p in candidates:
        try:
            if not p.exists() or not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                kv = _parse_line(line)
                if not kv:
                    continue
                key, val = kv
                if key not in os.environ and val != "":
                    os.environ[key] = val
            break
        except Exception:
            continue


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
