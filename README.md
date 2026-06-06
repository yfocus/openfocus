<!-- SPDX-License-Identifier: Apache-2.0 -->
<div align="center">
<p align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="resources/icons/cover_dark.jpeg">
  <source media="(prefers-color-scheme: light)" srcset="resources/icons/cover_light.jpeg">
  <img alt="OpenFocus" src="resources/icons/cover_light.jpeg">
</picture>

**Agent-native workspace for goals, execution tracking, and focus orchestration.**<br/>
**Manage your goals, track your agents, and know your next move.**<br/>
**Built for super individuals working with multiple AI agents.**

<a href="./spec/README.md">Spec</a> •
<a href="./spec/product-requirements.md">Product Requirements</a> •
<a href="./spec/architecture.md">Architecture</a> •
<a href="./LICENSE">License</a> •
<a href="./README.zh-CN.md">简体中文</a>

![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Status](https://img.shields.io/badge/status-active_development-purple)

</p>
</div>

<hr />

OpenFocus is an agent-native workspace that manages your goals, tracks execution, and helps you stay focused.

OpenFocus is perfect for:

1. Managing multiple goals and tasks while agents execute in parallel.
2. Tracking agent progress and knowing when human review is needed.
3. Capturing ideas, turning them into actionable goals, and choosing the next best move.

## Why OpenFocus?

When working with agents, have you ever run into these problems?

1. A task has been handed to an agent, but you do not know when it will finish or what to do while waiting.
2. Multiple agents are running at the same time, but it is hard to understand their current progress and completion state.
3. You keep switching contexts to give different agents the next instruction, and your attention bar runs out quickly.

The core problem is that AI-era knowledge work is becoming **goal setting + multi-agent execution + human review**. In this mode, the bottleneck is no longer just execution speed. The bottleneck is human attention bandwidth and context switching.

OpenFocus is an agent-native workstation for this workflow. It is not focused on “making agents faster,” but on reducing
task-switching overhead and increasing human throughput.

## Key Features

* **Goal and task focus:** Manage goals and tasks, review outcomes, and let agents do the concrete work. OpenFocus keeps the human in the loop without forcing the human to micromanage every step.

* **Execution tracking:** Agents and skills report progress into OpenFocus. The dashboard gives a global view of goals, tasks, recent events, and task-level execution history.

* **Next Move:** OpenFocus recommends what to do next based on current goals, tasks, agent events, memory, and feedback. Instead of repeatedly re-prioritizing after every prompt, check Next Move and keep momentum.

* **Agent as a first-class citizen:** OpenFocus integrates command-line agents through Companion, remote terminal, and prompt management. The core stays lightweight and flexible at the agent layer.

* **Built-in and bring-your-own agents:** OpenFocus uses built-in agents for focus orchestration and inspiration workflows, while also allowing users to run their own agents in remote terminals.

* **Inspiration Space:** Every idea can start in an Inspiration Space. Discuss with an agent, add resources, refine context, generate drafts, and publish the result as a goal with tasks.

* **Memory evolution:** OpenFocus records key behaviors and events, then turns them into audit, daily, and long-term memory. Memory can feed recommendations and future agent context.

* **Multi-node Companion:** Register local machines, dev boxes, and cloud hosts as Companions. Use the right execution environment for each task without losing the central control plane.

## Typical Workflows

### Multi-task switching

- Open the Dashboard to see goals, tasks, progress, and recent events.
- Check Next Move to decide the next task worth your attention.
- Create an Agent Space for a task and guide an agent through a remote terminal.

### Idea capture

Agents make it cheaper to turn fleeting ideas into real projects. Capture a good idea in Inspiration mode, discuss it later, add context, and publish it when the plan becomes actionable.

### Agents should not stop

During lunch, walking, commuting, or discussions with other people, keep observing agent progress and review results when needed. Let agents continue while your attention moves elsewhere.

## Quick Start

This guide starts a local-first OpenFocus instance on `127.0.0.1`. The default
database is created automatically at `.data/openfocus.db`.

### Prerequisites

Install these tools before running the project:

- Python `3.11` or newer.
- Poetry for Python dependency management.
- Node.js `18` or newer and npm.
- `make`.

Check your local environment:

```shell
python3 --version
poetry --version
node --version
npm --version
make --version
```

### Install

From a fresh checkout:

```shell
git clone <openfocus-repository-url>
cd openfocus
poetry install
npm install
cp .env-default .env
```

> [!TIP]
> You can give this `README.md` to an agent and ask it to install OpenFocus for
> you. The agent should read this file, run the commands in this section from
> the repository root, create `.env` from `.env-default`, build the frontend, and
> start the local service.

The default `.env` is enough to launch the web service. Add an OpenAI-compatible
or Ark-compatible API key only if you want built-in LLM workflows such as
Inspiration follow-up and draft generation:

```dotenv
OPENFOCUS_OPENAI_API_KEY=
OPENFOCUS_OPENAI_BASE_URL=https://api.openai.com/v1
OPENFOCUS_OPENAI_MODEL=gpt-4.1-mini
```

Common local settings:

```dotenv
OPENFOCUS_INSTANCE_ID=dev
OPENFOCUS_HOST=127.0.0.1
OPENFOCUS_PORT=8001
OPENFOCUS_GRPC_HOST=127.0.0.1
OPENFOCUS_GRPC_PORT=17891
OPENFOCUS_SERVER_GRPC_ADDR=127.0.0.1:17891
```

Existing shell environment variables take precedence over `.env`. To use an
isolated database, set `OPENFOCUS_DB_PATH=/absolute/path/to/openfocus.db`; if
unset, OpenFocus uses `.data/openfocus.db`.

### Build Frontend Assets

```shell
npm run build
```

OpenFocus serves frontend assets from `openfocus/static/dist`, so build the
frontend before starting the app and rebuild it after frontend source changes.

### Start OpenFocus

```shell
make serve
```

`make serve` loads configuration from the repository root `.env`, starts the
FastAPI app with reload enabled, initializes the SQLite database, and starts the
Companion gRPC listener unless `OPENFOCUS_GRPC_AUTOSTART=0` is set.

Then open:

```text
http://127.0.0.1:8001/goals
```

Useful health checks:

```shell
curl http://127.0.0.1:8001/goals
ls -lh .data/openfocus.db
```

If you changed `OPENFOCUS_PORT`, replace `8001` with your configured port.

### Start Companion

Open a second terminal in the same repository root and run this on a machine
that should host workspaces, terminals, or command-line agents:

```shell
make companion
```

This target loads configuration from the repository root `.env` before starting
the Companion. Then open the Companion page in OpenFocus and pair the device:

```text
http://127.0.0.1:8001/companions
```

**Install agent runtime hooks**

OpenFocus tracks Coco, Codex, and Claude Code turns through local hooks. The
installer backs up existing Coco/Codex/Claude hook configuration before writing
changes:

```shell
sh scripts/install_agent_hooks.py
```

By default the OpenFocus instance id is `default`. Put the instance id in the
repo-root `.env` file so the OpenFocus service, Companion, and hook installer all
read the same value automatically:

```dotenv
OPENFOCUS_INSTANCE_ID=dev
OPENFOCUS_PORT=8001
OPENFOCUS_GRPC_PORT=17891
OPENFOCUS_SERVER_GRPC_ADDR=127.0.0.1:17891
```

Then start each process without repeating the instance env vars:

```shell
make serve
make companion
sh scripts/install_agent_hooks.py
```

Existing shell environment variables still take precedence over values in
`.env`. You can also point to a specific env file with `OPENFOCUS_ENV_FILE`, and
disable dotenv loading with `OPENFOCUS_DOTENV=0`.

The default instance uses:

- hook socket: `~/.openfocus/hooks.sock`
- hook spool directory: `/tmp/openfocus-agent-hooks-<uid>/default`

For multiple OpenFocus instances, give each instance a stable
`OPENFOCUS_INSTANCE_ID` in that instance's env file. Use a separate checkout/cwd
with its own `.env`, or set `OPENFOCUS_ENV_FILE` to a different env file for the
second instance. Named instances use isolated defaults:

- hook socket: `~/.openfocus/hooks-<OPENFOCUS_INSTANCE_ID>.sock`
- hook spool directory: `/tmp/openfocus-agent-hooks-<uid>/<OPENFOCUS_INSTANCE_ID>`

Example `.env` for a development instance:

```dotenv
OPENFOCUS_INSTANCE_ID=dev
OPENFOCUS_PORT=8001
OPENFOCUS_GRPC_PORT=17891
OPENFOCUS_SERVER_GRPC_ADDR=127.0.0.1:17891
```

Example env file for a second debug instance:

```dotenv
OPENFOCUS_INSTANCE_ID=debug
OPENFOCUS_PORT=8002
OPENFOCUS_GRPC_PORT=17892
OPENFOCUS_SERVER_GRPC_ADDR=127.0.0.1:17892
```

If you override `OPENFOCUS_HOOK_SOCK` or `OPENFOCUS_HOOK_SPOOL_DIR`, use the same
values for Companion and `scripts/install_agent_hooks.py`.

**Codex hook trust**

Codex does not execute newly registered hooks until you explicitly trust them in
the Codex TUI. After installing hooks, start Codex in an OpenFocus Agent Space or
terminal, run:

```text
/hooks
```

Trust the OpenFocus entries for these Codex events:

- `SessionStart`: session correlation.
- `UserPromptSubmit`: turn start/running state.
- `PermissionRequest`: waiting-for-approval state.
- `PreToolUse`: running activity after approval.
- `PostToolUse`: running activity fallback after approval.
- `Stop`: turn completed/review-ready state.

Codex will only send hook events to OpenFocus after these entries are trusted.

**Coco hook registration**

Coco supports multiple hooks, so the installer appends an OpenFocus block to the
Coco config instead of replacing existing hooks. The default config path is
`~/.trae/traecli.yaml`; override it with `--coco-config` if needed.

The OpenFocus Coco block registers these events:

- `pre_tool_use`
- `post_tool_use`
- `post_tool_use_failure`
- `user_prompt_submit`
- `stop`
- `subagent_start`
- `subagent_stop`
- `session_start`
- `session_end`
- `pre_compact`
- `post_compact`
- `notification`
- `permission_request`

**Claude Code hook registration**

Claude Code hooks are installed into `~/.claude/settings.json`; override the
path with `--claude-settings` if needed. OpenFocus appends command hook matcher
groups and preserves existing Claude Code settings.

The OpenFocus Claude Code entries register these events:

- `SessionStart`
- `UserPromptSubmit`
- `PermissionRequest`
- `PreToolUse`
- `PostToolUse`
- `PostToolUseFailure`
- `Stop`
- `Notification`
- `SessionEnd`

Run `/hooks` in Claude Code to inspect the loaded OpenFocus entries.

## Development

```shell
make fmt
make lint
make test
make test-blackbox
make check
```

`make test-blackbox` runs the opt-in black-box workflow suite. It is intended
for core refactors and exercises public HTTP routes end to end.

Frontend development:

```shell
npm run dev
npm run build
```

If you are not using `npm run dev`, rebuild with `npm run build` before `make serve` so `/static/dist` contains up-to-date assets.

## Directory Structure

| Directory | Description |
|-----------|-------------|
| [openfocus](./openfocus) | FastAPI app, data models, Companion gRPC bridge, templates, and backend logic |
| [frontend](./frontend) | React islands entrypoints for richer interactive surfaces |
| [openfocus/static/terminal-panel](./openfocus/static/terminal-panel) | Embedded terminal panel frontend assets |
| [skills](./skills) | OpenFocus skills and reporting helpers |
| [spec](./spec/README.md) | Product and technical specifications |
| [tests](./tests) | Pytest test suite |

## Specifications

OpenFocus specs are maintained under [`spec/`](./spec/README.md):

- [Product Requirements](./spec/product-requirements.md)
- [Architecture](./spec/architecture.md)
- [Next Move](./spec/features/next-move.md)
- [Inspiration](./spec/features/inspiration.md)
- [Memory](./spec/features/memory.md)
- [Agent Integration](./spec/runtime/agent-integration.md)
- [Companion](./spec/runtime/companion.md)
- [Roadmap and Decisions](./spec/roadmap.md)

## License

OpenFocus is licensed under the Apache License 2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).
