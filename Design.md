# OpenFocus 项目设计文档

版本：`v0.1`  
日期：`2026-04-20`  

---

## 1. 背景与目标

AI 时代的个人工作模式正在从“亲自做完”切换为“提出目标 + 组织多个 Agent 执行 + 作为 Reviewer 做判断”。在该模式下，瓶颈不再是单次执行能力，而是**注意力带宽与上下文切换成本**：

- 用户同时推进多个目标与任务，容易陷入“下一步做什么”的决策疲劳。
- 多个 Agent 并行产出后，用户需要快速定位应优先 review 哪些结果、如何推动目标链继续向前。
- 当用户并行推进多任务时，需要能方便的“量化产出”，采用 Token消耗 或 Agent时 来量化吞吐。

### 1.1 核心目标（必须满足）

1. **目标设置**：用户可创建/维护目标，并与任务关联。
2. **下一步建议**：系统基于 Goal、Tasks、Events、Memory，推荐用户下一步应该推进的“事情”（可执行的 next action）。
3. **Plan Mode**：支持先规划再创建，Goal/Tasks 只在 `Create` 后写库。
4. **多 Agent 接入**：系统通过统一事件接口与 Companion / AgentSpace 机制接入外部 Agent。
5. **人工确认**：Task 是否完成必须由人确认（Agent会上报进度，但不代表任务完成）。
6. **本机工作区**：通过 Companion 提供目录选择、只读文件浏览、预览、远程终端与 Agent Session。

### 1.2 非目标（MVP 不做或延后）

- 不做全自动闭环的“无监督决策代理”（系统不能在无用户确认情况下擅自改目标方向）。
- 不追求复杂 RL/端到端学习式调度，当前基线采用**可解释的规则推荐**。
- 不要求兼容黑盒 Agent 的 token 统计（MVP 仅从模型 API 返回获取 token/用量；当前页面未提供独立 token 指标页）。
- 不提供独立的 `Review Inbox`、`History & Metrics` 页面；当前以 Dashboard 事件流、Task 详情事件区、Calendar 为主。
- 不提供独立的 `recommended_prompt` 生成 API；当前通过 AgentSpace 的 Agent Session 发送前注入 OpenFocus prompt。

### 1.3 语言与文案规范

- `Design.md`、`UserStory.md` 等系统文档可使用中文。
- 用户可见的按钮、标签、导航、状态文案、提示信息统一使用英文。
- 代码实现、注释、接口字段命名统一使用英文。

---

## 2. 产品形态与部署

### 2.1 形态

- **Web 应用**：在浏览器中使用（Dashboard / Plan Session / AgentSpace / Companion / Memory / Calendar）。
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
- `Agent`：只支持基于terminal的agent。
- `Event`：系统中的统一遥测记录，承接 Web 操作、Agent 进度。
- `Plan Session`：Plan Mode 会话，保存草案、消息与最终结构化结果。
- `AgentSpace`：围绕单个 Task 的工作区，绑定 workdir、Companion、remote terminal、Agent Session。
- `Companion`：提供目录选择、文件读取、终端、Agent Session 的本机桥接进程。
- `Audit Memory`：用户与 Agent 在 OpenFocus 内的原始审计日志，采用 rolling 文件保存。
- `Daily Memory`：按天汇总的记忆文件，承接 Audit Memory 的阶段性总结与日终定稿。
- `Long-term Memory`：从 daily 记忆中提炼出的长期稳定信息，沉淀到 `MEMORY.md`。

### 3.2 状态机（建议）

- `Goal.status`：`active | paused | done | archived`
- `Task.status`：`todo | in_progress | blocked | done | canceled`
- `GoalPlanSession.status`：`starting | in_progress | completed | error`
- `Companion.status`：`pending_certification | active`（UI 额外显示 `offline` / `waiting for pairing`）

---

## 4. 用户旅程与关键页面

### 4.1 典型流程（每日）

