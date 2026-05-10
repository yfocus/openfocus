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
3. **Inspiration**：支持先讨论/孵化再创建，Goal/Tasks 只在 `Publish` 后写库。
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

- **Web 应用**：在浏览器中使用（Dashboard / Inspiration / AgentSpace / Companion / Memory / Calendar）。
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
- `InspirationSpace`：围绕一个灵感主题的持续讨论/工作空间，保存消息、资源、草案版本、发布结果与独立 workspace，用于在正式创建 Goal/Tasks 之前进行探索、澄清与结构化。它支持 `Built-in Planner` 与 `Bring Your Own Agent` 两种模式。
- `AgentSpace`：围绕单个 Task 的工作区，绑定 workdir、Companion、remote terminal、Agent Session。
- `Companion`：提供目录选择、文件读取、终端、Agent Session 的本机桥接进程。
- `Audit Memory`：用户与 Agent 在 OpenFocus 内的原始审计日志，采用 rolling 文件保存。
- `Daily Memory`：按天汇总的记忆文件，承接 Audit Memory 的阶段性总结与日终定稿。
- `Long-term Memory`：从 daily 记忆中提炼出的长期稳定信息，沉淀到 `MEMORY.md`。

### 3.2 状态机（建议）

- `Goal.status`：`active | paused | done | archived`
- `Task.status`：`todo | in_progress | blocked | done | canceled`
- `InspirationSpace.status`：`open | closed | publishing | published | error`
- `InspirationSpace.mode`：`built_in | terminal`，表示默认工作台形态；`open` 状态下可从 built-in 开启 terminal，一旦进入 terminal 模式，详情页主工作区由 remote terminal 取代内置 agent 对话区，但资源、草案与发布流程保持一致。
- `Companion.status`：`pending_certification | active`（UI 额外显示 `offline` / `waiting for pairing`）

---

## 4. 用户旅程与关键页面

### 4.1 典型流程（每日）

1. 打开 Dashboard：看到左侧 Goal 列表、中间详情、右侧 `What's Next` 与 `Recent Events`。
   - `What's Next` 由 `Next Move Agent` 读取当前 Goals / Tasks / Events / Memory / Feedback 后，输出 3 个推荐 task。
2. 当用户还没有想清楚目标或需要进一步澄清思路时，进入 `Inspiration` 页面创建一个 `InspirationSpace`，选择使用内建规划 Agent，或打开 remote terminal 使用自己偏好的外部 agent。
3. 用户在 `InspirationSpace` 中多轮追问、补充资源、生成/同步 `Summary`、生成/修订 `Draft vN`，并在确认后正式发布为 `1 个 Goal + 多个 Tasks`；正式发布后生成只读 `Published Summary`。
4. 选择一个 Goal 或 Task：在 Dashboard 中查看详情、继续编辑、手动 `Finish/Reopen`。
5. 若任务需要外部执行：在 Task 详情中创建 `AgentSpace`，绑定 Companion 与工作目录，进入远端工作区。
6. Agent/Skill 运行过程中持续上报事件；用户在 Dashboard 右侧事件流或 Task 详情事件区查看进展，这些行为同时进入 audit memory。
7. 系统按阈值把 audit memory 总结进当天 daily 记忆；每次 summary 完成后立即滚动出新的 audit 文件，用户也可在 `Memory` 页面手动点击 `Summary` 触发同样流程。用户可通过 `Calendar` 回顾完成记录，并在 `Memory` 页面查看 audit 日志、daily 记忆与长期记忆。

### 4.2 页面清单（MVP）

- `Dashboard`（`/goals`）：左侧 Goal 列表、中间 Goal/Task 详情、右侧 `What's Next` 与 `Recent Events`。
- `Inspiration`（`/inspirations`、`/inspirations/{id}`）：灵感空间列表与详情页；支持持续讨论、资源管理、草案版本、发布确认与 `Fork New Inspiration`。
- `Task AgentSpace`：只读文件树 + 预览 + 远程终端。
- `Companion`：查看 Companion 列表、配对、删除、观察关联 AgentSpace。
- `Memory`：查看 audit 日志、daily 记忆、长期记忆；支持手动触发 audit summary，并区分已总结与未总结的 audit 文件。
- `Calendar`：在顶部导航中以对话框打开，提供 `Month` / `Swimlane` 两种月视图。

---

## 5. 系统架构设计

### 5.1 逻辑架构

