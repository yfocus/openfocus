<!-- SPDX-License-Identifier: Apache-2.0 -->
# OpenFocus Architecture

版本：`v0.1`
日期：`2026-05-11`

> 校准基线：本设计以当前 README 中的业务定位为准。OpenFocus 是 **agent-native workspace**，核心价值是帮助用户管理目标、跟踪执行，并通过注意力编排降低多 Agent 协作时的人类上下文切换成本。

---

## 背景与目标

AI 时代的个人工作模式正在从“亲力亲为”切换为“提出目标 + 组织多个 Agent 执行 + 人类审查”。在该模式下，瓶颈不再是单次执行能力，而是**注意力带宽与上下文切换成本**：

- 用户同时推进多个目标与任务，容易陷入“下一步做什么”的决策疲劳。
- 多个 Agent 并行产出后，用户需要快速定位应优先 review 哪些结果、如何推动目标链继续向前。
- 任务已经发布给 Agent 后，用户常常不知道还要等多久、下一段注意力该投向哪里。
- 多个 Agent 同时运行时，用户需要总览进展、识别完成时机，并及时审查结果。

### 核心目标（必须满足）

1. **聚焦目标和任务**：用户管理 Goal / Task、审查结果；具体执行优先交给 Agent。
2. **进展跟踪**：Agent/Skill 运行期间持续上报事件，用户通过 Dashboard 与 Task 详情总揽进展。
3. **Next Move**：系统基于 Goal、Task、Agent 运行事件与 Memory，推荐下一步应该推进的 task。
4. **Agent 一等公民**：通过 Companion + remote terminal + prompt management 集成支持命令行的外部 Agent，同时保留 OpenFocus 内置 Agent 能力。
5. **Inspiration Space**：支持先讨论/孵化再创建；Goal/Tasks 只在用户确认 `Publish` 后写库。
6. **Memory Evolution**：记录关键行为与事件，并总结为 audit / daily / long-term memory，供推荐与提示词上下文复用。
7. **人工确认**：Task 是否完成必须由人确认；Agent 上报完成只表示“可审查”，不自动把 Task 标记为 done。
8. **多端统一目标**：当前实现以 Web 为主；手机端接入是产品目标但不属于当前已落地范围。

### 非目标（MVP 不做或延后）

- 不做全自动闭环的“无监督决策代理”（系统不能在无用户确认情况下擅自改目标方向）。
- 不追求复杂 RL/端到端学习式调度，当前基线采用**可解释的规则推荐**。
- 不把 token/Agent 时等吞吐量指标作为当前核心产品主线；当前重点是目标、任务、进展、推荐与审查闭环。
- 不提供独立的 `Review Inbox`、`History & Metrics` 页面；当前以 Dashboard 事件流、Task 详情事件区、Calendar 为主。
- 不提供独立的 `recommended_prompt` 生成 API；当前通过 AgentSpace/remote terminal 的 prompt zone auto prompts 或 Agent Session 注入 OpenFocus 上下文。
- 不在当前 Web 实现中提供独立手机端客户端；移动端体验作为后续多端统一方向。

### 语言与文案规范

- `architecture.md`、`product-requirements.md` 等系统文档可使用中文。
- 用户可见的按钮、标签、导航、状态文案、提示信息统一使用英文。
- 代码实现、注释、接口字段命名统一使用英文。

---

## 产品形态与部署

### 形态

- **Web 应用（当前实现）**：在浏览器中使用（Dashboard / Inspiration / AgentSpace / Companion / Memory / Calendar）。
- **移动端接入（产品目标）**：用于随时随地启动/审查任务、记录灵感；当前仓库尚未实现独立手机端。
- **本地部署本地使用**（Local-first）：默认仅监听 `127.0.0.1`，数据保存在本地。

### 推荐技术栈（Python）

- 后端：`FastAPI` + `Pydantic` + `SQLAlchemy`（或 `SQLModel`）
- 存储：`SQLite`（MVP）→ 可扩展 `PostgreSQL`
- 异步任务：`RQ`/`Celery`（可选，MVP 可先用后台线程/async queue）
- 实时推送：`WebSocket`（FastAPI 原生支持）
- 前端：`Jinja2` 页面 + Vite/React islands（当前用于 AgentSpace 与 InspirationSpace 等交互模块）

