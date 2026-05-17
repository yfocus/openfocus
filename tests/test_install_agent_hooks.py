# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_installer():
    path = Path(__file__).resolve().parents[1] / "scripts" / "install_agent_hooks.py"
    spec = importlib.util.spec_from_file_location("install_agent_hooks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_codex_install_adds_instance_specific_hook_commands(tmp_path: Path) -> None:
    installer = _load_installer()
    hooks_path = tmp_path / "hooks.json"
    hook_sock = tmp_path / "hooks-dev.sock"
    hook_spool_dir = tmp_path / "spool-dev"

    assert (
        installer.install_codex(
            hooks_path,
            instance_id="dev",
            hook_sock=hook_sock,
            hook_spool_dir=hook_spool_dir,
            dry_run=False,
        )
        is True
    )
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    hook = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    command = hook["command"]
    assert "OPENFOCUS_REGISTERED_INSTANCE_ID=dev" in command
    assert f"OPENFOCUS_HOOK_SOCK={hook_sock}" in command
    assert f"OPENFOCUS_HOOK_SPOOL_DIR={hook_spool_dir}" in command
    assert "openfocus-codex-hook.sh" in command
    assert "user-prompt-submit" in command
    assert hook["timeout"] == 5
    assert "timeoutSec" not in hook
    stop_command = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "OPENFOCUS_REGISTERED_INSTANCE_ID=dev" in stop_command
    assert f"OPENFOCUS_HOOK_SOCK={hook_sock}" in stop_command
    assert f"OPENFOCUS_HOOK_SPOOL_DIR={hook_spool_dir}" in stop_command
    assert " stop" in stop_command

    assert (
        installer.install_codex(
            hooks_path,
            instance_id="dev",
            hook_sock=hook_sock,
            hook_spool_dir=hook_spool_dir,
            dry_run=False,
        )
        is False
    )

    second_sock = tmp_path / "hooks-debug.sock"
    assert (
        installer.install_codex(
            hooks_path,
            instance_id="debug",
            hook_sock=second_sock,
            hook_spool_dir=tmp_path / "spool-debug",
            dry_run=False,
        )
        is True
    )
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for entry in data["hooks"]["UserPromptSubmit"]
        for hook in entry["hooks"]
    ]
    assert any(f"OPENFOCUS_HOOK_SOCK={hook_sock}" in c for c in commands)
    assert any(f"OPENFOCUS_HOOK_SOCK={second_sock}" in c for c in commands)


def test_codex_install_prunes_legacy_openfocus_hook_without_socket(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    hooks_path = tmp_path / "hooks.json"
    legacy = f"sh '{installer.CODEX_HOOK}' user-prompt-submit"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {"type": "command", "command": legacy, "timeout": 5}
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert (
        installer.install_codex(
            hooks_path,
            instance_id="dev",
            hook_sock=tmp_path / "hooks-dev.sock",
            hook_spool_dir=tmp_path / "spool-dev",
            dry_run=False,
        )
        is True
    )
    text = hooks_path.read_text(encoding="utf-8")
    assert legacy not in text
    assert "OPENFOCUS_HOOK_SOCK=" in text
    assert "UserPromptSubmit" in text


def test_codex_install_prunes_same_instance_noncanonical_event_key(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    hooks_path = tmp_path / "hooks.json"
    hook_sock = tmp_path / "hooks.sock"
    old_command = installer._codex_command(
        "user-prompt-submit",
        instance_id="default",
        hook_sock=hook_sock,
        hook_spool_dir=tmp_path / "spool",
    )
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "userPromptSubmit": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": old_command,
                                    "timeoutSec": 5,
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert (
        installer.install_codex(
            hooks_path,
            instance_id="default",
            hook_sock=hook_sock,
            hook_spool_dir=tmp_path / "spool",
            dry_run=False,
        )
        is True
    )
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert data["hooks"]["userPromptSubmit"] == []
    hooks = data["hooks"]["UserPromptSubmit"][0]["hooks"]
    assert hooks[0]["command"] == old_command
    assert hooks[0]["timeout"] == 5
    assert "timeoutSec" not in hooks[0]


def test_codex_install_keeps_other_instance_canonical_event_key(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    hooks_path = tmp_path / "hooks.json"
    other_sock = tmp_path / "hooks-dev.sock"
    old_command = installer._codex_command(
        "user-prompt-submit",
        instance_id="dev",
        hook_sock=other_sock,
        hook_spool_dir=tmp_path / "spool-dev",
    )
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": old_command,
                                    "timeout": 5,
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert (
        installer.install_codex(
            hooks_path,
            instance_id="default",
            hook_sock=tmp_path / "hooks.sock",
            hook_spool_dir=tmp_path / "spool",
            dry_run=False,
        )
        is True
    )
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for entry in data["hooks"]["UserPromptSubmit"]
        for hook in entry["hooks"]
    ]
    assert old_command in commands


def test_codex_install_prioritizes_current_instance_before_other_openfocus_hooks(
    tmp_path: Path,
) -> None:
    installer = _load_installer()
    hooks_path = tmp_path / "hooks.json"
    dev_command = installer._codex_command(
        "user-prompt-submit",
        instance_id="dev",
        hook_sock=tmp_path / "hooks-dev.sock",
        hook_spool_dir=tmp_path / "spool-dev",
    )
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "sh '/Applications/VibeBuddy/codex-hook.sh' user-prompt-submit",
                                    "timeout": 5,
                                }
                            ],
                        },
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": dev_command,
                                    "timeout": 5,
                                }
                            ],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert (
        installer.install_codex(
            hooks_path,
            instance_id="default",
            hook_sock=tmp_path / "hooks.sock",
            hook_spool_dir=tmp_path / "spool",
            dry_run=False,
        )
        is True
    )
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    commands = [
        entry["hooks"][0]["command"] for entry in data["hooks"]["UserPromptSubmit"]
    ]
    assert "VibeBuddy" in commands[0]
    assert "OPENFOCUS_REGISTERED_INSTANCE_ID=default" in commands[1]
    assert commands[2] == dev_command