- `Core API Service`（FastAPI）
  - Goal/Task 管理
  - InspirationSpace 管理（消息、资源、草案、发布）
  - Next Move Agent（多信号推荐与反馈学习）
  - Agent/Skill Event Gateway
  - Companion / AgentSpace / Remote Terminal Gateway
  - Calendar 与 Memory 数据聚合
  - Memory Summarizer / Rotation Scheduler

- `Web UI`
  - 读写 Core API
  - WebSocket 转发 Remote Terminal I/O
  - SSE 展示 Agent Session 输出

### 5.2 组件边界

1. **Inspiration Planner（灵感规划）**：`InspirationSpace` 承载持续讨论、资源引用、terminal agent 桥接、草案版本与发布确认；在用户点击 `Publish` 之前，不写入 Goal/Tasks。
2. **Next Move Agent（注意力调度）**：读取 Goals / Tasks / Events / Memory / Feedback，综合输出 3 个 task 推荐，并根据用户 dismiss 反馈持续学习。
3. **Telemetry（遥测）**：统一写入 `events`，承接 `/api/agent/events` 与 `/api/skills/focus_report`。
4. **Companion Bridge**：通过 gRPC 长连接代理目录选择、文件只读访问、终端、Agent Session。
5. **AgentSpace Runtime**：围绕单个 Task 提供工作目录、远程终端、Prompt 注入、Agent Session 持久化。
6. **Memory Pipeline（记忆管线）**：把 Web 操作、Inspiration 交互、Agent 事件、Terminal I/O 写入 audit 记忆；在滚动阈值触发或用户手动触发时，把当前 audit 文件总结到 daily 记忆并立即滚动出新的 audit 文件；在次日 `00:00` 之后完成 daily 定稿并提炼长期记忆。

---

## 6. 数据模型（MVP 最小集合）

> 存储建议：MVP 使用 `SQLite`，以关系表为主；Artifact 大文本可存 DB，文件类存本地目录并记录路径。

### 6.1 表/实体（建议字段）

**当前核心实体 + 本次规划新增**

- `goals`
  - `id, content, summary, description, status, priority, importance, due_date, source_inspiration_space_id, source_inspiration_draft_id, created_at`

- `tasks`
  - `id, public_id, goal_id, title, summary, description, status, task_type, estimated_minutes, context_key, source_inspiration_space_id, source_inspiration_draft_id, created_at, completed_at`
  - 说明：
    - `task_type`：任务类型（如 `deep_work | communication | review | execution | admin`），可由用户指定或系统后补全。
    - `estimated_minutes`：任务预计耗时，供 Next Move Agent 评估时间块匹配度。
    - `context_key`：上下文标签，用于估算与当前工作流之间的切换成本（例如 repo / topic / workstream）。

- `events`
  - `id, kind, agent, task_id (nullable), payload_json, created_at`
  - 说明：`kind` 是事件类型（例如 `agent.llm_call.completed`、`skill.focus_report`）；`payload` 保存原始结构化内容。

- `next_move_runs`（新增）
  - `id, generated_at, trigger_kind, context_summary_json, recommendations_json, created_at`
  - 说明：记录一次 Next Move Agent 产出的完整上下文摘要与 3 个推荐结果，便于解释、调试、回放与学习。

- `next_move_feedback`（新增）
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
  - `id, space_id, version, goal_title, goal_description, tasks_json, open_questions, rejected_or_deferred_ideas, source_message_id, created_at`

- `inspiration_publish_records`
  - `id, space_id, draft_id, created_goal_id, created_task_ids_json, deferred_tasks_json, summary_resource_id, created_at`

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

---

## 7. Next Move Agent（Attention Scheduler）

### 7.1 输入信号（MVP 可获得）

- Goals：`due_date`、`priority`、`importance`、状态、所属主题
- Tasks：`status`、`task_type`、`estimated_minutes`、`context_key`、所属 Goal 的 DDL、最近是否有 `task.started/task.progress`
- Events：按 Task / Goal 聚合的近期 `events`，用于识别连续性、阻塞、切换成本与最近推进轨迹
- Memory：当日 `daily memory` 与 `MEMORY.md` 中沉淀的用户偏好、稳定事实、长期约束、长期工作模式
- Feedback：用户对历史推荐的拒绝理由、纠偏意见、已学习到的偏好/反偏好

### 7.2 输出形式

