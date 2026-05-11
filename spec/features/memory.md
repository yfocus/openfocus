<!-- SPDX-License-Identifier: Apache-2.0 -->
# 记忆系统与回顾能力

## Audit Memory

- Audit Memory 是 OpenFocus 的原始审计层，默认覆盖所有用户与 Agent 的关键行为。
- 必须纳入的事件类型包括：
  - Goal / Task 的创建、编辑、`Finish`、删除
  - Inspiration 的用户输入、资源操作、Agent 回复、`Draft vN`、`Publish`、`Fork`、`Reopen`
  - `/api/agent/events`、`/api/skills/focus_report` 等 Agent/Skill 上报
  - AgentSpace web shell 的所有输入与返回值
- Audit Memory 以 rolling 文件形式保存，按 `1h` 或 `2000` 条记录切分，保留 `7 days`。

## Daily Memory

- 每个 audit 文件在轮转时，都会触发一次总结任务，把该分段摘要写入当天的 `daily memory`。
- `daily memory` 文件名固定为 `YYYY-MM-dd.md`，同一天内允许多次追加阶段性总结。
- 每天 `00:00` 之后，系统必须为前一天启动一次日终总结任务，生成当天 daily 记忆的最终版本。

## Long-term Memory

- 日终总结完成后，系统从 finalized daily 记忆中提炼长期稳定信息，写入 `MEMORY.md`。
- 长期记忆只保留相对稳定的用户偏好、事实、长期约束，不直接拷贝瞬时任务噪音。
- `MEMORY.md` 永久保留，并参与后续推荐与规划。

## 当前回顾能力与延后项

- 已有回顾入口：Dashboard `Recent Events`、Task 详情 `Event`、`Calendar`、`Memory`。
- Memory 页面要求同时可查看 audit 日志、daily 记忆与 long-term memory；audit 可以按 rolling 文件组织呈现。
- 当前 Memory 已参与 Next Move 推荐与反馈学习；README 中提到的“RAG 召回后由用户选择是否拼接到 Agent 提示词”属于后续增强，当前实现尚未提供独立的记忆选择器/拼接确认 UI。
- 延后能力：独立的 `History & Metrics` 页面、吞吐量趋势图、基于事件/终端输出/资源文件的复盘视图。