1. 打开 Dashboard：看到左侧 Goal 列表、中间详情、右侧 `What's Next` 与 `Recent Events`。
2. 选择一个 Goal 或 Task：在中间栏查看详情、创建 Task、编辑内容、手动 `Finish/Reopen`。
3. 若任务需要外部执行：在 Task 详情中创建 `AgentSpace`，绑定 Companion 与工作目录，进入远端工作区。
4. Agent/Skill 运行过程中持续上报事件；用户在 Dashboard 右侧事件流或 Task 详情事件区查看进展，这些行为同时进入 audit memory。
5. 系统按阈值把 audit memory 总结进当天 daily 记忆；用户可通过 `Calendar` 回顾完成记录，并在 `Memory` 页面查看 audit 日志、daily 记忆与长期记忆。

### 4.2 页面清单（MVP）

- `Dashboard`（`/goals`）：左侧 Goal 列表、中间 Goal/Task 详情、右侧 `What's Next` 与 `Recent Events`。
- `Plan Session`：Plan Mode 对话页面；支持继续追问、`Retry`、`Create`。
- `Task AgentSpace`：只读文件树 + 预览 + 远程终端。
- `Companion`：查看 Companion 列表、配对、删除、观察关联 AgentSpace。
- `Memory`：查看 audit 日志、daily 记忆、长期记忆与系统生成状态。
- `Calendar`：在顶部导航中以对话框打开，提供 `Month` / `Swimlane` 两种月视图。

---

## 5. 系统架构设计

### 5.1 逻辑架构

- `Core API Service`（FastAPI）
  - Goal/Task 管理
  - Plan Session 管理
  - Recommendation Engine（规则版）
  - Agent/Skill Event Gateway
  - Companion / AgentSpace / Remote Terminal Gateway
  - Calendar 与 Memory 数据聚合
  - Memory Summarizer / Rotation Scheduler

- `Web UI`
  - 读写 Core API
  - WebSocket 转发 Remote Terminal I/O
  - SSE 展示 Agent Session 输出

### 5.2 组件边界

1. **Planner（规划）**：`New Goal` 打开 `Plan Mode` 后创建 `goal_plan_sessions`，通过 LLM 生成草案并在 `Create` 时写库。
2. **Attention Scheduler（注意力调度）**：基于 Goal DDL、优先级、重要度、最近 Task 事件做单条推荐。
3. **Telemetry（遥测）**：统一写入 `events`，承接 `/api/agent/events` 与 `/api/skills/focus_report`。
4. **Companion Bridge**：通过 gRPC 长连接代理目录选择、文件只读访问、终端、Agent Session。
5. **AgentSpace Runtime**：围绕单个 Task 提供工作目录、远程终端、Prompt 注入、Agent Session 持久化。
6. **Memory Pipeline（记忆管线）**：把 Web 操作、Plan 交互、Agent 事件、Terminal I/O 写入 audit 记忆；在滚动阈值触发时总结到 daily 记忆；在次日 `00:00` 之后完成 daily 定稿并提炼长期记忆。

---

## 6. 数据模型（MVP 最小集合）

> 存储建议：MVP 使用 `SQLite`，以关系表为主；Artifact 大文本可存 DB，文件类存本地目录并记录路径。

### 6.1 表/实体（建议字段）

**已实现（当前仓库）**

- `goals`
  - `id, content, summary, description, status, priority, importance, due_date, created_at`

- `tasks`
  - `id, public_id, goal_id, title, summary, description, status, created_at, completed_at`

- `events`
  - `id, kind, agent, task_id (nullable), payload_json, created_at`
  - 说明：`kind` 是事件类型（例如 `agent.llm_call.completed`、`skill.focus_report`）；`payload` 保存原始结构化内容。

- `goal_plan_sessions`（Plan 模式会话）
  - `id, status, draft_content, due_date, turns, result_json, created_goal_id, created_at, updated_at`

- `goal_plan_messages`（Plan 模式消息）
  - `id, session_id, role, content, created_at`

- `agent_spaces`
  - `id, task_public_id, companion_id, root_path, agent_type, created_at, updated_at`

- `agent_sessions`
  - `id, session_id, space_id, task_public_id, companion_id, root_path, agent_type, status, created_at, updated_at`

