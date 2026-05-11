<!-- SPDX-License-Identifier: Apache-2.0 -->
# Agent Integration and Human Confirmation

## 多 Agent 接入：Skill 与协议

目标：让 Codex / Claude Code / OpenClaw / Coco / Trae 等“不同运行时”的 Agent，能以轻量方式接入 OpenFocus：

- 接收任务上下文与 OpenFocus 上报协议提示
- 上报运行状态、进度与最终结果
- 通过事件、终端输出、resource 文件或发布总结沉淀产出；不再引入额外的独立产物对象

### 接入方式（两条腿走路）

1. **HTTP Skill（推荐）**：Agent 在其运行环境中调用 SDK，上报到本地 Core。
2. **Remote Terminal / CLI Wrapper（兼容终端型 Agent）**：通过 Companion 托管终端或薄封装命令，将进度、stdout/stderr、日志与最终结果转换为事件或资源，再回传 Core。

### Core 侧网关 API（建议）

- `POST /api/agent/events`：通用事件上报（每次请求持久化为 `events` 记录）
- `POST /api/skills/focus_report`：skill 上报任务执行情况（落库为事件；不自动标记 task 完成）
- `POST /api/agent_spaces/{space_id}/agent/sessions/{session_id}/send`：向 Agent Session 发送消息，并在服务端注入 OpenFocus prompt 头部

### ContextPack（最小字段）

- 当前实现不单独落地 `ContextPack` 实体。
- 发送 Agent Session 消息时，服务端会注入最小 OpenFocus prompt：
  - `taskId=<Task.public_id>`
  - `agentSessionId=<session_id>`
  - `openfocusBaseUrl=<base_url>`
  - 进度上报要求：`POST /api/agent/events`
  - 最终结果上报要求：`POST /api/skills/focus_report`

### 鉴权与可靠性

- 本地部署默认信任 `127.0.0.1`；若允许局域网访问，需要 `agent_key + HMAC`。
- 当前 `events` API 采用“每次请求直接落库”的简化模型，不要求 `event_id` 幂等键。
- 网络抖动重试：Skill SDK 应支持指数退避。

### Agent Session（当前实现）

- 当前通过 Companion 托管的 `Agent Session` 提供会话能力，而不是独立的推荐提示词生成接口。
- 相关接口：
  - `GET /api/agent_spaces/{space_id}/agent/sessions`
  - `POST /api/agent_spaces/{space_id}/agent/sessions/new`
  - `GET /api/agent_spaces/{space_id}/agent/sessions/{session_id}/messages`
  - `POST /api/agent_spaces/{space_id}/agent/sessions/{session_id}/send`
  - `GET /api/agent_spaces/{space_id}/agent/sessions/{session_id}/sse`
- 当前前端尚未提供完整的 Agent chat tab；后端能力先行落地，供后续页面集成。

---

## Human Confirmation Loop

### 当前闭环

- 所有 Agent/Skill 上报先进入 `events`，展示在 Dashboard `Recent Events` 与 Task 详情 `Event` 区块。
- `task.completed` / `skill.focus_report(status=succeeded)` 只表示“上报完成”，**不自动标记 Task 为 done**。
- Task 是否真正完成，由用户在 Task 详情中点击 `Finish`；已完成任务可点击 `Reopen` 恢复。

### 反哺范围

- Next Move 必须读取 Goal/Task 属性、近期事件、daily/long-term memory，以及历史推荐反馈。
- 更复杂的 Review verdict、返工任务自动生成、质量评分机制留待后续版本；但推荐纠偏学习闭环属于本次方案范围。
