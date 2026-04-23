# OpenFocus 项目设计文档

版本：`v0.1`  
日期：`2026-04-20`  
项目：`openfocus`（本地可部署 Web 应用，帮助用户在 AI 时代管理注意力、操纵多 Agent 高效产出）

---

## 1. 背景与目标

AI 时代的个人工作模式正在从“亲自做完”切换为“提出目标 + 组织多个 Agent 执行 + 作为 Reviewer 做判断”。在该模式下，瓶颈不再是单次执行能力，而是**注意力带宽与上下文切换成本**：

- 用户同时推进多个目标与任务，容易陷入“下一步做什么”的决策疲劳。
- 多个 Agent 并行产出后，用户需要快速定位应优先 review 哪些结果、如何推动目标链继续向前。
- 用户需要看到清晰的进展与历史成就（大量 TODO 被勾选），并量化“杠杆效应”：真实耗时 vs Agent 消耗（类似 `time` 命令的 `real` vs `cpu`/`user`）。

### 1.1 核心目标（必须满足）

1. **目标设置**：用户可创建/维护目标（长期/中期/短期），并与任务关联。
2. **下一步建议**：系统基于当前上下文，推荐用户下一步应该推进的“事情”（可执行的下一步行动）。
3. **多 Agent 派发与接入**：系统可代用户把任务提交给具体 Agent；也支持用户自带 Agent（如 Codex、Claude Code、OpenClaw、Coco、Trae 等）通过统一的 Skill/协议接入。
4. **基于运行数据的规划更新**：持续收集 Agent 运行情况（状态、耗时、token、产出质量），让系统不断优化建议与规划。
5. **历史展示与耗时/Token 对比**：提供让用户“爽”的历史视图（完成清单、里程碑），并展示“自己做可能 300h，但 Agent 群总消耗 10h”的对比。

### 1.2 非目标（MVP 不做或延后）

- 不做全自动闭环的“无监督决策代理”（系统不能在无用户确认情况下擅自改目标方向）。
- 不追求复杂 RL/端到端学习式调度，先做**可解释的推荐**与可配置规则。
- 不要求兼容黑盒 Agent 的 token 统计（MVP 仅从模型 API 返回获取 token/用量）。

---

## 2. 产品形态与部署

### 2.1 形态

- **Web 应用**：在浏览器中使用（Dashboard / Goals / Tasks / Review / History / Metrics）。
- **本地部署本地使用**（Local-first）：默认仅监听 `127.0.0.1`，数据保存在本地。

### 2.2 推荐技术栈（Python）

- 后端：`FastAPI` + `Pydantic` + `SQLAlchemy`（或 `SQLModel`）
- 存储：`SQLite`（MVP）→ 可扩展 `PostgreSQL`
- 异步任务：`RQ`/`Celery`（可选，MVP 可先用后台线程/async queue）
- 实时推送：`WebSocket`（FastAPI 原生支持）
- 前端：可先用轻量方案（`HTMX`/`Jinja2`）快速出功能；或 React/Vue（后续）

---

## 3. 核心概念模型

### 3.1 概念定义

- `Goal`：用户想达成的结果（可分层：Objective/Key Result/Initiative）。
- `Task`：为达成目标而执行的工作单元，可由人或 Agent 执行。
- `Step`：任务的下一步最小动作（用于推荐引擎输出“下一步做什么”）。
- `Agent`：执行者（内置或外部，如 Codex/Claude Code/OpenClaw/Coco/Trae）。
- `Skill`：安装/集成在 Agent 侧的插件能力包，使其能接收任务并上报遥测。
- `Run`：一次任务执行实例（输入上下文包 + 产出 + 耗时 + token + 状态）。
- `Artifact`：Run 的输出物（文本/链接/文件摘要/patch 等）。
- `Review`：用户对 Run 产出的审核与裁决（accept/revise/reject + 评分/备注）。
- `ContextPack`：系统为“推荐/派发”构造的上下文包（目标摘要、相关历史、约束、输入材料）。

### 3.2 状态机（建议）

- `Goal.status`：`active | paused | done | archived`
- `Task.status`：`todo | in_progress | blocked | done | canceled`
- `Run.status`：`queued | running | succeeded | failed | canceled`
- `Review.verdict`：`accept | revise | reject`

---

## 4. 用户旅程与关键页面

### 4.1 典型流程（每日）

1. 打开 Dashboard：看到“今日推荐下一步（Top N）”与“待 Review 收件箱”。
2. 选择一个推荐项：
   - 若是“需要你 review 的产出”，进入 Review 页面，给 verdict。
   - 若是“该派发的任务”，点击派发到指定 Agent。