- `agent_messages`
  - `id, session_id, role, request_id, content, done, error, created_at`

- `remote_terminal_sessions`
  - `id, space_id, task_public_id, companion_id, root_path, name, terminal_id, status, created_at, updated_at`

- `remote_terminal_outputs`
  - `id, space_id, terminal_id, data_b64, nbytes, created_at`

- `companions`
  - `id, device_id, name, base_url, status, auth_token, last_seen_at, pair_attempt_window_start, pair_attempt_count, created_at, updated_at`

### 6.2 Memory 文件布局与生命周期

- `audit memory`
  - 建议目录：`memory/audit/YYYY-MM-dd/HH-mm.log.md`
  - 内容：原始审计日志，覆盖 Goal/Task 的创建、编辑、`Finish`、删除，Plan Mode 交互，Agent 事件，以及 AgentSpace 内 web shell 的所有输入与返回值。
  - 轮转：满足任一条件即切文件：`1h` 或 `2000` 条记录。
  - TTL：`7 days`；到期后可由后台清理。

- `daily memory`
  - 文件名：`memory/daily/YYYY-MM-dd.md`
  - 内容：来自 audit memory 分段总结的日级记忆；每次 audit 文件轮转后，把该分段总结追加到当天文件。
  - 日终任务：每天 `00:00` 之后，后台启动一次总结任务，读取前一天的全部 daily 草稿并写回同名文件的最终版本。
  - TTL：永久保留。

- `long-term memory`
  - 文件名：`memory/MEMORY.md`
  - 内容：从日终定稿后的 daily 记忆中提炼出的稳定事实、用户偏好、长期约束与持续有效背景。
  - TTL：永久保留。

---

## 7. 推荐引擎（Attention Scheduler）

### 7.1 输入信号（MVP 可获得）

- 目标：`due_date`、`priority`、`importance`
- 任务：`status`、所属 Goal 的 DDL、最近是否有 `task.started/task.progress`
- 事件：按 Task 聚合的最新一条 `events` 记录，用于识别是否正在推进
- 记忆：当日 `daily memory` 与 `MEMORY.md` 中沉淀的用户偏好、稳定事实、长期约束

### 7.2 输出形式

- `Recommendation`（单条）
  - `type`：当前实现固定为 `do_task`
  - `target.goal_id`
  - `target.task_public_id`
  - `title`：一句话 next step
  - `why`：可解释理由（最多 3 条）
  - `expected_time_minutes`

### 7.3 策略（当前实现：可解释规则 + 单条排序）

建议采用线性打分：

- `Urgency`：DDL 越近分数越高
- `Priority`：Goal.priority
- `Importance`：Goal.importance
- `Context Freshness`：最近有 `task.started/task.progress` 的任务加分
- `Memory Match`：与 daily / long-term memory 中的偏好、长期约束、最近关注主题匹配时加分

当前不做 Review 队列优先，也不读取用户时间块；每次只返回 1 条推荐，避免再把选择压力转交给用户。

---

## 8. LLM 任务拆解（Planner）

### 8.1 触发点

- **显式进入 Plan Mode** 时触发拆解：用户在 Dashboard 的 `New Goal` 对话框开启 `Plan Mode=ON` 并点击 `Plan`，进入 Plan 会话流程。
- Plan Mode 的产出先以“草案”形式保存（人类在环）：在用户点击 `Create` 之前，不写入任何 `tasks`。
- （可选后续）用户在 Task 上点击“继续拆解”，让 LLM 生成更细的步骤/依赖。

### 8.2 产出要求（结构化）

- LLM 输出必须为结构化 JSON：
  - 任务标题、描述、优先级建议、估时、依赖（可为空）、验收标准（Definition of Done）
