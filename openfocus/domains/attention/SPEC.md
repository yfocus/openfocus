<!-- SPDX-License-Identifier: Apache-2.0 -->
# Attention Inbox Domain

The attention domain turns high-value journal events into persistent,
user-actionable recommendation items.

- Runtime R/W state no longer belongs here. It is owned by
  `openfocus/domains/agent_activity/` and persisted in `agent_turns` /
  `task_agent_activity`.
- External `/api/agent/events` and `focus_report` reports are journal-only and do
  not create current running/waiting activity.
- Next Move recommendation events may still create attention items because they
  are suggestions, not current runtime state.
- Active items remain visible until the user explicitly dismisses them or takes the primary action.
- Task actions prefer AgentSpace when present and fall back to the Dashboard task anchor.
- Low-value progress events remain in the Events timeline and audit memory.