3. Agent 运行中：Dashboard 实时显示运行状态、耗时、token。
4. Run 完成：自动进入待 Review；用户 review 后自动勾选 TODO，必要时生成返工/澄清任务。
5. History：看到今天勾选了多少、总 token、real vs agent_sum、节省率。

### 4.2 页面清单（MVP）

- `Dashboard`：下一步推荐列表、待 Review 列表、正在运行的 Runs、今日关键指标。
- `Goals`：目标树、优先级、成功标准、关联任务。
- `Tasks`：任务列表（可按 Goal/状态/优先级过滤），任务详情含 Steps、依赖、上下文材料。
  - Task 详情需包含「推荐提示词（Recommended Prompt）」：用户可直接复制粘贴到对应 Agent 执行。
    - 提示词必须内嵌 `taskId=Task.public_id`，并要求 Agent 在执行过程中定期向 OpenFocus 上报进度与 token 用量。
- `Review Inbox`：按风险/重要度排序的待 review 产出；一键生成“返工任务”。
- `History & Metrics`：完成清单、里程碑、real vs agent_sum、token 消耗趋势。

---

## 5. 系统架构设计

### 5.1 逻辑架构

- `Core API Service`（FastAPI）
  - Goal/Task/Step 管理
  - ContextPack 构建
  - Recommendation Engine
  - Dispatcher（派发到 Agent）
  - Agent Gateway（接收 Skill 遥测与产出）
  - Review 与 History 聚合

- `Web UI`
  - 读写 Core API
  - WebSocket 订阅 run 状态变更与指标刷新

### 5.2 组件边界

1. **Planner（规划）**：目标 → LLM 任务拆解 → 任务图（依赖/优先级/估时）。
2. **Attention Scheduler（注意力调度）**：在任意时刻输出“下一步推荐”。
3. **Context Builder（上下文整理）**：为推荐/派发准备 ContextPack，并持续沉淀 Artifact。
4. **Dispatcher（派发器）**：适配不同 Agent 运行时（HTTP / CLI / Webhook）。
5. **Telemetry（遥测）**：统一事件模型 + token/耗时采集。
6. **Review Loop（审核闭环）**：review verdict 反哺任务状态与推荐。

---

## 6. 数据模型（MVP 最小集合）

> 存储建议：MVP 使用 `SQLite`，以关系表为主；Artifact 大文本可存 DB，文件类存本地目录并记录路径。

### 6.1 表/实体（建议字段）

**已实现（当前仓库）**

- `goals`
  - `id, content, description, status, priority, importance, due_date, created_at`

- `tasks`
  - `id, public_id, goal_id, title, status, created_at, completed_at`

- `events`
  - `id, kind, agent, task_id (nullable), payload_json, created_at`
  - 说明：`kind` 是事件类型（例如 `agent.llm_call.completed`、`skill.focus_report`）；`payload` 保存原始结构化内容。

- `goal_plan_sessions`（Plan 模式会话）
  - `id, status, draft_content, due_date, turns, result_json, created_goal_id, created_at, updated_at`

- `goal_plan_messages`（Plan 模式消息）
  - `id, session_id, role, content, created_at`

---

## 7. 推荐引擎（Attention Scheduler）

### 7.1 输入信号（MVP 可获得）

- 目标/任务：`priority`、`due_at`、`blocked`、最近推进时间、剩余 steps
- Runs：运行中/待 review 数量、失败率、产出重要度（由任务优先级继承）
- 用户时间块：用户选择“现在可用时长”（如 15/30/60/120 分钟）

### 7.2 输出形式

- `Recommendation[]`（Top N）
  - `type`：`review_run | dispatch_task | do_step | clarify_goal`
  - `target_id`：run/task/step/goal
  - `title`：一句话 next step
  - `why`：可解释理由（3 条以内）
  - `expected_time_minutes`
  - `expected_leverage`：预计节省/杠杆（可先为空，后续学习）

### 7.3 策略（MVP：可解释规则 + 打分排序）

建议采用线性打分：

- `Urgency`：临近截止期加权
- `Importance`：目标/任务优先级
- `Unblock Value`：能解除阻塞的任务优先
- `Review Risk`：失败/高不确定/产出关键点（例如“架构方案”）优先 review
- `Context Freshness`：最近在做的事情加分（降低切换成本）
- `Time Fit`：与用户可用时间块匹配加分

