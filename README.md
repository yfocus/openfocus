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

**Install dependencies**

```shell
poetry install
npm install
```

**Build frontend assets**

```shell
npm run build
```

**Start OpenFocus**

```shell
make serve
```

Then open:

```text
http://127.0.0.1:8001/goals
```

**Start Companion**

Run this on a machine that should host workspaces, terminals, or command-line agents:

```shell
poetry run python -m openfocus.companion
```

Then pair it from the Companion page in OpenFocus.

## Development

```shell
make fmt
make lint
make test
make check
```

Frontend development:

```shell
npm run dev
npm run build
```

## Directory Structure

| Directory | Description |
|-----------|-------------|
| [openfocus](./openfocus) | FastAPI app, data models, Companion gRPC bridge, templates, and backend logic |
| [frontend](./frontend) | React islands entrypoints for richer interactive surfaces |
| [remote-terminal](./remote-terminal) | Embedded remote terminal frontend assets |
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
