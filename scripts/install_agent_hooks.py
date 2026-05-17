#!/usr/bin/env python3

# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E402, I001
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from openfocus.infrastructure import env as env_config  # noqa: E402

CODEX_HOOK = REPO_ROOT / "openfocus" / "hooks" / "openfocus-codex-hook.sh"
COCO_HOOK = REPO_ROOT / "openfocus" / "hooks" / "openfocus-coco-hook.sh"
CODEX_EVENTS = {
    "SessionStart": ("session-start", 5),
    "UserPromptSubmit": ("user-prompt-submit", 5),
    "PermissionRequest": ("permission-request", 600),
    "Stop": ("stop", 5),
}
NON_CANONICAL_CODEX_EVENTS = {
    "sessionStart",
    "userPromptSubmit",
    "permissionRequest",
    "preToolUse",
    "postToolUse",
    "preCompact",
    "postCompact",
    "session_start",
    "user_prompt_submit",
    "permission_request",
    "pre_tool_use",
    "post_tool_use",
    "pre_compact",
    "post_compact",
}


def _backup(path: Path, *, dry_run: bool) -> Path | None:
    if not path.exists():
        return None
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.openfocus-bak-{ts}")
    if not dry_run:
        shutil.copy2(path, backup)
    return backup


def _safe_instance_id(value: str | None) -> str:
    raw = str(value or "").strip() or "default"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-._")
    return safe or "default"


def _default_hook_sock(instance_id: str) -> Path:
    iid = _safe_instance_id(instance_id)
    if iid == "default":
        return Path("~/.openfocus/hooks.sock").expanduser()
    return Path(f"~/.openfocus/hooks-{iid}.sock").expanduser()


def _default_hook_spool_dir(instance_id: str) -> Path:
    return Path(f"/tmp/openfocus-agent-hooks-{os.getuid()}") / _safe_instance_id(
        instance_id
    )


def _codex_command(
    kind: str, *, instance_id: str, hook_sock: Path, hook_spool_dir: Path
) -> str:
    return (
        f"OPENFOCUS_REGISTERED_INSTANCE_ID={shlex.quote(_safe_instance_id(instance_id))} "
        f"OPENFOCUS_HOOK_SOCK={shlex.quote(str(hook_sock))} "
        f"OPENFOCUS_HOOK_SPOOL_DIR={shlex.quote(str(hook_spool_dir))} "
        f"sh {shlex.quote(str(CODEX_HOOK))} {shlex.quote(kind)}"
    )


def _is_openfocus_codex_command(command: str) -> bool:
    return str(CODEX_HOOK) in command


def _command_targets_hook_sock(command: str, hook_sock: Path) -> bool:
    return f"OPENFOCUS_HOOK_SOCK={str(hook_sock)}" in command


def _ensure_codex_hook_entry(
    data: dict[str, Any],
    event: str,
    kind: str,
    timeout: int,
    *,
    instance_id: str,
    hook_sock: Path,
    hook_spool_dir: Path,
) -> bool:
    hooks = data.setdefault("hooks", {})
    entries = hooks.setdefault(event, [])
    command = _codex_command(
        kind,
        instance_id=instance_id,
        hook_sock=hook_sock,
        hook_spool_dir=hook_spool_dir,
    )
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if isinstance(hook, dict) and str(hook.get("command") or "") == command:
                return False
    next_entry = {
        "matcher": "*",
        "hooks": [{"type": "command", "command": command, "timeout": timeout}],
    }
    insert_at = len(entries)
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        raw_hooks = entry.get("hooks")
        if not isinstance(raw_hooks, list):
            continue
        if any(
            _is_openfocus_codex_command(str(hook.get("command") or ""))
            for hook in raw_hooks
            if isinstance(hook, dict)
        ):
            insert_at = idx
            break
    entries.insert(insert_at, next_entry)
    return True


