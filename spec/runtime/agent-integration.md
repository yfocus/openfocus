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

---

## Event Spec：统一事件、Runtime Activity 与 Audit Memory

OpenFocus 将外部 Agent 报告和本机 runtime 状态分开处理：

- **events 表**：面向产品展示、Task history 与审计的结构化 journal。Recent Events 直接读取这里。
- **agent_turns / task_agent_activity**：面向悬浮球和当前运行态的 runtime read model，只由 OpenFocus/Companion 可信 runtime signal 驱动。
- **audit memory**：面向回忆、总结与长期记忆的细粒度审计日志。它记录更多细节，例如终端 I/O、Agent Session chunk、Inspiration 回合等。

`/api/agent/events` 与 `focus_report` 仍然用于 Agent/Skill 报告，但它们默认不再改变 session/task/agent 当前状态。设计原则：

1. `kind` 使用同一套命名体系：`domain.object.action` 或 `domain.action`，例如 `task.progress`、`agent.completed`、`inspiration.draft_generated`。
2. events 表保存展示所需的稳定子集；audit memory 保存更完整、更频繁的明细。
3. 写入 events 的 Agent/Skill 事件必须同步沉淀到 audit memory；只进入 audit memory 的高频/内部事件不要求同步进入 events。
4. Task 是否真正完成始终由用户确认；任何 `completed` / `succeeded` 上报都只表示“Agent 报告完成”。
5. 悬浮球 R/W 只读取 `task_agent_activity`，不能从 HTTP 上报事件推断当前状态。

### events 表格式

所有 `/api/agent/events` 请求必须使用以下结构：

```json
{
  "kind": "task.progress",
  "agent": "trae",
  "task_id": "task_public_id 或 null",
  "payload": {
    "status": "running",
    "message": "正在运行测试",
    "progress": 0.6
  }
}
```

字段约定：

- `kind`：必填，事件类型，最大 128 字符。
- `agent`：必填，上报方标识，例如 `coco`、`trae`、`claude-code`、`attention_scheduler`。
- `task_id`：可选，但**任务相关事件必须填写 `Task.public_id`**。Attention Inbox 只能为能关联到 Task 的事件生成提醒。
- `payload`：必填对象；允许扩展字段，但推荐遵守下面的通用字段。

通用 `payload` 字段：

- `status`：`running | succeeded | failed | blocked | waiting | canceled` 等状态。
- `message`：给用户看的短文本，适合 Recent Events 与 Attention 摘要展示。
- `summary`：比 `message` 更完整的执行摘要。
- `error`：失败详情或异常信息。
- `reason`：阻塞/失败/推荐的原因。
- `progress`：`0..1` 数字，表示粗粒度进度。
- `step` / `total_steps`：可选步骤进度。
- `task_public_id`：当 `task_id` 无法填写但 payload 可携带关联时使用；仍优先填写顶层 `task_id`。
- `metadata`：扩展元信息，不能包含密钥、token、完整凭证。

### Agent/Skill 推荐上报的 journal 事件

外部 Agent 在 AgentSpace 中工作时，推荐遵守以下事件子集。这些事件进入 `events` 与 audit memory，但不直接驱动悬浮球当前状态：

| kind | 触发时机 | 必要 payload | 状态影响 |
| --- | --- | --- | --- |
| `task.started` | Agent 自报开始处理当前 Task | `status="running"`, `message` | journal only |
| `task.progress` | 有可读进度、阶段变化或重要中间结果 | `status="running"`, `message`; 可选 `progress`, `step`, `total_steps` | journal only |
| `task.completed` | Agent 认为任务已完成，等待用户确认 | `status="succeeded"`, `summary` | journal only |
| `task.failed` | Agent 无法完成任务 | `status="failed"`, `error` 或 `summary` | journal only |
| `task.blocked` | Agent 需要用户/外部条件继续 | `status="blocked"` 或 `waiting`, `reason` | journal only |
| `agent.completed` | 一次 Agent run 结束，尤其是 Next Move/推荐类 Agent 完成 | `status`, `result` 或 `summary` | journal；Next Move 仍可生成推荐卡 |
| `skill.focus_report` | 使用 focus_report skill 上报最终结果 | 见下节 | journal only |

推荐上报节奏：

1. 开始时上报 `task.started`。
2. 长任务每个有意义阶段上报 `task.progress`；避免按 token、每行日志或无意义心跳上报。
3. 结束时只选择一种最终上报方式：优先使用 `POST /api/skills/focus_report`；若不可用，再用 `task.completed` / `task.failed` / `task.blocked`。

### focus_report 格式

