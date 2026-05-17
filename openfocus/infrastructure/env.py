# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
from pathlib import Path

_DOTENV_LOADED = False


def load_dotenv_once(*, repo_root: Path | None = None) -> None:
    """Best-effort load `.env` into process env without overriding existing vars."""

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
        root = (
            repo_root if repo_root is not None else Path(__file__).resolve().parents[2]
        )
        repo_env = root / ".env"
        if repo_env not in candidates:
            candidates.append(repo_env)
    except Exception:
        pass

    for path in candidates:
        try:
            if not path.exists() or not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                parsed = _parse_dotenv_line(line)
                if not parsed:
                    continue
                key, value = parsed
                if key not in os.environ and value != "":
                    os.environ[key] = value
            break
        except Exception:
            continue


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    raw = (line or "").strip()
    if not raw or raw.startswith("#"):
        return None
    if raw.startswith("export "):
        raw = raw[len("export ") :].lstrip()
    if "=" not in raw:
        return None
    key, value = raw.split("=", 1)
    key = (key or "").strip()
    if not key:
        return None
    value = (value or "").strip()
    if value and not (value.startswith('"') or value.startswith("'")):
        hash_idx = value.find("#")
        if hash_idx >= 0:
            before = value[:hash_idx]
            if before.rstrip() != before:
                value = before.strip()
    return key, _strip_quotes(value)


def _strip_quotes(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and (
        (text[0] == text[-1] == '"') or (text[0] == text[-1] == "'")
    ):
        return text[1:-1]
    return text
