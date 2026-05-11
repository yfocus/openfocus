<!-- SPDX-License-Identifier: Apache-2.0 -->
# Next Move（Attention Scheduler）

## 输入信号（MVP 可获得）

- Goals：`due_date`、`priority`、`importance`、状态、所属主题
- Tasks：`status`、`task_type`、`estimated_minutes`、`context_key`、所属 Goal 的 DDL、最近是否有 `task.started/task.progress`
- Events：按 Task / Goal 聚合的近期 `events`，用于识别连续性、阻塞、切换成本与最近推进轨迹
- Memory：当日 `daily memory` 与 `MEMORY.md` 中沉淀的用户偏好、稳定事实、长期约束、长期工作模式
- Feedback：用户对历史推荐的拒绝理由、纠偏意见、已学习到的偏好/反偏好

## 输出形式

- `RecommendationSet`（最多 3 条；候选不足时允许少于 3 条）
  - `generated_at`
  - `trigger_kind`：例如 `state_changed | periodic_refresh | manual_refresh | feedback_submitted`
  - `items[]`
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

## Agent 处理流程

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
   - 最终返回最多 3 个 task。
   - 要避免结果完全重复同一模式；但允许为了降低切换成本，保留同一 goal 下的连续推进项。
5. **解释生成**
   - 每个推荐都要生成面向人类的简短理由，而不是暴露内部 score 明细。

## 反馈学习闭环

用户可以对任意推荐提交 `dismiss` 反馈，Next Move 必须形成闭环：

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

## 触发机制

- Goal / Task 状态变化时触发
- 用户提交推荐反馈时触发
- 距上次分析超过 `30 分钟` 时触发
- 用户手动点击刷新时触发

## 产品约束

- 每次最多返回 `3` 个推荐 task，不返回长列表；候选不足时允许少于 3 个。
- 推荐必须可解释，但 explanation 面向用户，不暴露原始内部规则表。
- 推荐系统必须读取 Memory 与 Feedback，不能只看 Goal.priority / due_date 之类的静态字段。
- 若信息不足但存在可执行 task，仍需返回候选并标记较低信心；没有可执行 task 时允许为空。