---

## 核心概念模型

### 概念定义

- `Goal`：用户想达成的结果（可分层：Objective/Key Result/Initiative）。
- `Task`：为达成目标而执行的工作单元，可由人或 Agent 执行。
- `Agent`：OpenFocus 内置 Agent 与用户自带的命令行 Agent。外部 Agent 通过 Companion + remote terminal + prompt management 轻量接入；OpenFocus 不强绑定具体 Agent 运行时。
- `Event`：系统中的统一遥测记录，承接 Web 操作、Agent 进度。
- `InspirationSpace`：围绕一个灵感主题的持续讨论/工作空间，保存消息、资源、草案版本、发布结果与独立 workspace，用于在正式创建 Goal/Tasks 之前进行探索、澄清与结构化。它支持 `Built-in Planner` 与 `Bring Your Own Agent` 两种模式。
- `AgentSpace`：围绕单个 Task 的工作区，绑定 workdir、Companion、remote terminal、Agent Session。
- `Companion`：提供目录选择、文件读取、终端、Agent Session 的本机桥接进程。
- `Audit Memory`：用户与 Agent 在 OpenFocus 内的原始审计日志，采用 rolling 文件保存。
- `Daily Memory`：按天汇总的记忆文件，承接 Audit Memory 的阶段性总结与日终定稿。
- `Long-term Memory`：从 daily 记忆中提炼出的长期稳定信息，沉淀到 `MEMORY.md`。

### 状态机（建议）

- `Goal.status`：`active | paused | done | archived`
- `Task.status`：`todo | in_progress | blocked | done | canceled`
- `InspirationSpace.status`：`open | closed | publishing | published | error`
- `InspirationSpace.mode`：`built_in | terminal`，表示默认工作台形态；`open` 状态下可从 built-in 开启 terminal，一旦进入 terminal 模式，详情页主工作区由 remote terminal 取代内置 agent 对话区，但资源、草案与发布流程保持一致。
- `Companion.status`：`pending_certification | active`（UI 额外显示 `offline` / `waiting for pairing`）

---

## 用户旅程与关键页面

### 典型流程（每日）

1. 打开 Dashboard：看到左侧 Goal 列表、中间详情、右侧 `Next Move` 与 `Recent Events`。
   - `Next Move` 读取当前 Goals / Tasks / Events / Memory / Feedback 后，输出最多 3 个推荐 task。
2. 当用户还没有想清楚目标或需要进一步澄清思路时，进入 `Inspiration` 页面创建一个 `InspirationSpace`，选择使用内建规划 Agent，或打开 remote terminal 使用自己偏好的外部 agent。
3. 用户在 `InspirationSpace` 中多轮追问、补充资源、生成/同步 `Summary`、生成/修订 `Draft vN`，并在确认后正式发布为 `1 个 Goal + 多个 Tasks`；正式发布后生成只读 `Published Summary`。
4. 选择一个 Goal 或 Task：在 Dashboard 中查看详情、继续编辑、手动 `Finish/Reopen`。
5. 若任务需要外部执行：在 Task 详情中创建 `AgentSpace`，绑定 Companion 与工作目录，进入远端工作区。
6. Agent/Skill 运行过程中持续上报事件；用户在 Dashboard 右侧事件流或 Task 详情事件区查看进展，这些行为同时进入 audit memory。
7. 系统按阈值把 audit memory 总结进当天 daily 记忆；每次 summary 完成后立即滚动出新的 audit 文件，用户也可在 `Memory` 页面手动点击 `Summary` 触发同样流程。用户可通过 `Calendar` 回顾完成记录，并在 `Memory` 页面查看 audit 日志、daily 记忆与长期记忆。

### 页面清单（MVP）