def _prune_legacy_codex_openfocus_hooks(
    data: dict[str, Any], *, hook_sock: Path
) -> bool:
    changed = False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for event, entries in list(hooks.items()):
        if not isinstance(entries, list):
            continue
        next_entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                next_entries.append(entry)
                continue
            raw_hooks = entry.get("hooks")
            if not isinstance(raw_hooks, list):
                next_entries.append(entry)
                continue
            kept = []
            for hook in raw_hooks:
                command = (
                    str(hook.get("command") or "") if isinstance(hook, dict) else ""
                )
                if not _is_openfocus_codex_command(command):
                    kept.append(hook)
                    continue
                is_legacy_without_socket = "OPENFOCUS_HOOK_SOCK=" not in command
                is_legacy_without_spool = "OPENFOCUS_HOOK_SPOOL_DIR=" not in command
                is_same_instance = _command_targets_hook_sock(command, hook_sock)
                if (
                    is_legacy_without_socket
                    or (is_same_instance and is_legacy_without_spool)
                    or (event in NON_CANONICAL_CODEX_EVENTS and is_same_instance)
                ):
                    changed = True
                    continue
                kept.append(hook)
            if kept:
                entry = {**entry, "hooks": kept}
                next_entries.append(entry)
            else:
                changed = True
        hooks[event] = next_entries
    return changed


def install_codex(
    path: Path,
    *,
    instance_id: str = "default",
    hook_sock: Path,
    hook_spool_dir: Path,
    dry_run: bool,
) -> bool:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {"hooks": {}}
    changed = _prune_legacy_codex_openfocus_hooks(data, hook_sock=hook_sock)
    for event, (kind, timeout) in CODEX_EVENTS.items():
        changed |= _ensure_codex_hook_entry(
            data,
            event,
            kind,
            timeout,
            instance_id=instance_id,
            hook_sock=hook_sock,
            hook_spool_dir=hook_spool_dir,
        )
    if changed:
        backup = _backup(path, dry_run=dry_run)
        if backup is not None:
            verb = "would back up" if dry_run else "backed up"
            print(f"{verb} Codex config: {backup}")
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    return changed


def _coco_block(*, instance_id: str, hook_sock: Path, hook_spool_dir: Path) -> str:
    iid = _safe_instance_id(instance_id)
    command = (
        f"OPENFOCUS_REGISTERED_INSTANCE_ID={shlex.quote(iid)} "
        f"OPENFOCUS_HOOK_SOCK={shlex.quote(str(hook_sock))} "
        f"OPENFOCUS_HOOK_SPOOL_DIR={shlex.quote(str(hook_spool_dir))} "
        f"sh {shlex.quote(str(COCO_HOOK))}"
    )
    return f"""  # openfocus:coco-hook BEGIN instance={iid}
  - type: command
    command: {json.dumps(command)}
    timeout: '10s'
    matchers:
      - event: pre_tool_use
      - event: post_tool_use
      - event: post_tool_use_failure
      - event: user_prompt_submit
      - event: stop
      - event: subagent_start
      - event: subagent_stop
      - event: session_start
      - event: session_end
      - event: pre_compact
      - event: post_compact
      - event: notification
      - event: permission_request
  # openfocus:coco-hook END instance={iid}
"""


def _remove_legacy_coco_block(text: str) -> tuple[str, bool]:
    pattern = re.compile(
        r"  # openfocus:coco-hook BEGIN\n.*?  # openfocus:coco-hook END\n?",
        re.DOTALL,
    )
    new_text, count = pattern.subn("", text)
    return new_text, count > 0


