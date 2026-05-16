<!-- SPDX-License-Identifier: Apache-2.0 -->
<div align="center">
<p align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="resources/icons/cover_dark.jpeg">
  <source media="(prefers-color-scheme: light)" srcset="resources/icons/cover_light.jpeg">
  <img alt="OpenFocus" src="resources/icons/cover_light.jpeg">
</picture>

**面向目标管理、执行跟踪与注意力编排的 Agent 原生工作空间。**<br/>
**管理你的目标，跟踪你的 Agent，知道下一步该做什么。**<br/>
**为与多个 AI Agent 协作的超级个体而构建。**

<a href="./README.md">English</a> •
<a href="./spec/README.md">规范文档</a> •
<a href="./spec/product-requirements.md">产品需求</a> •
<a href="./spec/architecture.md">架构设计</a> •
<a href="./LICENSE">许可证</a>

![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Status](https://img.shields.io/badge/status-active_development-purple)

</p>
</div>

<hr />

OpenFocus是Agent原生的工作空间， 管理你的目标、跟踪任务并编排你的注意力.

OpenFocus 适合：

1. 同时管理多个目标与任务，并让多个 Agent 并行执行。
2. 跟踪 Agent 进展，知道什么时候需要人类审查。
3. 捕获灵感，把想法转化为可执行目标，并选择下一步最值得推进的任务。

## 为什么需要 OpenFocus？

在与 Agent 协作时，你是否遇到过这些问题？

1. 任务已经交给 Agent，但不知道还要多久执行完，也不知道等待期间该做什么。
2. 多个 Agent 同时运行时，很难总览它们当前的进展，尤其是不知道什么时候需要自己审查结果。
3. 频繁切换上下文给不同 Agent 下达下一步指令，注意力很快被耗尽。

问题的本质是：AI 时代的个人工作模式正在变成 **提出目标 + 组织多 Agent 执行 + 人类审查**。在这种模式下，效率瓶颈不再只是单次执行速度，
而是人的注意力带宽与上下文切换成本。

OpenFocus 就是为这种工作流准备的 Agent 原生工作站，它的核心关注点不是“让 Agent 更快”，而是降低任务切换成本，提升人的吞吐。

## 核心能力

* **聚焦目标和任务：** 用户只需要管理目标、任务并审查执行结果；具体执行优先交给 Agent。OpenFocus 保持 human in the loop，但不要求人类微管理每一步。

* **进展跟踪：** Agent 和 Skill 会持续向 OpenFocus 上报进展。Dashboard 可以总览目标、任务、最近事件，以及每个任务的执行历史。

* **Next Move：** OpenFocus 会根据当前目标、任务、Agent 运行事件、记忆系统和反馈推荐下一步任务。用户不需要每次敲完 prompt 后重新做优先级排序，看 Next Move 即可继续推进。

* **Agent 一等公民：** OpenFocus 通过 Companion、remote terminal 和 prompt management 集成命令行 Agent。OpenFocus 的核心是目标与注意力编排，在 Agent 层保持轻量和灵活。

* **内置 Agent 与自带 Agent：** OpenFocus 使用内置 Agent 支撑注意力编排和灵感创作，同时也允许用户在 remote terminal 中启动自己的 Agent。

* **灵感空间：** 每一个想法都可以进入 Inspiration Space。你可以与 Agent 讨论、补充资源、打磨上下文、生成草案，并最终发布为目标和任务。

* **记忆进化：** OpenFocus 会记录关键行为与事件，并沉淀为 audit memory、daily memory 和 long-term memory。记忆可用于推荐，也可作为未来 Agent 上下文的一部分。

* **多节点 Companion：** 本地电脑、开发机、云主机都可以注册为 Companion。不同任务可以在不同执行环境中完成，同时保留统一控制面。

## 典型使用场景

### 多任务切换

- 打开 Dashboard，总览目标、任务、进展和最近事件。
- 查看 Next Move，决定下一段注意力应该投入哪个任务。
- 为 Task 创建 Agent Space，通过 remote terminal 指挥 Agent 完成任务。

### 灵感捕获

Agent 极大降低了把想法变成现实的成本。每当有一个好点子时，可以先放进 Inspiration mode，之后继续讨论、补充上下文，并在计划成熟后发布。

### Agent 不能停

吃饭、散步、通勤、与人讨论时，都可以持续观察 Agent 进展，并在需要时审查结果。让 Agent 继续运行，让人的注意力流向更重要的地方。

## Quick Start

**安装依赖**

```shell
poetry install
npm install
```

**构建前端资源**

```shell
npm run build
```

OpenFocus 会直接从 `openfocus/static/dist` 提供前端静态资源，因此启动服务前必须先构建前端；如果修改了前端源码，也需要重新执行构建。

**启动 OpenFocus**

```shell
npm run build
make serve
```

然后打开：

```text
http://127.0.0.1:8001/goals
```

**启动 Companion**

在需要托管 workspace、terminal 或命令行 Agent 的机器上运行：

```shell
poetry run python -m openfocus.companion
```

然后在 OpenFocus 的 Companion 页面完成配对。

## 开发命令

```shell
make fmt
make lint
make test
make check
```

前端开发：

```shell
npm run dev
npm run build
```

如果你不是通过 `npm run dev` 进行前端开发，那么在执行 `make serve` 前请先运行 `npm run build`，确保 `/static/dist` 中的资源是最新的。

## 目录结构

| 目录 | 说明 |
|------|------|
| [openfocus](./openfocus) | FastAPI 应用、数据模型、Companion gRPC bridge、模板和后端逻辑 |
| [frontend](./frontend) | React islands 入口，用于更复杂的交互界面 |
| [openfocus/static/terminal-panel](./openfocus/static/terminal-panel) | 嵌入式终端面板前端资源 |
| [skills](./skills) | OpenFocus skills 与上报辅助工具 |
| [spec](./spec/README.md) | 产品与技术规范 |
| [tests](./tests) | Pytest 测试套件 |

## 规范文档

OpenFocus 的规范文档维护在 [`spec/`](./spec/README.md)：

- [产品需求](./spec/product-requirements.md)
- [架构设计](./spec/architecture.md)
- [Next Move](./spec/features/next-move.md)
- [Inspiration](./spec/features/inspiration.md)
- [Memory](./spec/features/memory.md)
- [Agent Integration](./spec/runtime/agent-integration.md)
- [Companion](./spec/runtime/companion.md)
- [Roadmap and Decisions](./spec/roadmap.md)

## 许可证

OpenFocus 使用 Apache License 2.0。详见 [LICENSE](./LICENSE) 和 [NOTICE](./NOTICE)。