- `Dashboard`（`/goals`）：左侧 Goal 列表、中间 Goal/Task 详情、右侧 `Next Move` 与 `Recent Events`。
- `Inspiration`（`/inspirations`、`/inspirations/{id}`）：灵感空间列表与详情页；支持持续讨论、资源管理、草案版本、发布确认与 `Fork New Inspiration`。
- `Task AgentSpace`：只读文件树 + 预览 + 远程终端。
- `Companion`：查看 Companion 列表、配对、删除、观察关联 AgentSpace。
- `Memory`：查看 audit 日志、daily 记忆、长期记忆；支持手动触发 audit summary，并区分已总结与未总结的 audit 文件。
- `Calendar`：在顶部导航中以对话框打开，提供 `Month` / `Swimlane` 两种月视图。

---

## 系统架构设计

### 逻辑架构

- `Core API Service`（FastAPI）
  - Goal/Task 管理
  - InspirationSpace 管理（消息、资源、草案、发布）
  - Next Move（多信号推荐与反馈学习）
  - Agent/Skill Event Gateway
  - Companion / AgentSpace / Remote Terminal Gateway
  - Calendar 与 Memory 数据聚合
  - Memory Summarizer / Rotation Scheduler

- `Web UI`
  - 读写 Core API
  - WebSocket 转发 Remote Terminal I/O
  - SSE 展示 Agent Session 输出

### 组件边界

1. **Inspiration Planner（灵感规划）**：`InspirationSpace` 承载持续讨论、资源引用、terminal agent 桥接、草案版本与发布确认；在用户点击 `Publish` 之前，不写入 Goal/Tasks。
2. **Next Move（注意力调度）**：读取 Goals / Tasks / Events / Memory / Feedback，综合输出最多 3 个 task 推荐，并根据用户 dismiss 反馈持续学习。
3. **Telemetry（遥测）**：统一写入 `events`，承接 `/api/agent/events` 与 `/api/skills/focus_report`。
4. **Companion Bridge**：通过 gRPC 长连接代理目录选择、文件只读访问、终端、Agent Session。
5. **AgentSpace Runtime**：围绕单个 Task 提供工作目录、远程终端、Prompt 注入、Agent Session 持久化。
6. **Memory Pipeline（记忆管线）**：把 Web 操作、Inspiration 交互、Agent 事件、Terminal I/O 写入 audit 记忆；在滚动阈值触发或用户手动触发时，把当前 audit 文件总结到 daily 记忆并立即滚动出新的 audit 文件；在次日 `00:00` 之后完成 daily 定稿并提炼长期记忆。

---

## 数据模型（MVP 最小集合）

> 存储建议：MVP 使用 `SQLite`，以关系表为主；大文本保存为结构化表字段或 workspace/resource 文件。早期文档中曾出现的“独立产物对象”不再作为产品概念使用，Agent 产出统一沉淀为事件、终端输出、资源文件或发布总结。

### 表/实体（建议字段）

**当前核心实体 + 本次规划新增**

- `goals`
  - `id, title, content, status, priority, importance, due_date, source_inspiration_space_id, source_inspiration_draft_id, created_at`
  - 说明：`title` 是用户/发布流程明确提供的标题；`content` 是正文。列表中需要短展示时只在渲染层截断 `title`，不再维护独立 `summary`/`description` 语义，也不通过 Agent/LLM 自动提炼标题。

- `tasks`
  - `id, public_id, goal_id, title, content, status, task_type, estimated_minutes, context_key, source_inspiration_space_id, source_inspiration_draft_id, created_at, completed_at`
  - 说明：
    - `title` 是用户/发布流程明确提供的标题；`content` 是正文。列表中需要短展示时只在渲染层截断 `title`，不再维护独立 `summary`/`description` 语义，也不通过 Agent/LLM 自动提炼标题。
    - `task_type`：任务类型（如 `deep_work | communication | review | execution | admin`），可由用户指定或系统后补全。
    - `estimated_minutes`：任务预计耗时，供 Next Move 评估时间块匹配度。
    - `context_key`：上下文标签，用于估算与当前工作流之间的切换成本（例如 repo / topic / workstream）。

