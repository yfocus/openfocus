<!-- SPDX-License-Identifier: Apache-2.0 -->
# Roadmap and Decisions

## MVP 里程碑与验收

### Milestone 1：核心数据与页面（1-2 周）

- Goal/Task CRUD
- Dashboard 基础版（三栏布局 + Goal/Task 详情）
- SQLite 持久化

验收：可创建目标与任务，刷新页面不丢数据。

### Milestone 2：Inspiration 规划与下一步推荐（2-3 周）

- Inspiration（先讨论/孵化再创建）→ 支持内建规划 Agent 与 BYO Agent terminal 两种模式；terminal 模式通过 `Summary` 桥接后由内建 LLM 生成 `Draft vN`，发布前只写草案与资源，不写 Goal/Tasks
- `GET /api/recommendations/next` 返回下一步推荐（带 why）

验收：Inspiration 的 `Publish` 后能创建 Goal/Tasks，并生成只读 `Published Summary`；terminal 模式能在独立 workspace 下直接注入 prompt、生成并同步 `resources/draft_summary.md`；Dashboard 能显示“下一步建议”。

### Milestone 3：Agent 派发 + Skill 遥测 + Review（3-5 周）

- Companion 注册、配对、目录选择
- AgentSpace + Remote Terminal + Agent Session 基础能力
- `/api/agent/events` 与 `/api/skills/focus_report` 遥测接入；Task 是否完成由人确认（不自动标记 done）

验收：外部 Agent 能接入并回传事件；用户可在 Task 详情页手动确认完成/重新打开，并能在 AgentSpace 中使用远端终端。

### Milestone 4：Calendar & Memory（5-6 周）

- Calendar 月视图与 Swimlane
- 三层记忆系统：audit rolling / daily `YYYY-MM-dd.md` / long-term `MEMORY.md`

验收：系统能按阈值轮转 audit memory、生成 daily 记忆并在次日完成日终定稿，同时从中提炼长期记忆。

---

## 关键决策记录（已确认）

- 产品形态：`Web`，支持本地部署本地使用。
- 外部 Agent：面向 `Codex / Claude Code / OpenClaw / Coco / Trae` 等运行时。
- 灵感规划与任务拆解：由 `LLM` 负责通过 Inspiration 生成草案与任务步骤；Goal/Task 的 `title` 必须来自用户输入或草案显式字段，不允许在创建时再从 `content` 自动总结/提炼。
- token/Agent 时等吞吐量指标不是当前核心主线；如后续引入，也只作为辅助复盘指标。
- 推荐粒度：直接推荐用户“下一步要做的事情”（可执行 next action）。
- UI labels、user-facing copy、code comments 使用英文；系统文档可使用中文。
- `New Goal` 不提供 `Auto` 标题生成；用户必须显式填写 `Title` 与 `Content`。
- 记忆系统采用三层结构：`audit memory`（7 天 rolling）、`daily memory`（`YYYY-MM-dd.md`）、`long-term memory`（`MEMORY.md`）。