def install_coco(
    path: Path,
    *,
    instance_id: str,
    hook_sock: Path,
    hook_spool_dir: Path,
    dry_run: bool,
) -> bool:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text, removed_legacy = _remove_legacy_coco_block(text)
    marker = f"openfocus:coco-hook BEGIN instance={_safe_instance_id(instance_id)}"
    command = (
        f"OPENFOCUS_REGISTERED_INSTANCE_ID={shlex.quote(_safe_instance_id(instance_id))} "
        f"OPENFOCUS_HOOK_SOCK={shlex.quote(str(hook_sock))} "
        f"OPENFOCUS_HOOK_SPOOL_DIR={shlex.quote(str(hook_spool_dir))} "
        f"sh {shlex.quote(str(COCO_HOOK))}"
    )
    if marker in text or command in text:
        if not removed_legacy:
            return False
        new_text = text
        backup = _backup(path, dry_run=dry_run)
        if backup is not None:
            verb = "would back up" if dry_run else "backed up"
            print(f"{verb} Coco config: {backup}")
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_text, encoding="utf-8")
        return True

    block = _coco_block(
        instance_id=instance_id, hook_sock=hook_sock, hook_spool_dir=hook_spool_dir
    )
    if not text.strip():
        new_text = "hooks:\n" + block
    elif "\nhooks:\n" in f"\n{text}":
        lines = text.splitlines(keepends=True)
        insert_at = len(lines)
        seen_hooks = False
        for idx, line in enumerate(lines):
            if line.startswith("hooks:"):
                seen_hooks = True
                continue
            if seen_hooks and line and not line.startswith((" ", "\t", "\n", "\r")):
                insert_at = idx
                break
        new_text = "".join(lines[:insert_at]) + block + "".join(lines[insert_at:])
    else:
        new_text = text.rstrip() + "\n\nhooks:\n" + block

    backup = _backup(path, dry_run=dry_run)
    if backup is not None:
        verb = "would back up" if dry_run else "backed up"
        print(f"{verb} Coco config: {backup}")
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
    return True


def main() -> int:
    env_config.load_dotenv_once(repo_root=REPO_ROOT)
    parser = argparse.ArgumentParser(
        description="Install OpenFocus hooks for Coco/Trae and Codex."
    )
    parser.add_argument("--coco-config", default="~/.trae/traecli.yaml")
    parser.add_argument("--codex-hooks", default="~/.codex/hooks.json")
    parser.add_argument(
        "--instance-id",
        default=os.environ.get("OPENFOCUS_INSTANCE_ID") or "default",
        help="OpenFocus instance id. Agent events are accepted only by the matching instance.",
    )
    parser.add_argument(
        "--hook-sock",
        default=os.environ.get("OPENFOCUS_HOOK_SOCK") or "",
        help="Target Companion hook socket for this OpenFocus instance.",
    )
    parser.add_argument(
        "--hook-spool-dir",
        default=os.environ.get("OPENFOCUS_HOOK_SPOOL_DIR") or "",
        help="Fallback directory for hook envelope files when socket delivery is blocked.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    instance_id = _safe_instance_id(args.instance_id)
    hook_sock = (
        Path(str(args.hook_sock)).expanduser()
        if str(args.hook_sock or "").strip()
        else _default_hook_sock(instance_id)
    )
    hook_spool_dir = (
        Path(str(args.hook_spool_dir)).expanduser()
        if str(args.hook_spool_dir or "").strip()
        else _default_hook_spool_dir(instance_id)
    )
    coco_changed = install_coco(
        Path(args.coco_config).expanduser(),
        instance_id=instance_id,
        hook_sock=hook_sock,
        hook_spool_dir=hook_spool_dir,
        dry_run=args.dry_run,
    )
    codex_changed = install_codex(
        Path(args.codex_hooks).expanduser(),
        instance_id=instance_id,
        hook_sock=hook_sock,
        hook_spool_dir=hook_spool_dir,
        dry_run=args.dry_run,
    )
    print(f"coco changed: {coco_changed}")
    print(f"codex changed: {codex_changed}")
    print(f"instance id: {instance_id}")
    print(f"hook socket: {hook_sock}")
    print(f"hook spool dir: {hook_spool_dir}")
    if codex_changed:
        print(
            "Codex will ask you to trust the new OpenFocus hook on next matching hook run; "
            "if the TUI only shows a startup notice, open /hooks and approve the OpenFocus entries."
        )
    if args.dry_run:
        print("dry run: no files were modified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
