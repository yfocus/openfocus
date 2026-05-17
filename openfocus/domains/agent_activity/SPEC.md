<!-- SPDX-License-Identifier: Apache-2.0 -->
# Agent Activity Domain

`agent_activity` owns OpenFocus runtime state for agent work. It is separate from
the generic `events` journal.

## Responsibilities

- Normalize runtime signals from Companion, hooks, Codex app-server notifications,
  Coco hooks, and OpenFocus-managed agent sessions.
- Maintain `agent_turns` as the lifecycle record for each user prompt / agent
  turn.
- Maintain `task_agent_activity` as the current read model used by the global
  floating ball.
- Provide `/api/agent_activity/summary` for R/W counts and cards.

## Rules

- `runtime.session.started` only marks a session as available; it must not create
  running task activity by itself.
- `runtime.turn.submitted` and `runtime.turn.started` create or resume a running
  turn after task correlation succeeds.
- Waiting states are explicit:
  - `runtime.turn.waiting_for_approval`
  - `runtime.turn.waiting_for_input`
  - `runtime.turn.waiting_for_confirmation`
- `runtime.turn.completed` means the agent turn ended and user review is needed;
  it does not mark the business task as done.
- `/api/agent/events` and `focus_report` are journal/reporting APIs. They must not
  mutate `agent_turns` or `task_agent_activity`.

## Floating Ball Buckets

- R: `task_agent_activity.state == running`
- W: `waiting | review_ready | failed | stale | canceled`
- No badge: session-only, subagent, context-compaction, journal-only events.