- `events`
  - `id, kind, agent, task_id (nullable), payload_json, created_at`
  - 说明：`kind` 是事件类型（例如 `agent.llm_call.completed`、`skill.focus_report`）；`payload` 保存原始结构化内容。

- `next_move_runs`
  - `id, generated_at, trigger_kind, context_summary_json, recommendations_json, created_at`
  - 说明：记录一次 Next Move 产出的完整上下文摘要与推荐结果，便于解释、调试、回放与学习。

- `next_move_feedback`
  - `id, run_id, task_public_id, feedback_type, reason_code, reason_text, learned_summary, created_at`
  - 说明：记录用户对推荐的显式反馈；`feedback_type` MVP 先支持 `dismiss`（不喜欢 / 暂不合适）。

- `inspiration_spaces`
  - `id, title, status, mode, workspace_path, published_goal_id, forked_from_space_id, last_activity_at, message_turn_count, created_at, updated_at, closed_at, published_at`
  - 说明：`mode` 支持 `built_in | terminal`；`workspace_path` 指向该 space 独立工作目录，目录下必须包含 `resources/`。

- `inspiration_messages`
  - `id, space_id, role, kind, content, draft_version, created_at`
  - 说明：`kind` 用于区分普通消息、草案消息、标题建议、发布事件等系统消息。

- `inspiration_resources`
  - `id, space_id, resource_seq_id, type, name, text_content, url_content, file_path, external_path, source, is_system_generated, created_at, updated_at, deleted_at`
  - 说明：`type` 支持 `url | image | text | summary`；所有资源都必须在 `workspace_path/resources/` 下有文件表示。`source` 支持 `user | built_in_agent | terminal_agent | system`，用于区分手动上传、内建 agent 生成、terminal agent 同步与系统归档。

- `inspiration_terminals`（可复用 `remote_terminal_sessions`，也可做轻量视图/别名）
  - `id, inspiration_space_id, companion_id, root_path, terminal_id, name, status, created_at, updated_at`
  - 说明：terminal 的 `root_path` 固定为 `inspiration_spaces.workspace_path`；输出仍可复用 `remote_terminal_outputs` 按 `terminal_id` 记录。

- `inspiration_drafts`
  - `id, space_id, version, goal_title, goal_content, tasks_json, open_questions, rejected_or_deferred_ideas, source_message_id, created_at`
  - 说明：实现可保留旧列名 `goal_description` 作为兼容存储，但产品语义上它是待发布 Goal 的 `content`；`tasks_json` 中每个 task 也以 `title` / `content` 为准，可兼容读取旧草案里的 `description`。

- `inspiration_publish_records`
  - `id, space_id, draft_id, created_goal_id, created_task_ids_json, deferred_tasks_json, summary_resource_id, created_at`

- `agent_spaces`
  - `id, task_public_id, companion_id, root_path, agent_type, created_at, updated_at`

- `agent_space_prompts`
  - `id, title, content, enabled, auto_enabled, created_at, updated_at`
  - 说明：`enabled` 控制 prompt 是否展示在 AgentSpace prompt zone；`auto_enabled` 控制该 prompt 是否在每次 AgentSpace terminal input submit 时自动拼接进输入。`auto_enabled` 不替代用户显式确认：它只改变提示词注入，不触发额外 agent 操作；Agent Session `/send` 不追加这些自定义 prompts。

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

### Memory 文件布局与生命周期

- `audit memory`
  - 建议目录：`memory/audit/YYYY-MM-dd/YYYY-MM-dd_HH-mm-ss.md`
  - 内容：原始审计日志，覆盖 Goal/Task 的创建、编辑、`Finish`、删除，Inspiration 交互，Agent 事件，以及 AgentSpace 内 web shell 的所有输入与返回值。
  - 轮转：满足任一条件即切文件：`1h` 或 `2000` 条记录。
  - summary 规则：每次自动轮转或手动点击 `Summary` 后，都必须先为当前 audit 文件生成 summary，再立刻创建一个新的 audit 文件继续接收后续日志。
  - 展示规则：Memory 页面里的 audit 文件列表必须区分“已做过 summary”的文件与“尚未 summary”的文件。
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
