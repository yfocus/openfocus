## 用户需求
AI 时代的个人工作模式正在从“亲自做完”切换为“提出目标 + 组织多个 Agent 执行 + 作为 Reviewer 做判断”。在该模式下，瓶颈不再是单次执行能力，
而是**人的注意力带宽与上下文切换成本**：

- 人脑难以同时并行管理超过3个任务，很容易在频繁的上下文切换中耗尽心力。
- 用户同时推进多个目标与任务，容易陷入“下一步做什么”的决策疲劳。
- 多个 Agent 并行产出后，用户需要快速定位应优先 review 哪些结果、如何推动目标链继续向前。
- 当用户并行推进多任务时，需要能方便的“量化产出”，采用 Token消耗 或 Agent时 来量化吞吐。

## User Story

### Language Policy

- System specs may stay in Chinese.
- User-facing labels, buttons, navigation items, status text, and interaction copy must use English.
- Code implementation, comments, and API naming must use English.

### Dashboard

**概述**
Dashboard is the main workspace. It shows a goal list on the left, goal/task detail in the middle, and `What's Next` plus `Recent Events` on the right.

**Dashboard URI（路由）**
- `GET /goals`：Dashboard（左侧目标列表 + 中间详情 + 右侧辅助栏）。
- `GET /goals?goal={goal_id}`：在 Dashboard 中打开该目标详情（中间栏）。
- `GET /goals?task={task_public_id}`：在 Dashboard 中打开该任务详情（中间栏；左侧自动选中所属目标）。
- `GET /goals?new=1`：在 Dashboard 中直接打开 New Goal 对话框。

```mermaid
flowchart TD
  A["/goals"] -->|点击 Goal| B["/goals?goal={goal_id}"]
  A -->|点击 Task| C["/goals?task={task_public_id}"]
  A -->|New Goal| D["/goals?new=1"]
  C -->|查看目标| B
```

**视觉与交互**
1. 左侧栏只展示目标列表（用于扫视与选择目标），不在目标卡片下展开/展示 task。
   - Task 的查看与选择统一在中间详情里完成（例如在目标详情的 Tasks 表格中点击进入任务详情）。
2. Information density follows the current implementation:
   - Left goal cards show a short summary, a done/total ratio, and a status dot.
   - Goal detail shows original content, DDL, elapsed time, related tasks, and related events.
   - Task detail shows original title/content, DDL, elapsed time, stable `taskId`, and related events.
   - Task status must remain visually distinguishable (`todo` / `in progress` / `done`).
3. Dashboard 采用纵向**三栏布局**：左侧「目标列表」+ 中间「详情」+ 右侧「辅助栏」。
   - 参考 ChatGPT/OpenAI 的页面体验：Dashboard 主体容器不滚动（或尽量不出现整页滚动条），滚动发生在各自栏内。
   - 左/中/右三栏必须**独立滚动**：滚动左侧不影响中/右，滚动中间不影响左/右。
   - 右侧辅助栏再分上下两栏：上栏是 What's Next，下栏是 Recent Events（事件流）。
   - 可用性强约束：任何一栏内容超过视口高度时，必须能在该栏内滚动（不能出现“三栏都无法滚动”的状态）。
   - 视觉强约束：滚动条样式需与整体 terminal/neon 风格统一（避免默认系统滚动条突兀）。
4. 键盘可用性（强约束）：Dashboard **整个界面**支持键盘方向键切换焦点移动。
  - `↑/↓`：在 Dashboard 内可见的可交互元素之间移动焦点（包括左侧目标/任务、中间详情按钮、右侧 What's Next / Recent Events 等）。
  - `←/→`：在左/中/右三栏之间移动焦点（优先选择与当前焦点纵向位置最接近的元素）。
  - 当焦点落在左侧目标/任务项上时，需同步切换中间详情。
