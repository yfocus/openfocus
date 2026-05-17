<!-- SPDX-License-Identifier: Apache-2.0 -->
# Inspiration Planner（灵感规划）

## 触发点

- 用户创建 `InspirationSpace` 时选择默认模式：`Built-in Planner` 或 `Bring Your Own Agent`。
- `Built-in Planner` 模式下，内建规划 Agent 通过持续追问、资源引用与上下文澄清来帮助用户收敛目标。
- `Bring Your Own Agent` 模式下，OpenFocus 启动 remote terminal，用户在 terminal 中运行自己偏好的 agent；此时详情页主工作区完全切换为 terminal，不再展示内置 agent 对话流、消息输入框、terminal header 草案按钮或 `Suggest Titles` / `Generate Draft` 等内置交互入口，也不展示 AgentSpace 的 prompt auto controls。OpenFocus 不解析 terminal 对话语义，只通过 `resources/draft_summary.md` 与草案/发布链路桥接。
- terminal agent 是“不受信协作者”：它可以在 workspace 中产出文件，但不能直接写入 Goal/Task，不能绕过 OpenFocus 的草案生成、用户确认与发布链路。
- built-in 模式下，当用户显式触发 `/draft_goal_tasks` 或内建 Agent 判断上下文已经足够完整时，可生成新的 `Draft vN`；terminal 模式下通过 `Prompt Zone` 的 `Create Goal` 选择一个已同步 resource，并基于该 resource 生成 `Draft vN`。
- 在用户点击 `Publish` 之前，不写入任何 Goal/Tasks；所有结构化结果都先保存为草案。
- `New Goal` 对话框只负责立即创建目标；需要规划或灵感孵化时，统一进入 `Inspiration` 模块。

## Inspiration Workspace 与 Terminal Runtime

- 每个 `InspirationSpace` 创建时必须分配独立 `workspace_path`，建议目录形态为 `.data/inspirations/{space_id}/`。
- workspace 初始化时必须创建：
  - `resources/`：用户资源、terminal agent 产物与系统 summary 的文件目录。
- `url`、`text`、`image`、`summary` 资源都需要在 `resources/` 下有稳定文件路径；数据库中的 `file_path/external_path` 用于建立 UI 资源与文件之间的映射。
- 创建 `InspirationSpace` 时，OpenFocus 必须把用户填写的 title 与 first note 自动写成一个 Markdown 初始资源文件放入 `resources/`，并同步到 Resources 列表。
- terminal 模式启动 remote terminal 时，`cwd` 固定为 `workspace_path`；terminal session 生命周期跟随 `InspirationSpace`，Companion 重启导致 session 丢失是可接受的，但 workspace 与资源文件不能丢。
- terminal 模式的 Remote Terminal 是主工作区：若 Companion 在线且当前没有 terminal session，页面应自动创建一个默认 terminal；用户也可在 terminal 窗口内用 `+` 创建新的 terminal tab。创建失败必须返回可读的 4xx/5xx JSON 错误，不得让前端只显示 `Internal Server Error`。
- terminal 复用现有 Companion gRPC terminal 能力与 WebSocket/ttyd 嵌入能力；实现上可以扩展 `RemoteTerminalSession` 的 owner 字段，或新增 Inspiration 专用关联表，避免伪造 Task/AgentSpace。
- terminal 输入注入必须走现有 terminal input 审计链路，并记录 `kind=inspiration.terminal_prompt_injected` 或等价事件。

## Bring Your Own Agent 桥接协议

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

## 产出要求（结构化）

- LLM 输出必须为结构化草案：
  - `goal_title`
  - `goal_content`（兼容旧草案字段 `goal_description`，发布时写入 `Goal.content`）
  - `tasks[]`（每项包含 `title` 与 `content`；兼容旧草案字段 `description`，发布时写入 `Task.content`；可附带优先级建议、估时、依赖、Definition of Done）
  - `open_questions`
  - `rejected_or_deferred_ideas`
- 每次草案生成都必须保存历史版本。built-in 模式在消息流中以 `Draft vN` 的 assistant 卡片形式展示；terminal 模式在 terminal 工作台下方以独立 Draft/Publish 卡片展示，不显示普通内置 agent 消息流。
- 发布确认区仅负责“勾选要发布的 tasks 并确认发布”；若用户想改 goal/task 内容，需要继续对话，让 Agent 重新生成新草案。
- tasks 在发布确认卡片中默认全选；用户未勾选的 tasks 视为本次 `deferred`，需要写入发布结果与 `Published Summary`。
- 正式发布固定创建 `1 个 Goal + 用户勾选的多个 Tasks`，并为 Goal/Task 保留指向来源 `InspirationSpace` 与 `InspirationDraft` 的回链；发布创建 Goal/Task 时必须写入 `goal.created` / `task.created` 事件。

## Inspiration 资源与总结规则

- `InspirationSpace` 支持 `url | image | text | summary` 四类资源。
- V1 中，`url` 资源只保存链接与名称；`image` 资源仅支持本地上传；资源侧栏支持 `Send to prompt`、`Preview`、`Rename`、`Delete`。
- 所有用户可见资源都必须落到 workspace 的 `resources/` 目录：文本写为 `.md/.txt`，URL 写为包含名称与链接的 `.url.md`，图片复制到资源目录，summary 写为 `.md`。
- `Send to prompt` 必须插入结构化引用，而不是直接拼接资源全文。
- `published` 时必须生成一个只读的 `Published Summary` 资源，内容包含：`Idea`、`Why now`、`Goal`、`Published tasks`、`Open questions`、`Rejected / deferred ideas`。terminal 模式下的 `Summary` 只是发布输入，不能替代 `Published Summary`。
- 阶段总结按“每 10 轮用户+assistant 往返或 1 小时”触发，但 V1 中默认只进入内部 memory 管线，不作为用户可见资源展示。

## Inspiration 状态与 Fork 规则

- `open`：可对话、可管理资源、可生成标题建议与草案。
- `closed`：暂停/封存，只读；可在详情页 `Reopen`，且若未发布则允许删除。
- `published`：永久只读；若要基于该结果继续思考，必须通过 `Fork New Inspiration` 创建新的 `InspirationSpace`。
- Fork 时默认继承上一版的 `Published Summary` 资源，并可选择附带部分原始资源；新空间默认带 follow-up 风格标题。