并强制加入“Review 优先”通道：当待 review 队列积压或存在高风险 run 时，推荐优先引导用户 review。

---

## 8. LLM 任务拆解（Planner）

### 8.1 触发点

- 新建 `Goal` 时自动触发拆解，生成初版 `Task` 列表与 Steps。
- 用户在 Task 上点击“继续拆解”，让 LLM 生成更细 Steps 或依赖。

### 8.2 产出要求（结构化）

- LLM 输出必须为结构化 JSON：
  - 任务标题、描述、优先级建议、估时、依赖（可为空）、验收标准（Definition of Done）
- 系统将其写入 `tasks/task_steps`，并记录对应 `artifact`（便于追溯）。

---

## 9. 多 Agent 接入：Skill 与协议

目标：让 Codex / Claude Code / OpenClaw / Coco / Trae 等“不同运行时”的 Agent，能以统一方式：

- 接收任务（含 ContextPack）
- 上报运行状态
- 上报 token/耗时
- 回传产出（Artifact）

### 9.1 接入方式（两条腿走路）

1. **HTTP Skill（推荐）**：Agent 在其运行环境中调用 SDK，上报到本地 Core。
2. **CLI Wrapper Skill（兼容终端型 Agent）**：提供一个薄封装命令，将 stdin/stdout/日志转换为事件与 artifact，再回传 Core。

### 9.2 Core 侧网关 API（建议）

- `POST /api/agent/events`：通用事件上报（每次请求持久化为 `events` 记录）
- `POST /api/skills/focus_report`：skill 上报任务执行情况（落库 + 自动勾选 task 完成）

### 9.3 ContextPack（最小字段）

- `goal_summary`：目标摘要（标题/成功标准/优先级）
- `task`：当前任务（标题/DoD/约束/依赖）
- `history`：相关 artifacts 摘要（最近 N 条）
- `instructions`：本次 run 的明确要求（输出格式、边界、检查点）
- `review_points`：需要用户 review 的关注点（让 Agent 明确产出要点）

### 9.4 鉴权与可靠性

- 本地部署默认信任 `127.0.0.1`；若允许局域网访问，需要 `agent_key + HMAC`。
- 事件上报需幂等：`event_id` 去重；run 状态机单向流转。
- 网络抖动重试：Skill SDK 应支持指数退避。

### 9.5 Task 推荐提示词（Recommended Prompt）

目标：为每个 `Task` 按需生成一段“可直接贴给 Agent 的提示词”，并把遥测要求前置到任务执行指令中。

#### 9.5.1 生成时机（必须满足）

- 推荐提示词是“按需生成”的能力：仅在用户在页面点击“生成提示词”，或客户端主动发起请求时才生成并返回。
- 不在 Task 创建时自动生成；不在页面首次渲染时自动生成。

#### 9.5.2 持久化策略（必须满足）

- 推荐提示词 **不得写入数据库**（避免与任务内容/遥测规范耦合导致存量数据难以迁移）。
- 服务端 **不做缓存**：每次请求都实时生成并返回。

#### 9.5.3 生成方式（必须满足）

- 生成推荐提示词时必须调用 OpenFocus 的 `agent loop`（即复用 `run_tool_loop(...)` 的 LLM 交互/工具调用框架）。

#### 9.5.4 提示词内容规范（必须满足）

提示词必须包含：

1) **任务本体**：任务标题（`Task.title`）+ 关联 goal 信息：
   - 必须包含 `Goal.content`
   - 若 `Goal.description` 非空则必须包含
   - 若 `Goal.due_date` 存在则必须包含
2) **稳定 taskId**：明确写死 `taskId=<Task.public_id>`，并要求所有上报携带该字段，便于 OpenFocus 识别归因。
3) **进度上报要求**：Agent 必须在任务执行过程中定期上报进度：
   - 必须在开始时上报一次（`task.started`）
   - 每完成一个里程碑必须上报一次（`task.progress`）
   - 若连续 15 分钟没有里程碑产出，也必须上报一次（`task.progress`）
   - 结束时必须上报一次（`task.completed` 或 `task.failed`）
4) **token 用量上报要求**：要求 Agent 在上报中携带 token 用量。
   - 若 Agent 能精确统计，则上报累积值：`prompt_tokens / completion_tokens / total_tokens`。
   - 若无法获取精确 token，则必须上报 `total_tokens=null`，并在 `message` 中说明原因（例如“运行环境不提供 token 统计”）。