- **Plan Mode 阶段仅保存草案**：写入 `goal_plan_sessions.result_json` 与 `goal_plan_messages`（便于追溯与二次编辑）。
- 用户点击 `Create` 后，才会把选中的任务写入 `tasks` 并创建 Goal。
- 当前 Plan Ready 阶段支持：继续补充要求、勾选要创建的 tasks、`Retry`、`Create`；不提供页面内 step 编辑与 `Cancel` 按钮。

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
- `POST /api/agent_spaces/{space_id}/agent/sessions/{session_id}/send`：向 Agent Session 发送消息，并在服务端注入 OpenFocus prompt 头部

### 9.3 ContextPack（最小字段）

- 当前实现不单独落地 `ContextPack` 实体。
- 发送 Agent Session 消息时，服务端会注入最小 OpenFocus prompt：
  - `taskId=<Task.public_id>`
  - `agentSessionId=<session_id>`
  - `openfocusBaseUrl=<base_url>`
  - 进度上报要求：`POST /api/agent/events`
  - 最终结果上报要求：`POST /api/skills/focus_report`

### 9.4 鉴权与可靠性

- 本地部署默认信任 `127.0.0.1`；若允许局域网访问，需要 `agent_key + HMAC`。
- 当前 `events` API 采用“每次请求直接落库”的简化模型，不要求 `event_id` 幂等键。
- 网络抖动重试：Skill SDK 应支持指数退避。

### 9.5 Agent Session（当前实现）

- 当前通过 Companion 托管的 `Agent Session` 提供会话能力，而不是独立的推荐提示词生成接口。
- 相关接口：
  - `GET /api/agent_spaces/{space_id}/agent/sessions`
  - `POST /api/agent_spaces/{space_id}/agent/sessions/new`
  - `GET /api/agent_spaces/{space_id}/agent/sessions/{session_id}/messages`
  - `POST /api/agent_spaces/{space_id}/agent/sessions/{session_id}/send`
  - `GET /api/agent_spaces/{space_id}/agent/sessions/{session_id}/sse`
- 当前前端尚未提供完整的 Agent chat tab；后端能力先行落地，供后续页面集成。

---

## 10. Human Confirmation Loop

### 10.1 当前闭环

- 所有 Agent/Skill 上报先进入 `events`，展示在 Dashboard `Recent Events` 与 Task 详情 `Event` 区块。
- `task.completed` / `skill.focus_report(status=succeeded)` 只表示“上报完成”，**不自动标记 Task 为 done**。
- Task 是否真正完成，由用户在 Task 详情中点击 `Finish`；已完成任务可点击 `Reopen` 恢复。

### 10.2 反哺范围

- 当前推荐引擎主要读取 Goal 属性与最近 Task 事件。
- 更复杂的 Review verdict、返工任务自动生成、质量评分机制留待后续版本。

---

## 11. 记忆系统与回顾能力

### 11.1 Audit Memory

- Audit Memory 是 OpenFocus 的原始审计层，默认覆盖所有用户与 Agent 的关键行为。
- 必须纳入的事件类型包括：
  - Goal / Task 的创建、编辑、`Finish`、删除
  - Plan Mode 的用户输入、Agent 回复、`Retry`、`Create`
  - `/api/agent/events`、`/api/skills/focus_report` 等 Agent/Skill 上报
  - AgentSpace web shell 的所有输入与返回值
- Audit Memory 以 rolling 文件形式保存，按 `1h` 或 `2000` 条记录切分，保留 `7 days`。

### 11.2 Daily Memory

- 每个 audit 文件在轮转时，都会触发一次总结任务，把该分段摘要写入当天的 `daily memory`。
- `daily memory` 文件名固定为 `YYYY-MM-dd.md`，同一天内允许多次追加阶段性总结。
- 每天 `00:00` 之后，系统必须为前一天启动一次日终总结任务，生成当天 daily 记忆的最终版本。

### 11.3 Long-term Memory

- 日终总结完成后，系统从 finalized daily 记忆中提炼长期稳定信息，写入 `MEMORY.md`。
- 长期记忆只保留相对稳定的用户偏好、事实、长期约束，不直接拷贝瞬时任务噪音。
- `MEMORY.md` 永久保留，并参与后续推荐与规划。

### 11.4 当前回顾能力与延后项

