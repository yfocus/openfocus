<!-- SPDX-License-Identifier: Apache-2.0 -->
# OpenFocus 开发流程（给 Agent/开发者）

本文件规定在本仓库开发 feature/fix 的默认流程与质量门槛。

## 0. 项目目标

- `spec/README.md` 是规范入口；`spec/architecture.md`、`spec/product-requirements.md` 及专题 spec 文件共同定义项目的完整设计，是 source of truth。
- 开发前必须先阅读 `spec/README.md` 及相关专题 spec，确认当前要实现的目标与范围边界。
- 若实现与 spec 不一致，优先与用户确认并修改 spec，更新 spec 后再进行开发。

## 1. 代码组织与抽象
- 模块要放置在独立的目录下，模块根目录下要放置一个SPEC文件定义模块行为。
- 保持模块边界清晰：
  - Web/API：`openfocus/main.py`
  - 数据模型：`openfocus/models.py`
  - DB/连接：`openfocus/db.py`
  - Agents：`openfocus/agent/`（core/llm/tools/storage/agents）
  - Skills：`skills/<skill_name>/`（必须包含 `SKILL.md` + `scripts/`）
- 做“适当抽象”：
  - 可复用的协议/循环放到 `openfocus/agent/core/`
  - 与具体业务无关的能力（例如 LLM provider、工具注册）不要塞进业务 agent 文件
  - 抽象必须服务于可测试性与可扩展性，避免过度工程

## 2. 测试要求（强制）

- 每个 feature 和 bug fix 必须新增/更新对应单元测试。
- 单元测试应覆盖：
  - Web 路由（Goals/Tasks/Plan）
  - REST API（`/api/agent/events`、`/api/skills/focus_report`）
  - 关键工具（如 `list_goals/describe_goal`）
- 测试隔离：
  - 测试必须使用独立 SQLite（通过 `OPENFOCUS_DB_PATH`），不得污染本地 `.data/openfocus.db`

运行：

- `poetry run pytest`

## 3. 端到端自测（强烈建议）

- 每次开发完成后，至少做一次最小 E2E：
  - `make serve`
  - 浏览器打开 `/goals`，创建一个 goal + task
  - 用 `skills/focus_report/scripts/focus_report.py` 上报 `status=succeeded`
  - 确认 task 在 UI/DB 中被标记为 `done`

## 4. 变更后清理（强制）

- 每次提交前都要清理 “与本地改动相关的” 不再使用的代码片段：
  - 未使用的 import、死代码、重复实现、遗留调试输出
- 删除/移动文件需确保：
  - 测试全绿
  - `spec/` 下规范文档与实现一致

## 5. Skill 规范

- 每个 skill 必须是目录：`skills/<skill_name>/`
- 必须包含：
  - `SKILL.md`（包含 YAML front matter：`name/description/metadata`）
  - `scripts/`（可执行脚本，示例命令使用 `{baseDir}` 占位符）