5. 左侧栏只负责导航与状态概览：**不允许出现 Plan / Edit 等操作按钮**，所有编辑/规划/删除等操作统一放到中间详情页。
6. 顶部导航栏提供一个明显的 `New Goal` 按钮（弹窗/对话框均可），用于快速创建目标。
   - 视觉强约束：`New Goal` 必须与 `Dashboard/Memory/Companion` 同一套导航按钮样式。
   - 交互强约束（New Goal 形态升级）：
     - 对话框包含：`Title`（goal content, <=2000）+ `Content`（详细描述, 必填）+ `DDL`（必填）。
     - `Auto` 开关必须放在 `Title` 输入框右侧：
       - `Auto=ON`：`Title` 不允许输入（只读），并显示 `Auto generate from content`。
       - On `Save`, the system must use LLM-based smart extraction from `Content` to produce `Title`.
       - `Auto=OFF`：`Title` 才允许输入。
       - 必须移除 `gen` 按钮（不再通过按钮生成 Title）。
     - `Plan Mode` 行为（交互强约束）：
       - 对话框底部提供 `Plan Mode` 开关。
       - `Plan Mode=ON`：提交按钮文案显示为 `Plan`；点击后进入 Plan 流程，不在此处直接创建 goal（由 Plan Mode 的 `Create` 写库）。
       - `Plan Mode=OFF`：提交按钮文案显示为 `Save`；点击后立即创建 goal。
       - 顶部导航栏不提供单独的 Plan Mode 按钮。
7. 「摘要」规则（强约束）：
  - Goal/Task 创建时：若原文长度超过 20 个字符，必须生成一个**不超过 20 个字符**的短摘要；否则摘要=原文。
  - Dashboard 左侧栏只展示摘要（节省空间，便于扫视）；原文只在中间详情栏展示。
  - 摘要用于展示，不改变原文内容。
8. Recent Events：
  - 事件按“越近越靠前”排序。
  - 事件项若关联 task，应支持一键跳转到该 task 的详情。
  - 事件数据必须来自真实 `events` 落库（不是前端假数据）。
  - 只对“能被系统识别到的有效 taskId（Task.public_id）”提供“打开”按钮，避免出现能点但打不开的事件。
   - 文案强约束：事件展示必须面向人阅读且使用英文：
     - 显示 `Source: Web` 或 `Source: Agent (name)`；不得出现 `agent: ui` 这类调试表达。
     - 事件类型使用 human-readable English labels（例如 `Result Report` / `Progress Report` / `Manual Finish` / `Reopen`），避免直接暴露内部 kind。
     - 状态需可读化（例如把 `status=succeeded` 展示为 `Completed (pending confirmation)`）。
9. 选中态视觉（强约束）：被选中的 goal/task **整张卡片框**必须高亮（不仅是文字/按钮高亮）。
10. 左侧信息密度（强约束）：
   - 左侧列表以“扫视”为主，状态/耗时等信息不得挤占摘要主信息区（避免出现「待启动/已进行」大段占位）。
   - 状态可以用小图标/颜色点位表达；详细状态/耗时在中间详情栏展开。
11. 完成确认（强约束）：
   - 外部 Agent/Skill 的“完成上报”（例如 `POST /api/agent/events` 的 `task.completed` 或 `POST /api/skills/focus_report` 的 `status=succeeded`）**不等于任务真实完成**。
   - 上报只会生成事件（用于历史/推荐/复盘），**不得自动把 Task 标记为 done**。
   - Task 是否完成必须由人确认：在 Task detail 里提供 `Finish` 按钮。
   - 已完成任务在 Task detail 里提供 `Reopen` 按钮，用于恢复到未完成状态。
   - 「任务详情」需要展示与该 task 相关的事件列表（最近若干条），用于用户复核 Agent 产出与上报内容。
12. 文案约束：避免出现调试风格的长句与键值对（例如 `status=... priority=... importance=... created ... ago`），页面只保留对用户有用的信息密度与更简洁的表达。

13. 新建 Task（交互强约束）：
   - 新建 Task 不允许在左侧 GOALS/TASKS 栏内直接输入创建；必须与 New Goal 一样通过弹窗/对话框创建。
   - 对话框包含「任务标题」与「详细描述」两个输入框，且**两个都必填**。
   - 点击保存后立即落库。

14. 目标详情编辑：
   - 目标详情需展示目标与详细描述原文。
   - 支持在目标详情内编辑（不依赖跳转到单独编辑页）。

15. 顶端导航栏提供Companion选项，点击后跳转到Companion管理页。
    - Companion page shows current registered Companion basics: `name/device_id`, related AgentSpace list, created date, and status.
    - Companion statuses in the current product are: `active`, `offline`, and `waiting for pairing` (backend state key: `pending_certification`).
    - For waiting companions, the page shows an inline pairing code input plus `Pair` button inside each card.
    - Pairing code contains 10 letters or digits; each minute allows up to 10 attempts.


### Task's Agent Space

**概述**
AgentSpace is the task workspace. It binds one task to one workdir on one Companion. The current implementation stores `agent_type=trae-cli` and does not expose agent type selection in the create dialog.

