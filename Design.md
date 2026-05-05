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
4. Run 完成：进入待 Review；用户 review/确认后再手动标记 Task 完成（必要时生成返工/澄清任务）。
5. History：看到今天勾选了多少、总 token、real vs agent_sum、节省率。

### 4.2 页面清单（MVP）

- `Dashboard`：下一步推荐列表、待 Review 列表、正在运行的 Runs、今日关键指标。
- `Goals`：目标树、优先级、成功标准、关联任务。
- `Tasks`：任务列表（可按 Goal/状态/优先级过滤），任务详情含 Steps、依赖、上下文材料。
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

- **显式进入 Plan Mode** 时触发拆解：用户在 Dashboard 的 `New Goal` 对话框开启 `Plan Mode=ON` 并点击 `Save`，进入 Plan 会话流程。
- Plan Mode 的产出先以“草案”形式保存（人类在环）：在用户点击 `Accept` 之前，不写入任何 `tasks`。
- （可选后续）用户在 Task 上点击“继续拆解”，让 LLM 生成更细的步骤/依赖。

### 8.2 产出要求（结构化）

- LLM 输出必须为结构化 JSON：
  - 任务标题、描述、优先级建议、估时、依赖（可为空）、验收标准（Definition of Done）
- **Plan Mode 阶段仅保存草案**：写入 `goal_plan_sessions.result_json` 与 `goal_plan_messages`（便于追溯与二次编辑）。
- 用户点击 `Accept` 后，才会把选中的任务写入 `tasks` 并创建 Goal。

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
- `POST /api/skills/focus_report`：skill 上报任务执行情况（落库为事件；不自动标记 task 完成）

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
   - `POST /api/skills/focus_report`：用于最终结果上报（落库为事件；不自动勾选任务完成）

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
- 用途：汇总最终状态与产出，作为事件落库与复盘依据。
- 强约束：上报“完成”不等于真实完成，Task 是否完成必须由人确认（详情页按钮）。
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
  - `accept`：记录为 Review 通过，用于推荐/复盘，但**不自动标记 Task 为 done**。
  - `revise`：记录原因，并建议生成“返工任务/补充信息任务”。
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

## 12. Companion 机制

Companion 是运行在本机（或远端工作机）上的常驻桥接进程，用于把浏览器环境无法直接完成的“本机能力”提供给 OpenFocus。

典型例子：系统目录选择器返回**绝对路径**、托管 `coco/codex` 的交互式进程（PTY）、流式读写 stdin/stdout、列出由 OpenFocus 托管的 agent 会话等。

### 12.1 目标

- 让 OpenFocus 保持 Web 形态（local-first control plane），同时具备“像桌面应用一样”的本机能力。
- 支持多机：每台机器运行一个 Companion，统一接入到用户主机上的 OpenFocus，实现跨机器的 AgentSpace/会话托管。
- 所有动作可追溯：命令、输出、状态变更都可落库为 `events`，进入 Review/History/复盘体系。

### 12.2 组件边界（Control Plane vs Data Plane）

- OpenFocus（Control Plane）：目标/任务/事件、推荐、Review、审计与状态机；对外暴露 Web UI 与 Core API。
- Companion（Data Plane）：执行本机操作与资源托管，负责：
  - 弹系统选择器（目录/文件）并返回绝对路径
  - 托管交互式 agent 进程（`coco`/`codex`），提供 PTY 与流式 I/O
  - 文件系统只读浏览/预览（可选；也可由 OpenFocus 直读 workspace，取决于部署）
  - 管理由 OpenFocus 启动的会话列表（`list_managed_agents`），支持 new session/释放

> 原则：Companion 默认只实现“白名单能力”，不做任意 shell 执行，避免演变成通用远控。

### 12.3 通讯模型

#### 12.3.1 总体原则（统一采用长连接，避免反向可达性依赖）

- Companion **不提供 HTTP Web 服务**（不对外监听端口），仅作为客户端。
- Companion 以出站方式连接 OpenFocus（更易穿透 NAT/防火墙），由 OpenFocus 通过该长连接“下发命令”。
- 通讯协议统一采用 **gRPC 双向流（bidirectional streaming）**，用 **ping/pong** 机制确认心跳与在线状态。