- `RecommendationSet`（固定 3 条）
  - `generated_at`
  - `trigger_kind`：例如 `state_changed | periodic_refresh | manual_refresh | feedback_submitted`
  - `items[3]`
    - `type`：当前固定为 `do_task`
    - `rank`：1~3
    - `target.goal_id`
    - `target.task_public_id`
    - `title`
    - `task_type`
    - `expected_time_minutes`
    - `context_switch_cost`：对外可解释为 `low | medium | high`
    - `why`：可解释理由（最多 3 条）
    - `confidence`：`high | medium | low`

### 7.3 Agent 处理流程

建议采用“候选收集 → 硬过滤 → 特征评估 → 排序与去重 → 解释生成”的多阶段流程：

1. **候选收集**
   - 收集所有可执行的 task 候选。
   - 必须排除：`done`、`canceled`、已删除、所属 goal 已完成/归档的 task。
2. **硬过滤**
   - 过滤掉当前不可打开、缺失关键上下文、明显不适合作为 next step 的 task。
3. **特征评估**
   - `Urgency`：DDL 越近、风险越高，分数越高。
   - `Importance/Priority`：高重要度、高优先级加分。
   - `Time Fit`：预计耗时与当前推荐场景匹配时加分；过长任务在碎片场景下降权。
   - `Context Continuity`：若与最近推进的 goal / task / topic / workspace 连续，则降低切换成本并加分。
   - `Task Type Fit`：结合用户偏好判断是否适合当前注意力状态。
   - `Memory Match`：与 `daily memory` / `MEMORY.md` 中用户长期偏好、近期关注主题相匹配时加分。
   - `Feedback Penalty`：若同类推荐近期被多次 dismiss，则降权。
4. **排序与去重**
   - 最终返回 3 个 task。
   - 要避免 3 个结果完全重复同一模式；但允许为了降低切换成本，保留同一 goal 下的连续推进项。
5. **解释生成**
   - 每个推荐都要生成面向人类的简短理由，而不是暴露内部 score 明细。

### 7.4 反馈学习闭环

用户可以对任意推荐提交 `dismiss` 反馈，Next Move Agent 必须形成闭环：

1. 记录原始反馈：推荐的是哪个 task、用户给出的 reason code / free text。
2. 总结学习结论：把多次反馈归纳为结构化偏好，例如：
   - “上午不喜欢需要 2h 以上的深度任务”
   - “用户更偏好延续当前 repo 的 review，而不是切去新主题”
   - “DDL 不紧的 admin 类任务应降低优先级”
3. 写回可持续复用的载体：
   - 短期结论进入 `daily memory`
   - 稳定偏好进入 `MEMORY.md`
   - 结构化反馈记录进入 `next_move_feedback`
4. 下一轮推荐必须读取这些学习结果，而不是只在当前会话中临时生效。

### 7.5 触发机制

- Goal / Task 状态变化时触发
- 用户提交推荐反馈时触发
- 距上次分析超过 `30 分钟` 时触发
- 用户手动点击刷新时触发

### 7.6 产品约束

- 每次固定返回 `3` 个推荐 task，不返回 1 个，也不返回长列表。
- 推荐必须可解释，但 explanation 面向用户，不暴露原始内部规则表。
- 推荐系统必须读取 Memory 与 Feedback，不能只看 Goal.priority / due_date 之类的静态字段。
- 若信息不足，仍需返回 3 个候选并标记较低信心，而不是空结果。

---

## 8. Inspiration Planner（灵感规划）

### 8.1 触发点

- 用户创建 `InspirationSpace` 时选择默认模式：`Built-in Planner` 或 `Bring Your Own Agent`。
- `Built-in Planner` 模式下，内建规划 Agent 通过持续追问、资源引用与上下文澄清来帮助用户收敛目标。
- `Bring Your Own Agent` 模式下，OpenFocus 启动 remote terminal，用户在 terminal 中运行自己偏好的 agent；此时详情页主工作区完全切换为 terminal，不再展示内置 agent 对话流、消息输入框、terminal header 草案按钮或 `Suggest Titles` / `Generate Draft` 等内置交互入口，也不展示 `Agent Mode` 开关。OpenFocus 不解析 terminal 对话语义，只通过 `resources/draft_summary.md` 与草案/发布链路桥接。
- terminal agent 是“不受信协作者”：它可以在 workspace 中产出文件，但不能直接写入 Goal/Task，不能绕过 OpenFocus 的草案生成、用户确认与发布链路。
- built-in 模式下，当用户显式触发 `/draft_goal_tasks` 或内建 Agent 判断上下文已经足够完整时，可生成新的 `Draft vN`；terminal 模式下通过 `Prompt Zone` 的 `Create Goal` 选择一个已同步 resource，并基于该 resource 生成 `Draft vN`。
- 在用户点击 `Publish` 之前，不写入任何 Goal/Tasks；所有结构化结果都先保存为草案。
- `New Goal` 对话框不再承载 Plan Mode；需要规划或灵感孵化时，统一进入 `Inspiration` 模块。

