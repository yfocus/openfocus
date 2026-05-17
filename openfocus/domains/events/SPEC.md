<!-- SPDX-License-Identifier: Apache-2.0 -->
# Events Domain

`events` owns the user-facing journal and audit event write path.

## Responsibilities

- Persist structured journal rows for `/api/agent/events`, `focus_report`, UI
  actions, runtime signal projections, and system events.
- Provide recent event and calendar projections for product surfaces.
- Mirror important event writes to audit memory when requested.
- Derive Attention Inbox recommendations only for explicit recommendation
  events such as Next Move.

## Boundaries

- External agent reports are journal-only. They must not mutate business
  `Task.status`.
- External agent reports must not mutate `agent_turns`,
  `agent_runtime_sessions`, or `task_agent_activity`.
- Current runtime state belongs to `openfocus/domains/agent_activity/`.
- Human confirmation events such as `task.confirmed_done` are business events;
  they may change task status through the goals/task service, not by generic
  event ingestion.

## Compatibility

`POST /api/agent/events` keeps the existing body shape:

```json
{
  "kind": "task.progress",
  "agent": "codex",
  "task_id": "optional-task-public-id",
  "payload": {}
}
```

The endpoint may accept richer payloads over time, but its default effect remains
journal and audit persistence only.