#### 12.3.2 gRPC 通道职责（Control Plane ⇄ Data Plane）

- 连接建立：Companion 连接 OpenFocus 的 gRPC server，并发送 `hello/register`（device_id/name/capabilities/可选已配对 token）。
  - **身份标识**：OpenFocus 会为每个 Companion 分配一个稳定的 `companion_id`（服务端生成）。
    - Companion 首次连接时不携带 `companion_id`（或为空/0），OpenFocus 在 `welcome` 中回传分配结果。
    - Companion 必须把 `companion_id` 持久化到本地 state；后续重启/重连时必须携带该 `companion_id`，以便 OpenFocus 复用同一条设备记录。
    - 目的：避免“同一台节点重启后被识别为新的 Companion”，导致 UI/AgentSpace 绑定漂移。
- 心跳确认：
  - OpenFocus 定期发送 `Ping(ts)`；Companion 必须尽快回 `Pong(ping_ts)`。
  - OpenFocus 仅依据“长连接是否存活 + 最近一次 pong/消息时间”判定 `active/offline`。
- 命令下发（OpenFocus -> Companion）：
  - 例如：`choose_directory`、`spawn_agent(coco)`、`list_sessions`、`send_stdin`、`terminate_session` 等。
  - 每个命令必须带 `request_id`，以支持并发、多路复用与幂等。
- 结果/事件回传（Companion -> OpenFocus）：
  - 命令结果：`request_id` 对应的 `response`（ok/error + payload）。
  - 过程事件：stdout/stderr、阶段进度、会话状态变更等（可映射为 `/api/agent/events` 落库）。
- 断线重连：指数退避；重连后重新发送 `hello`；OpenFocus 以 `device_id` 关联同一设备。

#### 12.3.3 Enrollment 配对（认证码）在 gRPC 模型下的落点

- Companion 本地生成 10 位字母/数字认证码：
  - 每次用户点击认证后生成一个，有效期10分钟
  - 用户尝试输入后立即轮换一次
  - 每分钟最多尝试10次
- 用户在 OpenFocus UI 输入认证码后：
  - OpenFocus 通过 gRPC 向对应 Companion 下发 `pair_confirm(code)`
  - Companion 校验成功后返回 `auth_token`
  - OpenFocus 保存 `auth_token` 并将 Companion 状态置为 `active`

> 注：浏览器不直连 Companion；浏览器只调用 OpenFocus API。OpenFocus 负责在控制面做鉴权、审计与状态机。

### 12.4 安全模型（必须）

风险：如果 Companion 连接到“假的 OpenFocus”，等价于把本机能力交给攻击者。

最小安全闭环建议：

1) **TLS + 强校验**：Companion 必须校验 OpenFocus 的服务端证书（生产环境推荐 mTLS）。
2) **Enrollment 配对**：首次接入需要人确认（一次性配对码/短期 token），配对后下发长期设备凭证（客户端证书或长 token）。
3) **命令白名单 + 沙箱**：
   - `spawn_agent` 仅允许 `coco/codex` 等受控命令
   - `cwd` 必须在允许的 workspace 根目录内（防止任意目录执行）
   - 文件访问默认只读，限制文件大小与类型
4) **审计与追溯**：所有命令/结果/输出都写入 OpenFocus `events`，并在 Companion 本地保留审计日志。
5) **高危动作二次确认（可选）**：写文件/执行外部命令/打开敏感目录等需要本机弹窗确认或策略开关。

### 12.5 远程终端（Remote Terminal）

目标：在 AgentSpace 的 AGENT 栏提供“可交互终端”，让用户直接在远端工作机（Companion 所在机器）的 workspace 中运行命令。

#### 12.5.1 架构分层

- 浏览器：渲染终端（xterm.js），只通过 OpenFocus Web API 通信。
- OpenFocus（Control Plane）：
  - 负责终端 session 的创建/关闭/鉴权
  - 将浏览器的输入/resize 转发给 Companion
  - 将 Companion 的输出通过 WebSocket 复用到浏览器