**视觉与交互**
1. In Task detail, clicking `Create Space` opens a dialog to choose a workdir and a Companion.
   After AgentSpace creation succeeds, the page auto-jumps to AgentSpace. For tasks that already have a workspace, clicking `Space` opens AgentSpace.
   - Task 详情页的 `Create Space/Space` 左侧提供一个 `Goal` 按钮，用于跳回该 task 所属 goal。
2. AgentSpace uses the current three-column layout: `FILES` + `PREVIEW` + `TERMINAL`.
   - `FILES` is a read-only tree.
   - `PREVIEW` is read-only preview for code / markdown / image files.
   - `TERMINAL` is the remote terminal area.
3. 远程终端支持选项卡：
   - 点击右侧终端区的 `+` 创建一个新的终端 tab（每个 tab 对应一个独立的 PTY/session）。
   - 点击 tab 右上角的 `x` 关闭该终端（关闭后该 session 不再保留）。
   - 若该 AgentSpace 下没有任何终端且 Companion 在线，页面应自动创建一个默认终端。
4. 终端交互逻辑：
   - 用户在终端中输入命令，输入/输出以流式方式实时回显。
   - 终端默认以 AgentSpace 的工作目录作为启动目录（cwd=root_path）。
   - 终端 session 在 AgentSpace 生命周期内保留；若 Companion 重启/崩溃，允许终端 session 丢失。
   - Terminal area includes a `Prompt Zone` with `Agent Mode` toggle in the current implementation; it is not a separate Agent chat tab.
5. 释放工作区：
   - 点击“释放工作区”会释放该 AgentSpace，并清理该空间下的所有远程终端（以及 OpenFocus 侧的终端记录）。
   - 若 Companion 离线，允许清理仅发生在 OpenFocus 侧（终端可能在 Companion 上残留，但不影响 OpenFocus 侧继续使用）。
6. 使用Companion机制实现AgentSpace。

待定
1. 点击tui中的文件路径&行号能在FILES和PREVIEW里头跳转。

### PlanMode

**概述**
Plan Mode 用于“先规划再创建”：在用户点击 `Create` 之前，不写入 Goal/Tasks。

**视觉与交互**
1. Plan Mode 的入口在 `New Goal` 对话框：打开 `Plan Mode` 开关后，点击 `Plan` 不创建 goal，直接进入 Plan 流程。
   - 不在左侧列表上放按钮。
   - 顶部导航栏不提供 Plan Mode 按钮。
2. 页面流转（当前实现）：`Input → Planning → Plan Ready → Reply/Retry/Create`。
   - Input：从 `New Goal` 对话框进入 plan session。
   - Planning：等待 agent 输出期间必须展示明确的进行中状态，并锁定发送按钮/输入框，避免重复点击与误操作。
   - Plan Ready：agent 给出可执行 steps 后，进入“人类确认”区。
     - User can continue sending more requirements in the same session.
     - User can select which generated tasks to create.
     - `Retry` starts a new plan session with the same draft.
     - `Create` writes Goal/Tasks and returns to Dashboard.
   - 强约束：在 `Create` 之前不得写入任何 task（人类在环）。
3. 对话的实现参考 ChatGPT/豆包：等待大模型时必须有提示，且不能继续发送/编辑输入内容（避免“无提示 + 可重复点击”这种反人类交互）。
   - 发送快捷键（强约束）：当输入框聚焦时，macOS 使用 `Cmd+Enter` 发送；其他平台使用 `Ctrl+Enter` 发送；单独 `Enter` 保持换行。

### Calendar

**概述**
Calendar 用于按“月”查看完成记录（task.confirmed_done）与目标时间线（goal created → due）。

**视觉与交互**
1. 顶部导航栏在 `Companion` 后追加 `Calendar` 按钮，点击后弹出日历对话框（不跳转页面）。
2. Calendar 提供两种 `by month` 视图：
   - `Month`：常规矩形月历；每一天展示“当天完成的任务数”，点击某一天在下方列出当天完成的任务，可点击任务跳转到 `/goals?task={task_public_id}`。
   - `Swimlane`：泳道图（横轴=该月日期，每行=一个 goal 的时间区间 created_at → due_date），点击 goal 打开该 goal 下所有 tasks 列表；支持 `Back` 返回泳道图。

### What's Next

**概述**
`What's Next` 升级为一个 `Next Move Agent`。它不是简单的单条规则推荐，而是一个会读取全量上下文的推荐 Agent：可获取用户所有 `goals`、`tasks`、`events`、当日 `daily memory`、`MEMORY.md`、显式用户偏好，以及用户对历史推荐的反馈，并基于这些信息给出下一步建议。

