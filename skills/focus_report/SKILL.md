<!-- SPDX-License-Identifier: Apache-2.0 -->
---
name: focus_report
description: Report task execution results (name/prompt/response/status) from external agents back to OpenFocus, and let OpenFocus persist it as events and auto-mark tasks done when appropriate.
metadata:
  {
    "openclaw": {
      "requires": {"bins": ["python3"]}
    }
  }
---

# focus_report

该 skill 用于让外部 Agent（如 coco/trae/claude-code/codex/openclaw）把任务执行情况上报给 OpenFocus。

OpenFocus 服务端接收后会：

- 把每次上报持久化为一条 `events` 记录（`kind=skill.focus_report`）

## 服务端依赖

OpenFocus 需要运行在本地，并暴露接口：

- `POST /api/skills/focus_report`

## 环境变量

- `OPENFOCUS_SERVER`：OpenFocus 服务地址，默认 `http://127.0.0.1:8001`
- `OPENFOCUS_AGENT`：上报方标识，默认 `external`

## 用法

基础用法：

```bash
python3 {baseDir}/scripts/focus_report.py \
  --task-name "Implement focus_report" \
  --status succeeded \
  --user-prompt "..." \
  --assistant-response "..."
```

推荐用法（带 task_public_id，便于 OpenFocus 精确勾选）：

```bash
python3 {baseDir}/scripts/focus_report.py \
  --server http://127.0.0.1:8001 \
  --agent coco \
  --goal-id 3 \
  --task-public-id e12e08ce-0606-4af0-90ed-ffc8d4f866e5 \
  --task-name "skill测试task" \
  --status succeeded \
  --user-prompt "do it" \
  --assistant-response "done"
```

## 上报字段

- `task_name`：任务名称（必填）
- `status`：任务状态（必填），如 `running/succeeded/failed/canceled`
- `user_prompt`：用户输入（可选）
- `assistant_response`：Agent 输出（可选）
- `goal_id`：关联 goal（可选）
- `task_public_id`：关联 task（可选，强烈建议）