5) **上报接口与示例**：提示词必须包含两段可执行的上报示例：
   - `POST /api/agent/events`：用于开始/进度/结束的过程上报
   - `POST /api/skills/focus_report`：用于最终结果上报（用于自动勾选任务完成）

#### 9.5.5 /api/agent/events 约定（过程上报）

- Endpoint：`POST /api/agent/events`
- Request：
  - `kind`：必须为 `task.started` / `task.progress` / `task.completed` / `task.failed` 之一
  - `agent`：上报方标识，如 `coco/trae/claude-code/codex/openclaw`
  - `task_id`：必须为 `Task.public_id`
  - `payload`：必须包含：
    - `percent`: 0~100
    - `message`: 简短进度说明
    - `usage`: `{prompt_tokens, completion_tokens, total_tokens}`（允许 `total_tokens=null`）

#### 9.5.6 /api/skills/focus_report 约定（最终结果上报）

- Endpoint：`POST /api/skills/focus_report`
- 用途：汇总最终状态与产出，并触发任务自动勾选。
- 要求：`task_public_id` 必须填写为 `Task.public_id`；token 用量必须放入 `metadata.usage`（字段同上）。

#### 9.5.7 服务端 API（MVP）

- `GET /api/tasks/{task_public_id}/recommended_prompt`
  - 行为：按需生成并返回推荐提示词（不落库）。
  - 返回：`{task_public_id, prompt}`

---

## 10. Review 闭环与自动推进

### 10.1 Review 机制

- 每个完成的 `Run` 进入 `Review Inbox`。
- 用户给出 verdict：
  - `accept`：可自动将对应 Task/Step 标记为完成（规则可配置）。
  - `revise`：自动生成“返工任务/补充信息任务”，并派回同一 Agent 或换 Agent。
  - `reject`：记录原因，建议重新拆解或澄清目标。

### 10.2 反哺推荐与规划

- 记录：哪些推荐被用户采纳/跳过；哪些 run 经常需要 revise。
- 下一版：将这些信号用于调整打分权重与 agent 选择策略。

---

## 11. 历史展示与时间/Token 指标

### 11.1 指标定义

- `Real Time`：从派发到用户完成 review 的自然时间（wall-clock）。
- `Agent Time Sum`：所有 runs 的 `wall_seconds` 求和（类比 cpu time，体现并行杠杆）。
- `Tokens`：`prompt_tokens / completion_tokens / total_tokens` 聚合（按任务/目标/日/agent）。
- `Leverage`：
  - `self_estimate_hours / agent_time_hours`（用户自评 vs Agent 实耗）
  - `self_estimate_hours / real_hours`（真实进度感）

### 11.2 视觉呈现（MVP）

- 每日完成清单（可折叠展示每个 Task 的 artifacts 与 review 结论）。
- 当日指标卡片：`完成数`、`token`、`real`、`agent_sum`、`节省率`。

---

## 12. MVP 里程碑与验收

### 12.1 Milestone 1：核心数据与页面（1-2 周）

- Goal/Task/Step CRUD
- Dashboard 基础版（任务列表 + 状态）
- SQLite 持久化

验收：可创建目标与任务，刷新页面不丢数据。

### 12.2 Milestone 2：LLM 拆解与下一步推荐（2-3 周）

- Goal → LLM 自动拆解 Task/Step（结构化写库）
- `GET /v1/recommendations/next` 返回 Top N（带 why）

验收：新建目标后自动出现可推进的任务池；Dashboard 能显示“下一步建议”。

### 12.3 Milestone 3：Agent 派发 + Skill 遥测 + Review（3-5 周）

- 注册 Agent、派发任务创建 Run
- Skill SDK（Python）能上报 run 事件、token、artifact
- Review Inbox + verdict 驱动任务状态变化

验收：外部 Agent 能接入并回传产出；用户 review 后 TODO 自动勾选或生成返工任务。

### 12.4 Milestone 4：History & Metrics（5-6 周）

- 当日/历史完成墙
- real vs agent_sum 与 token 趋势图

验收：用户能清晰看到进展与杠杆对比。

---

## 13. 关键决策记录（已确认）

- 产品形态：`Web`，支持本地部署本地使用。
- 外部 Agent：面向 `Codex / Claude Code / OpenClaw / Coco / Trae` 等运行时。
- 任务拆解：由 `LLM` 负责生成任务与步骤。
- token 统计：MVP 仅从模型 API 返回获取（不兼容黑盒）。
- 推荐粒度：直接推荐用户“下一步要做的事情”（可执行 next action）。