- Companion（Data Plane）：
  - 为每个 terminal session 启动一个 PTY（shell），并持续读取 PTY 输出
  - 接收输入写入 PTY master fd，并支持窗口大小 resize

#### 12.5.2 协议与数据格式

- Control Plane ⇄ Data Plane：复用 12.3 的 gRPC 双向流，在 protobuf 中增加 Terminal 相关消息：
  - `TerminalStart/Stop/Input/Resize/ListSessions`
  - `TerminalOutput(terminal_id, data, closed, error)`
- 浏览器 ⇄ OpenFocus：
  - HTTP：列出/新建/关闭终端（按 AgentSpace 维度管理）
  - WebSocket：实时转发输入/输出
  - 终端数据为二进制，WebSocket 侧采用 `base64` 放入 JSON 字段（`data_b64`）

#### 12.5.3 终端生命周期与清理策略

- 创建：用户点击 `+`（或页面首次进入且无终端时自动创建）→ OpenFocus `POST /api/agent_spaces/{space_id}/terminals/new` → gRPC `TerminalStart` → 返回 `terminal_id`。
- 交互：浏览器通过 `WS /api/agent_spaces/{space_id}/terminals/{terminal_id}/ws` 发送：
  - `{"type":"input","data_b64":"..."}`
  - `{"type":"resize","cols":..,"rows":..}`
  OpenFocus 将其转为 gRPC `TerminalInput/TerminalResize`。
- 输出：Companion 推送 `TerminalOutput` 到 OpenFocus，OpenFocus 将其广播到订阅该 `terminal_id` 的 WebSocket 客户端。
- 关闭：用户点击 tab 上的 `x` → OpenFocus `POST /api/agent_spaces/{space_id}/terminals/{terminal_id}/close` → gRPC `TerminalStop`。
- 释放：AgentSpace 被释放时，OpenFocus 需尽力对该空间下所有 `terminal_id` 执行 `TerminalStop`，并删除 OpenFocus 侧终端记录；Companion 离线时允许仅清理 OpenFocus 侧。

---

## 13. MVP 里程碑与验收

### 13.1 Milestone 1：核心数据与页面（1-2 周）

- Goal/Task/Step CRUD
- Dashboard 基础版（任务列表 + 状态）
- SQLite 持久化

验收：可创建目标与任务，刷新页面不丢数据。

### 13.2 Milestone 2：LLM 拆解与下一步推荐（2-3 周）

- Plan Mode（先规划再创建）→ LLM 生成任务拆解草案（先写入 plan session 草案，Accept 后写库 Goal/Tasks）
- `GET /api/recommendations/next` 返回下一步推荐（带 why）

验收：Plan Mode 的 Accept 后能创建 Goal/Tasks；Dashboard 能显示“下一步建议”。

### 13.3 Milestone 3：Agent 派发 + Skill 遥测 + Review（3-5 周）

- 注册 Agent、派发任务创建 Run
- Skill SDK（Python）能上报 run 事件、token、artifact
- Review/事件流：能查看上报产出并复盘；Task 是否完成由人确认（不自动标记 done）

验收：外部 Agent 能接入并回传产出；用户可在 Task 详情页手动确认完成/重新打开，并可生成返工任务。

### 13.4 Milestone 4：History & Metrics（5-6 周）

- 当日/历史完成墙
- real vs agent_sum 与 token 趋势图

验收：用户能清晰看到进展与杠杆对比。

---

## 14. 关键决策记录（已确认）

- 产品形态：`Web`，支持本地部署本地使用。
- 外部 Agent：面向 `Codex / Claude Code / OpenClaw / Coco / Trae` 等运行时。
- 任务拆解：由 `LLM` 负责生成任务与步骤。
- token 统计：MVP 仅从模型 API 返回获取（不兼容黑盒）。
- 推荐粒度：直接推荐用户“下一步要做的事情”（可执行 next action）。