### 8.1.1 Inspiration Workspace 与 Terminal Runtime

- 每个 `InspirationSpace` 创建时必须分配独立 `workspace_path`，建议目录形态为 `.data/inspirations/{space_id}/`。
- workspace 初始化时必须创建：
  - `resources/`：用户资源、terminal agent 产物与系统 summary 的文件目录。
- `url`、`text`、`image`、`summary` 资源都需要在 `resources/` 下有稳定文件路径；数据库中的 `file_path/external_path` 用于建立 UI 资源与文件之间的映射。
- 创建 `InspirationSpace` 时，OpenFocus 必须把用户填写的 title 与 first note 自动写成一个 Markdown 初始资源文件放入 `resources/`，并同步到 Resources 列表。
- terminal 模式启动 remote terminal 时，`cwd` 固定为 `workspace_path`；terminal session 生命周期跟随 `InspirationSpace`，Companion 重启导致 session 丢失是可接受的，但 workspace 与资源文件不能丢。
- terminal 模式的 Remote Terminal 是主工作区：若 Companion 在线且当前没有 terminal session，页面应自动创建一个默认 terminal；用户也可在 terminal 窗口内用 `+` 创建新的 terminal tab。创建失败必须返回可读的 4xx/5xx JSON 错误，不得让前端只显示 `Internal Server Error`。
- terminal 复用现有 Companion gRPC terminal 能力与 WebSocket/ttyd 嵌入能力；实现上可以扩展 `RemoteTerminalSession` 的 owner 字段，或新增 Inspiration 专用关联表，避免伪造 Task/AgentSpace。
- terminal 输入注入必须走现有 terminal input 审计链路，并记录 `kind=inspiration.terminal_prompt_injected` 或等价事件。

### 8.1.2 Bring Your Own Agent 桥接协议

- terminal 模式的桥接资源固定为 `resources/draft_summary.md`，在 UI 中显示为名称固定的 `Summary`。
- remote terminal `Prompt Zone` 区的 `Summary` 按钮直接向当前 terminal 注入一段单行 prompt 文本，但不自动发送 Enter，不需要 preview sheet 或二次确认。该 prompt 要求外部 agent：
  - 阅读当前 workspace 与 `resources/`。
  - 与用户在 terminal 内继续澄清（如需要）。
  - 创建或更新 `resources/draft_summary.md`。
  - 使用固定 Markdown 结构：一级标题是 goal title，一级标题下方是 goal content；每个 task 使用一个二级标题，二级标题下方是 task content。
- OpenFocus 通过资源栏 `Add Resource` 下方的 `Sync Resource`、保存后自动轮询，或 terminal 输出事件触发轻量扫描，扫描 workspace 的 `resources/` 目录并刷新 Resources 栏；其中 `resources/draft_summary.md` upsert 为 `InspirationResource(type=summary, name=Summary, source=terminal_agent)`，其他文件按文件类型同步为普通资源。
- terminal 模式的 Resources `Send` 按钮必须把资源内容注入当前 remote terminal 的输入区，而不是写入内置消息 composer。
- terminal 模式的结构化草案生成先让用户在 `Create Goal` 输入框中手动输入或从下拉栏选择一个 resource 文件，再调用 OpenFocus 的结构化草案生成能力生成 `Draft vN`。这一步不要求 terminal agent 输出 JSON，也不信任 terminal agent 直接创建 Goal/Task；UI 上它呈现为 Draft/Publish 确认卡片，不恢复内置 agent 对话区，也不在 terminal header 中提供额外按钮。
- 若 terminal agent 写出的 summary 缺少必要 section，UI 不阻塞同步，但在生成 draft 前提示缺失项，并让内建 Agent 在草案中补充 `open_questions`。
- 正式 `Publish` 成功后，OpenFocus 基于最终 `Draft vN`、用户勾选的 tasks 与 deferred items 生成新的只读 `Published Summary`。`Summary` 保留为输入资源，不被覆盖，也不等同于发布归档。

### 8.2 产出要求（结构化）

- LLM 输出必须为结构化草案：
  - `goal_title`
  - `goal_description`
  - `tasks[]`（标题、描述、优先级建议、估时、依赖、Definition of Done）
  - `open_questions`
  - `rejected_or_deferred_ideas`
