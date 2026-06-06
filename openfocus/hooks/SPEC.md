<!-- SPDX-License-Identifier: Apache-2.0 -->
# Runtime Hook Shims

`openfocus/hooks/` contains local agent hook shims for forwarding runtime signals
to the Companion hook receiver. Shims are best-effort: they must not block or
fail the agent runtime when OpenFocus is unavailable.

## Responsibilities

- Read the agent hook JSON payload from stdin.
- Add local runtime context: cwd, tty, ppid, OpenFocus instance id, hook socket,
  spool directory, task id, session id, and terminal id.
- Emit an envelope with `schema_version=1`, `agent_runtime`, `hook_kind`,
  `runtime_ts`, `runtime`, and `payload`.
- Send the envelope to `OPENFOCUS_HOOK_SOCK`; if socket delivery fails, atomically
  write a `.json` envelope to `OPENFOCUS_HOOK_SPOOL_DIR`.
- Exit 0 regardless of socket or spool failures, after best-effort logging to
  `/tmp/openfocus-agent-hooks.log`.

## Instance Routing

- `OPENFOCUS_REGISTERED_INSTANCE_ID` identifies the OpenFocus instance that
  registered the hook.
- If the running agent has `OPENFOCUS_INSTANCE_ID`, the shim forwards only when
  it matches the registered instance.
- If the running agent lacks `OPENFOCUS_INSTANCE_ID`, only hooks registered for
  the `default` instance forward events.

## Supported Runtimes

- Codex: `openfocus-codex-hook.sh`, `agent_runtime="codex"`.
- Coco/Trae: `openfocus-coco-hook.sh`, `agent_runtime="coco"`.
- Claude Code: `openfocus-claude-hook.sh`, `agent_runtime="claude-code"`.