**视觉与交互**
1. 每当目标或任务的状态发生变化、时间过去了30分钟或用户主动进行刷新，就要进行一次分析。
2. 推荐结果展示在 Dashboard 右侧上栏（`What's Next`），展示形态升级为 **3 个 task recommendation cards**。
   - 每张卡至少包含：Task 标题、所属 Goal、预计耗时、任务类型、推荐理由、`Open` 按钮、`Not for now` 按钮。
   - 推荐理由必须面向人类可读，避免暴露调试字段或原始打分细节。
3. Next Move Agent 的输入必须覆盖以下信号：
   - 所有进行中/未完成的 Goals 与 Tasks。
   - 与这些任务相关的 Events（用于判断最近是否刚推进过、是否存在连续上下文）。
   - 用户偏好与记忆（来自 `daily memory` 与 `MEMORY.md`）。
   - 用户对历史推荐的显式反馈（尤其是“不喜欢/不适合现在”的理由）。
4. Next Move Agent 的推荐逻辑必须综合考虑：
   - 任务类型（例如深度思考型、沟通协调型、机械执行型、Review 型）。
   - 预计耗时（短平快任务 vs. 需要大块专注时间的任务）。
   - 上下文切换成本（是否延续当前正在推进的目标、主题、工作区、文件上下文）。
   - 任务重要度、优先级、DDL 风险。
   - 用户长期偏好（例如偏好快速反馈、连续推进同类任务、避免碎片切换等）。
5. What’s Next **一次返回 3 个推荐 task**（强约束）：
   - 不是 1 个，也不是无限列表。
   - 3 个推荐之间需要有排序，但不要求强行完全不同；Agent 可以在“延续当前上下文”与“覆盖不同工作模式”之间做平衡。
6. 用户可以对任意一个推荐点击 `Not for now` 并填写理由。
   - 理由既支持预设选项，也支持补充自由文本。
   - Agent 必须对这些反馈进行总结学习，并在后续推荐中主动规避相同问题。
7. 学习结果不能只停留在当次交互内。
   - 反馈结论必须沉淀为可复用的偏好/反偏好信号，供后续 Next Move 推荐持续读取。
8. 强约束：推荐结果中不得出现已删除、已完成、已归档、无效打开的 task。
9. 强约束：如果当前上下文不足以给出高质量推荐，Agent 也应返回 3 个候选，但要在理由中明确表达不确定性，而不是返回空白区域。


### Memory

**概述**
Memory uses a three-layer markdown system: `audit memory`, `daily memory`, and `long-term memory`.

**视觉与交互**
1. Memory 页面展示三个主视图：`Audit`、`Daily` 与 `Long-term`。
   - `Audit` shows rolling audit files grouped by time window.
   - `Daily` shows the current `YYYY-MM-dd.md` file or a selected historical daily file.
   - `Long-term` shows `MEMORY.md`.
   - The page layout, typography, and color system should align with the Dashboard style baseline.
2. Audit memory must be visible in the Memory page in MVP.
   - The UI may present audit logs by file list, time range, or rolling segments.
   - The UI does not need to flatten all audit files into one infinite stream.
3. Audit memory must include all key behaviors from users and agents inside OpenFocus:
   - Goal / Task create, edit, `Finish`, delete
   - Plan Mode interaction history
   - Agent / Skill reports
   - All web shell inputs and outputs in AgentSpace
4. Audit memory rotates automatically when either threshold is reached:
   - `1 hour`
   - `2000 entries`
5. Every audit rotation triggers a summary job.
   - The generated summary is appended into that day's `daily memory`.
   - After each summary finishes, the system must immediately roll a brand new audit file for subsequent logs.
   - The Audit tab must provide a `Summary` button so the user can manually trigger the same summary-and-roll flow.
   - Audit files that already produced a summary must be visually marked in the file list so they are distinguishable from files that have not been summarized.
   - Audit memory files keep a `7 days` TTL.
6. After `00:00`, the system starts a daily finalization job for the previous day.
   - It reads the whole previous day's daily memory draft.
   - It writes back the finalized version to the same `YYYY-MM-dd.md` file.
   - It extracts stable user preferences and facts into `MEMORY.md`.
7. `daily memory` and `MEMORY.md` are permanent files and have no TTL.
8. Recommendation and planning may read both the latest daily memory and `MEMORY.md`, but the UI must keep user-facing text in English.
9. `Long-term` is read-only by default.
   - The primary action shows `Edit` by default.
   - Only after clicking `Edit` does the text area become editable and the action switch to `Save`.