- 已有回顾入口：Dashboard `Recent Events`、Task 详情 `Event`、`Calendar`、`Memory`。
- Memory 页面要求同时可查看 audit 日志、daily 记忆与 long-term memory；audit 可以按 rolling 文件组织呈现。
- 延后能力：独立的 `History & Metrics` 页面、`real vs agent_sum` 与 token 趋势图、基于 Run/Review/Artifact 的复盘视图。

---

## 12. Companion 机制

Companion 是运行在本机（或远端工作机）上的常驻桥接进程，用于把浏览器环境无法直接完成的“本机能力”提供给 OpenFocus。

典型例子：系统目录选择器返回**绝对路径**、托管 `coco/codex` 的交互式进程（PTY）、流式读写 stdin/stdout、列出由 OpenFocus 托管的 agent 会话等。

### 12.1 目标

- 让 OpenFocus 保持 Web 形态（local-first control plane），同时具备“像桌面应用一样”的本机能力。
- 支持多机：每台机器运行一个 Companion，统一接入到用户主机上的 OpenFocus，实现跨机器的 AgentSpace/会话托管。
- 所有动作可追溯：命令、输出、状态变更都可落库为 `events`，并进入 audit memory / daily memory / long-term memory 体系。

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
4) **审计与追溯**：所有命令/结果/输出都写入 OpenFocus `events`，同时进入 audit memory；Companion 本地可选保留辅助日志。
5) **高危动作二次确认（可选）**：写文件/执行外部命令/打开敏感目录等需要本机弹窗确认或策略开关。

### 12.5 远程终端（Remote Terminal）

目标：在 AgentSpace 的右侧终端区域提供“可交互终端”，让用户直接在远端工作机（Companion 所在机器）的 workspace 中运行命令。

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

- Goal/Task CRUD
- Dashboard 基础版（三栏布局 + Goal/Task 详情）
- SQLite 持久化

验收：可创建目标与任务，刷新页面不丢数据。

### 13.2 Milestone 2：LLM 拆解与下一步推荐（2-3 周）

- Plan Mode（先规划再创建）→ LLM 生成任务拆解草案（先写入 plan session 草案，Create 后写库 Goal/Tasks）
- `GET /api/recommendations/next` 返回下一步推荐（带 why）

验收：Plan Mode 的 Create 后能创建 Goal/Tasks；Dashboard 能显示“下一步建议”。

### 13.3 Milestone 3：Agent 派发 + Skill 遥测 + Review（3-5 周）

- Companion 注册、配对、目录选择
- AgentSpace + Remote Terminal + Agent Session 基础能力
- `/api/agent/events` 与 `/api/skills/focus_report` 遥测接入；Task 是否完成由人确认（不自动标记 done）

验收：外部 Agent 能接入并回传事件；用户可在 Task 详情页手动确认完成/重新打开，并能在 AgentSpace 中使用远端终端。

### 13.4 Milestone 4：Calendar & Memory（5-6 周）

- Calendar 月视图与 Swimlane
- 三层记忆系统：audit rolling / daily `YYYY-MM-dd.md` / long-term `MEMORY.md`

验收：系统能按阈值轮转 audit memory、生成 daily 记忆并在次日完成日终定稿，同时从中提炼长期记忆。

---

## 14. 关键决策记录（已确认）

- 产品形态：`Web`，支持本地部署本地使用。
- 外部 Agent：面向 `Codex / Claude Code / OpenClaw / Coco / Trae` 等运行时。
- 任务拆解：由 `LLM` 负责生成任务与步骤。
- token 统计：MVP 仅从模型 API 返回获取（不兼容黑盒）。
- 推荐粒度：直接推荐用户“下一步要做的事情”（可执行 next action）。
- UI labels、user-facing copy、code comments 使用英文；系统文档可使用中文。
- `New Goal` 的 `Auto` 为智能提炼 `Title` 的能力，而不是简单首行回填。
- 记忆系统采用三层结构：`audit memory`（7 天 rolling）、`daily memory`（`YYYY-MM-dd.md`）、`long-term memory`（`MEMORY.md`）。