- 每次草案生成都必须保存历史版本。built-in 模式在消息流中以 `Draft vN` 的 assistant 卡片形式展示；terminal 模式在 terminal 工作台下方以独立 Draft/Publish 卡片展示，不显示普通内置 agent 消息流。
- 发布确认区仅负责“勾选要发布的 tasks 并确认发布”；若用户想改 goal/task 内容，需要继续对话，让 Agent 重新生成新草案。
- tasks 在发布确认卡片中默认全选；用户未勾选的 tasks 视为本次 `deferred`，需要写入发布结果与 `Published Summary`。
- 正式发布固定创建 `1 个 Goal + 用户勾选的多个 Tasks`，并为 Goal/Task 保留指向来源 `InspirationSpace` 与 `InspirationDraft` 的回链。

### 8.3 Inspiration 资源与总结规则

- `InspirationSpace` 支持 `url | image | text | summary` 四类资源。
- V1 中，`url` 资源只保存链接与名称；`image` 资源仅支持本地上传；资源侧栏支持 `Send to prompt`、`Preview`、`Rename`、`Delete`。
- 所有用户可见资源都必须落到 workspace 的 `resources/` 目录：文本写为 `.md/.txt`，URL 写为包含名称与链接的 `.url.md`，图片复制到资源目录，summary 写为 `.md`。
- `Send to prompt` 必须插入结构化引用，而不是直接拼接资源全文。
- `published` 时必须生成一个只读的 `Published Summary` 资源，内容包含：`Idea`、`Why now`、`Goal`、`Published tasks`、`Open questions`、`Rejected / deferred ideas`。terminal 模式下的 `Summary` 只是发布输入，不能替代 `Published Summary`。
- 阶段总结按“每 10 轮用户+assistant 往返或 1 小时”触发，但 V1 中默认只进入内部 memory 管线，不作为用户可见资源展示。

### 8.4 Inspiration 状态与 Fork 规则

- `open`：可对话、可管理资源、可生成标题建议与草案。
- `closed`：暂停/封存，只读；可在详情页 `Reopen`，且若未发布则允许删除。
- `published`：永久只读；若要基于该结果继续思考，必须通过 `Fork New Inspiration` 创建新的 `InspirationSpace`。
- Fork 时默认继承上一版的 `Published Summary` 资源，并可选择附带部分原始资源；新空间默认带 follow-up 风格标题。

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

- Next Move Agent 必须读取 Goal/Task 属性、近期事件、daily/long-term memory，以及历史推荐反馈。
- 更复杂的 Review verdict、返工任务自动生成、质量评分机制留待后续版本；但推荐纠偏学习闭环属于本次方案范围。

---

## 11. 记忆系统与回顾能力

### 11.1 Audit Memory

- Audit Memory 是 OpenFocus 的原始审计层，默认覆盖所有用户与 Agent 的关键行为。
- 必须纳入的事件类型包括：
  - Goal / Task 的创建、编辑、`Finish`、删除
  - Inspiration 的用户输入、资源操作、Agent 回复、`Draft vN`、`Publish`、`Fork`、`Reopen`
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

### 13.2 Milestone 2：Inspiration 规划与下一步推荐（2-3 周）

- Inspiration（先讨论/孵化再创建）→ 支持内建规划 Agent 与 BYO Agent terminal 两种模式；terminal 模式通过 `Summary` 桥接后由内建 LLM 生成 `Draft vN`，发布前只写草案与资源，不写 Goal/Tasks
- `GET /api/recommendations/next` 返回下一步推荐（带 why）

验收：Inspiration 的 `Publish` 后能创建 Goal/Tasks，并生成只读 `Published Summary`；terminal 模式能在独立 workspace 下直接注入 prompt、生成并同步 `resources/draft_summary.md`；Dashboard 能显示“下一步建议”。

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
- 灵感规划与任务拆解：由 `LLM` 负责通过 Inspiration 生成草案、标题建议与任务步骤。
- token 统计：MVP 仅从模型 API 返回获取（不兼容黑盒）。
- 推荐粒度：直接推荐用户“下一步要做的事情”（可执行 next action）。
- UI labels、user-facing copy、code comments 使用英文；系统文档可使用中文。
- `New Goal` 的 `Auto` 为智能提炼 `Title` 的能力，而不是简单首行回填。
- 记忆系统采用三层结构：`audit memory`（7 天 rolling）、`daily memory`（`YYYY-MM-dd.md`）、`long-term memory`（`MEMORY.md`）。