def test_coco_install_allows_multiple_openfocus_instances(tmp_path: Path) -> None:
    installer = _load_installer()
    cfg = tmp_path / "traecli.yaml"

    assert (
        installer.install_coco(
            cfg,
            instance_id="dev",
            hook_sock=tmp_path / "hooks-dev.sock",
            hook_spool_dir=tmp_path / "spool-dev",
            dry_run=False,
        )
        is True
    )
    assert (
        installer.install_coco(
            cfg,
            instance_id="debug",
            hook_sock=tmp_path / "hooks-debug.sock",
            hook_spool_dir=tmp_path / "spool-debug",
            dry_run=False,
        )
        is True
    )
    text = cfg.read_text(encoding="utf-8")
    assert "openfocus:coco-hook BEGIN instance=dev" in text
    assert "openfocus:coco-hook BEGIN instance=debug" in text
    assert "OPENFOCUS_REGISTERED_INSTANCE_ID=dev" in text
    assert "OPENFOCUS_REGISTERED_INSTANCE_ID=debug" in text
    assert "hooks-dev.sock" in text
    assert "hooks-debug.sock" in text
    assert "spool-dev" in text
    assert "spool-debug" in text

    assert (
        installer.install_coco(
            cfg,
            instance_id="dev",
            hook_sock=tmp_path / "hooks-dev.sock",
            hook_spool_dir=tmp_path / "spool-dev",
            dry_run=False,
        )
        is False
    )


def test_coco_install_removes_legacy_block_without_instance(tmp_path: Path) -> None:
    installer = _load_installer()
    cfg = tmp_path / "traecli.yaml"
    cfg.write_text(
        """hooks:
  # openfocus:coco-hook BEGIN
  - type: command
    command: 'sh "/old/openfocus-coco-hook.sh"'
    timeout: '10s'
    matchers:
      - event: user_prompt_submit
  # openfocus:coco-hook END
""",
        encoding="utf-8",
    )

    assert (
        installer.install_coco(
            cfg,
            instance_id="dev",
            hook_sock=tmp_path / "hooks-dev.sock",
            hook_spool_dir=tmp_path / "spool-dev",
            dry_run=False,
        )
        is True
    )
    text = cfg.read_text(encoding="utf-8")
    assert "/old/openfocus-coco-hook.sh" not in text
    assert "openfocus:coco-hook BEGIN instance=dev" in text


def test_installer_main_loads_instance_id_from_env_file(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    installer = _load_installer()
    env_path = tmp_path / ".env"
    env_path.write_text("OPENFOCUS_INSTANCE_ID=dev\n", encoding="utf-8")
    coco_config = tmp_path / "traecli.yaml"
    codex_hooks = tmp_path / "hooks.json"

    monkeypatch.delenv("OPENFOCUS_INSTANCE_ID", raising=False)
    monkeypatch.delenv("OPENFOCUS_HOOK_SOCK", raising=False)
    monkeypatch.delenv("OPENFOCUS_HOOK_SPOOL_DIR", raising=False)
    monkeypatch.setenv("OPENFOCUS_ENV_FILE", str(env_path))
    monkeypatch.setattr(installer.env_config, "_DOTENV_LOADED", False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "install_agent_hooks.py",
            "--coco-config",
            str(coco_config),
            "--codex-hooks",
            str(codex_hooks),
        ],
    )

    assert installer.main() == 0
    out = capsys.readouterr().out
    assert "instance id: dev" in out
    assert "hooks-dev.sock" in out
    assert "spool dir: /tmp/openfocus-agent-hooks-" in out

    codex_data = json.loads(codex_hooks.read_text(encoding="utf-8"))
    command = codex_data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "OPENFOCUS_REGISTERED_INSTANCE_ID=dev" in command
    assert ".openfocus/hooks-dev.sock" in command

    coco_text = coco_config.read_text(encoding="utf-8")
    assert "openfocus:coco-hook BEGIN instance=dev" in coco_text
    assert "OPENFOCUS_REGISTERED_INSTANCE_ID=dev" in coco_text