`POST /api/skills/focus_report` 是任务最终结果的推荐接口，会落库为 `kind="skill.focus_report"` 事件：

```json
{
  "agent": "trae",
  "task_name": "实现事件规范",
  "status": "succeeded",
  "goal_id": null,
  "task_public_id": "task_public_id",
  "user_prompt": "用户原始需求摘要",
  "assistant_response": "完成了什么、验证结果、后续建议",
  "metadata": {
    "changed_files": ["spec/runtime/agent-integration.md"],
    "tests": ["poetry run pytest tests/test_api_events_and_skills.py"]
  }
}
```

`status` 约定：

- 成功：`succeeded | success | ok | done | completed`
- 失败：`failed | fail | error | timeout | denied | panic`
- 阻塞：`blocked | waiting | waiting_on_someone`
- 进行中：`running | in_progress | progress`

### 内部 audit memory 事件

内部模块可以只写 audit memory，用于更细粒度地记录历史。常见 audit-only kind：

- `agent.session.created` / `agent.session.user_message` / `agent.session.chunk` / `agent.session.terminated`
- `terminal.created` / `terminal.input` / `terminal.output` / `terminal.closed`
- `inspiration.message` / `inspiration.title_suggestions` / `inspiration.draft_generated` / `inspiration.phase_summary`
- `inspiration.resource_added` / `inspiration.resources_synced` / `inspiration.published` / `inspiration.publish_error`
- `goal.edited` / `goal.deleted` / `task.edited` / `task.deleted`

当 audit-only 事件需要展示给用户、参与 Recent Events 或触发 Attention 时，应提升为 events 表事件，并保持相同 `kind` 命名语义。

### Runtime Activity 派生规则

悬浮球不展示所有 events，只展示 runtime activity read model，并显示“进入当前状态已有多久”：

1. Running：`runtime.turn.submitted` / `runtime.turn.started` / `runtime.turn.resumed` → `task_agent_activity.state = running`。
2. Waiting：`runtime.turn.waiting_for_approval` / `runtime.turn.waiting_for_input` / `runtime.turn.waiting_for_confirmation` → `state = waiting`。
3. Review ready：`runtime.turn.completed` → `state = review_ready`，等待用户在 Task 中确认完成或继续。
4. Failed/stale/canceled：`runtime.turn.failed` / `runtime.turn.stale` / `runtime.turn.canceled` 进入 W 分区。
5. Next Move：`agent.completed` 且 `payload.result.items[].target.task_public_id` 存在时仍可生成推荐卡，但不计入 R/W 徽标。

悬浮球徽标展示两个数字：`R` 表示 running turn 数量，`W` 表示 waiting / review-ready / failed / stale / canceled 数量。状态持续时间以进入当前 activity state 的时间为准；同一状态内的新进度只更新摘要和最新 signal，不重置持续时间。

### OpenFocus runtime events

Runtime events 是 OpenFocus 内部规范事件，通常来自 Companion gRPC，而不是外部 HTTP agent report：

- `runtime.session.started`
- `runtime.session.resumed`
- `runtime.session.ended`
- `runtime.session.offline`
- `runtime.turn.submitted`
- `runtime.turn.started`
- `runtime.turn.activity`
- `runtime.turn.waiting_for_approval`
- `runtime.turn.waiting_for_input`
- `runtime.turn.waiting_for_confirmation`
- `runtime.interaction.resolved`
- `runtime.turn.resumed`
- `runtime.turn.completed`
- `runtime.turn.failed`
- `runtime.turn.canceled`
- `runtime.turn.stale`
- `runtime.subagent.started`
- `runtime.subagent.completed`
- `runtime.context.compacted`

### ContextPack（最小字段）

- 当前实现不单独落地 `ContextPack` 实体。
- 发送 Agent Session 消息时，服务端会注入最小 OpenFocus prompt：
  - `taskId=<Task.public_id>`
  - `agentSessionId=<session_id>`
  - `openfocusBaseUrl=<base_url>`
  - 事件上报要求：按本文件 Event Spec 调用 `POST /api/agent/events`，并在 prompt 中直接写明接口结构、字段、推荐 kind、status 合法值与 payload 常用字段
  - 上报时机要求：Agent run 启动后必须立刻先上报 `agent.started`；开始处理 task 后必须立刻上报 `task.started`；完成时必须上报 `agent.completed`，并继续上报该 task 的最终结果（优先 `POST /api/skills/focus_report`，否则 `task.completed` / `task.failed` / `task.blocked`）
  - 最终结果上报要求：优先调用 `POST /api/skills/focus_report`

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
